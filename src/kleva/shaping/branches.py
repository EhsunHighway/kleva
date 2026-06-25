from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CFunction, CParam, CTypeCatalog, DerivedLocal
from .candidates import BranchCandidate


@dataclass(frozen=True)
class BranchShapeOps:
    source_for_branch_shaping: Callable[[str | None, str], str]
    cast_aliases:              Callable[[str, dict[str, CParam]], dict[str, tuple[str, str]]]
    decoded_field_aliases:     Callable[[str], dict[str, tuple[str, str, str]]]
    direct_field_aliases:      Callable[[str], dict[str, tuple[str, str]]]
    derived_local_aliases:     Callable[[str], dict[str, DerivedLocal]]
    checksum_recompute_lines:  Callable[[str, dict[str, tuple[str, str]]], list[str]]
    alias_pointer_guard_setup: Callable[..., list[str]]
    cast_alias_backing_setup:  Callable[[str, str, str, dict[str, CParam]], list[str]]
    cast_field_expr:           Callable[[str, str, str], str]
    host_to_network_fn:        Callable[[str], str]
    nonmatching_value:         Callable[[str], str]
    literal_or_macro_value:    Callable[[str], bool]
    safe_c_name:               Callable[[str], str]
    is_void_star:              Callable[[CParam], bool]
    loop_table_candidates:     Callable[..., list[BranchCandidate]]
    state_switch_candidates:   Callable[..., list[BranchCandidate]]
    fallback_lookup_candidates: Callable[..., list[BranchCandidate]]
    callee_success_candidates: Callable[..., list[BranchCandidate]]


def source_branch_candidates(
    func: CFunction,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str],
    ops: BranchShapeOps,
) -> list[BranchCandidate]:
    """
    Generate static source-shaped path candidates from the function body.

    These are not tests yet. They are extra fixture variants that must still
    pass KLEE/EVA/native certification before unit tests are emitted.
    """
    body = ops.source_for_branch_shaping(source_text, func.name)
    if not body:
        return []

    params = {p.name: p for p in func.params}
    aliases = ops.cast_aliases(body, params)
    decoded_aliases = ops.decoded_field_aliases(body)
    direct_aliases = ops.direct_field_aliases(body)
    derived_aliases = ops.derived_local_aliases(body)
    checksum_fixups = ops.checksum_recompute_lines(body, aliases)
    pointer_guard_setup = ops.alias_pointer_guard_setup(body, aliases, type_catalog, set()) if type_catalog else []
    candidates: list[BranchCandidate] = []
    seen_names: set[str] = set()

    def add_candidate(name: str, setup: list[str]) -> None:
        safe = ops.safe_c_name(name)
        if safe in seen_names:
            return
        seen_names.add(safe)
        candidates.append(BranchCandidate(safe, setup))

    def rhs_visible_in_harness(rhs: str) -> bool:
        if "->" not in rhs:
            if rhs in params or rhs in aliases:
                return True
            return ops.literal_or_macro_value(rhs)
        base = rhs.split("->", 1)[0].strip()
        return base in params or base in aliases

    if "casted-fields" in shaping_features:
        for alias, (cast_type, expr) in aliases.items():
            backing_setup = [*pointer_guard_setup, *ops.cast_alias_backing_setup(alias, cast_type, expr, params)]
            for m in re.finditer(rf"switch\s*\(\s*{re.escape(alias)}->(\w+)\s*\)", body):
                field = m.group(1)
                for case in re.findall(r"\bcase\s+([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*:", body[m.end():]):
                    setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = {case};"]
                    if re.search(rf"{re.escape(alias)}->code\s*==\s*0", body):
                        setup.append(f"(({cast_type} *){expr})->code = 0;")
                    setup.extend(checksum_fixups)
                    add_candidate(f"source_case_{case}", setup)
                setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = 255;"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_default_{field}", setup)

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_{value}", setup)

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_eq_{value}", setup)
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {ops.nonmatching_value(value)};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_ne_{value}", setup)

    if "byte-order" in shaping_features:
        for local, (decode_fn, alias, field) in decoded_aliases.items():
            if alias not in aliases:
                continue
            cast_type, expr = aliases[alias]
            encode_fn = ops.host_to_network_fn(decode_fn)
            if not encode_fn:
                continue
            for op, rhs in re.findall(
                rf"\b{re.escape(local)}\s*(<|>|<=|>=|==|!=)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                if not rhs_visible_in_harness(rhs):
                    continue
                if op == "<":
                    true_value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
                    false_value = rhs
                elif op == ">":
                    true_value = f"(({rhs}) + 1)"
                    false_value = rhs
                elif op == "!=":
                    true_value = ops.nonmatching_value(rhs)
                    false_value = rhs
                elif op == "==":
                    true_value = rhs
                    false_value = ops.nonmatching_value(rhs)
                elif op == "<=":
                    true_value = rhs
                    false_value = f"(({rhs}) + 1)"
                else:
                    true_value = rhs
                    false_value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
                target = ops.cast_field_expr(cast_type, expr, field)
                add_candidate(
                    f"source_{local}_{ops.safe_c_name(op)}_{ops.safe_c_name(rhs)}",
                    [f"{target} = {encode_fn}({true_value});"],
                )
                add_candidate(
                    f"source_{local}_not_{ops.safe_c_name(op)}_{ops.safe_c_name(rhs)}",
                    [f"{target} = {encode_fn}({false_value});"],
                )

    for p in func.params:
        if not p.is_pointer or ops.is_void_star(p):
            continue
        field_guards = re.findall(
            rf"\b{re.escape(p.name)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        )
        by_field: dict[str, list[str]] = {}
        for field, value in field_guards:
            by_field.setdefault(field, []).append(value)
        for field, values in by_field.items():
            for value in values:
                add_candidate(
                    f"source_{p.name}_{field}_{value}",
                    [*pointer_guard_setup, f"{p.name}->{field} = {value};"],
                )

        for field, value in re.findall(
            rf"\b{re.escape(p.name)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            add_candidate(
                f"source_{p.name}_{field}_eq_{value}",
                [*pointer_guard_setup, f"{p.name}->{field} = {value};"],
            )

    for candidate in ops.loop_table_candidates(body, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    for candidate in ops.state_switch_candidates(body, source_text, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    for candidate in ops.fallback_lookup_candidates(body, source_text, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    for candidate in ops.callee_success_candidates(body, source_text, type_catalog, set(params), shaping_features):
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    return candidates
