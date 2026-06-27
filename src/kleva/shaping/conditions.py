from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import DerivedLocal


@dataclass(frozen=True)
class ConditionSetupOps:
    setup_local_bitwise_or: Callable[..., list[str]]
    setup_local_value:      Callable[..., list[str]]
    append_unique:          Callable[[list[str], str, set[str]], None]
    nonmatching_value:      Callable[[str], str]


def split_conjuncts(expr: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        if depth == 0 and expr[i:i + 2] == "&&":
            part = "".join(cur).strip()
            if part:
                parts.append(part)
            cur = []
            i += 2
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def strip_outer_parens(expr: str) -> str:
    out = expr.strip()
    changed = True
    while changed and out.startswith("(") and out.endswith(")"):
        changed = False
        depth = 0
        balanced_outer = True
        for i, ch in enumerate(out):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(out) - 1:
                    balanced_outer = False
                    break
        if balanced_outer:
            out = out[1:-1].strip()
            changed = True
    return out


def rewrite_result_expr(
    expr: str,
    result_var: str,
    result_expr: str,
) -> str:
    return re.sub(rf"\b{re.escape(result_var)}->", f"{result_expr}.", expr.strip())


def rewrite_source_alias_exprs(
    line: str,
    aliases: dict[str, tuple[str, str]],
    result_var: str | None = None,
    result_expr: str | None = None,
) -> str:
    out = line
    if result_var and result_expr:
        out = re.sub(rf"\b{re.escape(result_var)}->", f"{result_expr}.", out)
    for alias, (cast_type, expr) in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(
            rf"\b{re.escape(alias)}->",
            f"(({cast_type} *){expr})->",
            out,
        )
    return out


def condition_setup_lines(
    condition: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    ops: ConditionSetupOps,
    result_var: str | None = None,
    result_expr: str | None = None,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()

    for raw_part in split_conjuncts(condition):
        part = strip_outer_parens(raw_part)

        m = re.fullmatch(r"(\w+)\s*&\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, value = m.groups()
            for line in ops.setup_local_bitwise_or(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                ops.append_unique(setup, line, seen)
            continue

        m = re.fullmatch(r"!\s*\(\s*(\w+)\s*&\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*\)", part)
        if m:
            local, _value = m.groups()
            for line in ops.setup_local_value(local, "0", aliases, decoded_aliases, direct_aliases, derived_aliases):
                ops.append_unique(setup, line, seen)
            continue

        m = re.fullmatch(
            r"(\w+)\s*(==|!=|>|>=|<|<=)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
            part,
        )
        if m:
            local, op, rhs = m.groups()
            if result_var and result_expr:
                rhs = rewrite_result_expr(rhs, result_var, result_expr)
            if op == "==":
                value = rhs
            elif op == "!=":
                value = ops.nonmatching_value(rhs)
            elif op == ">":
                value = f"(({rhs}) + 1)"
            elif op == ">=":
                value = rhs
            elif op == "<":
                value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
            else:
                value = rhs
            for line in ops.setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                ops.append_unique(setup, line, seen)
            continue

        m = re.fullmatch(r"(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, value = m.groups()
            for line in ops.setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                ops.append_unique(setup, line, seen)
            continue

    return setup
