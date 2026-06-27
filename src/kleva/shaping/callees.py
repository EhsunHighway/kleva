from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CTypeCatalog
from .candidates import BranchCandidate, CallOutcomeFact
from .conditions import split_conjuncts, strip_outer_parens


@dataclass(frozen=True)
class CalleeSuccessOps:
    function_decl_map:           Callable[[str], dict]
    function_definition_body:    Callable[[str | None, str], str]
    split_call_args:             Callable[[str], list[str]]
    append_unique:               Callable[[list[str], str, set[str]], None]
    nonmatching_value:           Callable[[str], str]
    literal_or_macro_value:      Callable[[str], bool]
    safe_c_name:                 Callable[[str], str]


def return_guard_conditions(prefix: str) -> list[str]:
    conditions: list[str] = []
    i = 0
    while i < len(prefix):
        m = re.search(r"\bif\s*\(", prefix[i:])
        if not m:
            break
        cond_start = i + m.end()
        depth = 1
        j = cond_start
        while j < len(prefix) and depth > 0:
            ch = prefix[j]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            j += 1
        if depth != 0:
            break

        condition = prefix[cond_start:j - 1].strip()
        following = prefix[j:j + 300]
        if re.match(r"\s*return\b", following) or re.match(r"\s*\{[^{}]*\breturn\b", following, flags=re.DOTALL):
            conditions.append(condition)
        i = j
    return conditions


def invert_simple_return_guard(
    condition: str,
    visible_roots: set[str],
    append_unique: Callable[[list[str], str, set[str]], None],
    nonmatching_value: Callable[[str], str],
) -> list[str]:
    if "||" in condition:
        return []

    setup: list[str] = []
    seen: set[str] = set()
    not_equals_by_field: dict[tuple[str, str], list[str]] = {}
    parts = [strip_outer_parens(p) for p in split_conjuncts(condition)]

    for part in parts:
        m = re.fullmatch(r"!\s*([A-Za-z_]\w*)", part)
        if m:
            local = m.group(1)
            if local in visible_roots:
                append_unique(setup, f"{local} = 1;", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)", part)
        if m:
            local = m.group(1)
            if local in visible_roots:
                append_unique(setup, f"{local} = 0;", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, rhs = m.groups()
            if local in visible_roots:
                append_unique(setup, f"{local} = {rhs};", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, rhs = m.groups()
            if local in visible_roots:
                append_unique(setup, f"{local} = {nonmatching_value(rhs)};", seen)
            continue

        m = re.fullmatch(
            r"([A-Za-z_]\w*)\s*(<|<=|>|>=)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            part,
        )
        if m:
            local, op, rhs = m.groups()
            if local not in visible_roots:
                continue
            if op == "<":
                value = rhs
            elif op == "<=":
                value = f"(({rhs}) + 1)"
            elif op == ">":
                value = rhs
            else:
                value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
            append_unique(setup, f"{local} = {value};", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            obj, field, rhs = m.groups()
            if obj in visible_roots:
                not_equals_by_field.setdefault((obj, field), []).append(rhs)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            obj, field, rhs = m.groups()
            if obj in visible_roots:
                append_unique(setup, f"{obj}->{field} = {nonmatching_value(rhs)};", seen)
            continue

        m = re.fullmatch(r"!\s*([A-Za-z_]\w*)->([A-Za-z_]\w*)", part)
        if m:
            obj, field = m.groups()
            if obj in visible_roots:
                append_unique(setup, f"{obj}->{field} = 1;", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", part)
        if m:
            obj, field = m.groups()
            if obj in visible_roots:
                append_unique(setup, f"{obj}->{field} = 0;", seen)
            continue

        m = re.fullmatch(
            r"([A-Za-z_]\w*)->([A-Za-z_]\w*)\s*(<|<=|>|>=)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            part,
        )
        if m:
            obj, field, op, rhs = m.groups()
            if obj not in visible_roots:
                continue
            if op == "<":
                value = rhs
            elif op == "<=":
                value = f"(({rhs}) + 1)"
            elif op == ">":
                value = rhs
            else:
                value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
            append_unique(setup, f"{obj}->{field} = {value};", seen)

    for (obj, field), values in not_equals_by_field.items():
        append_unique(setup, f"{obj}->{field} = {values[0]};", seen)

    return setup


def callee_success_setup_for_call(
    callee: str,
    args: list[str],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: CalleeSuccessOps,
) -> tuple[list[str], list[str]]:
    if not source_text or not type_catalog:
        return [], []

    function_decls = ops.function_decl_map(source_text)
    callee_decl = function_decls.get(callee)
    callee_body = ops.function_definition_body(source_text, callee)
    if not callee_decl or not callee_body or len(args) != len(callee_decl.params):
        return [], []

    arg_by_param = {p.name: a for p, a in zip(callee_decl.params, args)}
    visible_roots = set(arg_by_param.values())
    setup: list[str] = []
    seen: set[str] = set()
    for condition in return_guard_conditions(callee_body):
        rewritten = condition
        for param, arg in sorted(arg_by_param.items(), key=lambda item: len(item[0]), reverse=True):
            rewritten = re.sub(rf"\b{re.escape(param)}\b", arg, rewritten)
        for line in invert_simple_return_guard(rewritten, visible_roots, ops.append_unique, ops.nonmatching_value):
            ops.append_unique(setup, line, seen)
    return setup, []


def source_guard_setup_before_call(
    body: str,
    call_pos: int,
    visible_roots: set[str],
    append_unique: Callable[[list[str], str, set[str]], None],
    nonmatching_value: Callable[[str], str],
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    for condition in return_guard_conditions(body[:call_pos]):
        for line in invert_simple_return_guard(condition, visible_roots, append_unique, nonmatching_value):
            append_unique(setup, line, seen)
    return setup


def callee_success_setups_in_block(
    block: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: CalleeSuccessOps,
) -> tuple[list[str], list[str]]:
    """Return structural setup that makes visible checked callees succeed."""
    if not source_text or not type_catalog:
        return [], []

    setup: list[str] = []
    preamble: list[str] = []
    seen_setup: set[str] = set()
    seen_preamble: set[str] = set()

    patterns = [
        re.compile(
            r"\bint\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(([^;]*)\)\s*;\s*"
            r"if\s*\(\s*\1\s*==\s*-1\s*\)",
            flags=re.DOTALL,
        ),
        re.compile(
            r"\bif\s*\(\s*([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*==\s*-1\s*\)",
            flags=re.DOTALL,
        ),
    ]

    calls: list[tuple[str, str]] = []
    for m in patterns[0].finditer(block):
        _result_var, callee, args_raw = m.groups()
        calls.append((callee, args_raw))
    for m in patterns[1].finditer(block):
        callee, args_raw = m.groups()
        calls.append((callee, args_raw))

    for callee, args_raw in calls:
        callee_setup, callee_preamble = callee_success_setup_for_call(
            callee,
            ops.split_call_args(args_raw),
            source_text,
            type_catalog,
            ops,
        )
        for line in callee_setup:
            ops.append_unique(setup, line, seen_setup)
        for line in callee_preamble:
            ops.append_unique(preamble, line, seen_preamble)

    return setup, preamble


def callee_success_candidates(
    body: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    visible_roots: set[str],
    shaping_features: set[str],
    ops: CalleeSuccessOps,
) -> list[BranchCandidate]:
    if "callee-success" not in shaping_features:
        return []

    candidates: list[BranchCandidate] = []
    seen: set[str] = set()

    def visible(expr: str) -> bool:
        expr = expr.strip()
        if ops.literal_or_macro_value(expr):
            return True
        m = re.match(r"([A-Za-z_]\w*)", expr)
        return bool(m and m.group(1) in visible_roots)

    patterns = [
        re.compile(
            r"\bint\s+([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(([^;]*)\)\s*;\s*"
            r"if\s*\(\s*\1\s*==\s*-1\s*\)",
            flags=re.DOTALL,
        ),
        re.compile(
            r"\bif\s*\(\s*([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*==\s*-1\s*\)",
            flags=re.DOTALL,
        ),
    ]

    for m in patterns[0].finditer(body):
        _result_var, callee, args_raw = m.groups()
        args = ops.split_call_args(args_raw)
        if not all(visible(arg) for arg in args[:3]):
            continue
        setup, preamble = callee_success_setup_for_call(callee, args, source_text, type_catalog, ops)
        if not setup:
            continue
        guard_setup = source_guard_setup_before_call(body, m.start(), visible_roots, ops.append_unique, ops.nonmatching_value)
        name = ops.safe_c_name(f"source_{callee}_success")
        if name in seen:
            continue
        seen.add(name)
        candidates.append(BranchCandidate(
            name,
            [*guard_setup, *setup],
            preamble,
            witness_outputs=True,
            origin="regex",
            call_facts=[CallOutcomeFact(callee, "equals_-1", "success")],
        ))

    for m in patterns[1].finditer(body):
        callee, args_raw = m.groups()
        args = ops.split_call_args(args_raw)
        if not all(visible(arg) for arg in args[:3]):
            continue
        setup, preamble = callee_success_setup_for_call(callee, args, source_text, type_catalog, ops)
        if not setup:
            continue
        guard_setup = source_guard_setup_before_call(body, m.start(), visible_roots, ops.append_unique, ops.nonmatching_value)
        name = ops.safe_c_name(f"source_{callee}_success")
        if name in seen:
            continue
        seen.add(name)
        candidates.append(BranchCandidate(
            name,
            [*guard_setup, *setup],
            preamble,
            witness_outputs=True,
            origin="regex",
            call_facts=[CallOutcomeFact(callee, "equals_-1", "success")],
        ))

    return candidates
