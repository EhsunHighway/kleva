from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..ast.model import CFunction, CParam, CTypeCatalog, DerivedLocal
from ..ir.model import FunctionIR
from ..ir.relations import negated_relation
from .candidates import (
    BranchCandidate,
    BranchFact,
    CallOutcomeFact,
    HelperSideEffectFact,
    NullnessFact,
    ObjectPathFact,
    OwnershipPathFact,
    PostStateFact,
    ScalarIntervalFact,
    SemanticFact,
    StateTransitionFact,
)


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
    ir_condition_candidates:   Callable[[FunctionIR], list[BranchCandidate]]
    ir_callback_candidates:    Callable[[FunctionIR, CFunction, CTypeCatalog | None], list[BranchCandidate]]
    ir_callee_candidates:      Callable[[FunctionIR, str | None, CTypeCatalog | None], list[BranchCandidate]]
    ir_parser_candidates:      Callable[[FunctionIR], list[BranchCandidate]]
    ir_table_candidates:       Callable[[FunctionIR], list[BranchCandidate]]
    loop_table_candidates:     Callable[..., list[BranchCandidate]]
    state_switch_candidates:   Callable[..., list[BranchCandidate]]
    ir_state_switch_candidates: Callable[[FunctionIR], list[BranchCandidate]]
    fallback_lookup_candidates: Callable[..., list[BranchCandidate]]
    callee_success_candidates: Callable[..., list[BranchCandidate]]
    ir_lookup_candidates:      Callable[[FunctionIR], list[BranchCandidate]] = field(default=lambda _ir: [])


def source_branch_candidates(
    func: CFunction,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str],
    ops: BranchShapeOps,
    function_ir: FunctionIR | None = None,
) -> list[BranchCandidate]:
    """
    Generate static implementation-shaped path candidates.

    These are not tests yet. They are extra fixture variants that must still
    pass KLEE/EVA/native certification before unit tests are emitted.
    """
    params = {p.name: p for p in func.params}
    candidates: list[BranchCandidate] = []
    seen_names: set[str] = set()
    ir_preferred_setups: set[tuple[str, ...]] = set()
    ir_preferred_facts: set[SemanticFact] = set()
    regex_fallbacks = "regex-fallbacks" in shaping_features
    body = ops.source_for_branch_shaping(source_text, func.name) if regex_fallbacks else ""
    if not body and function_ir is None:
        return []

    aliases = ops.cast_aliases(body, params) if regex_fallbacks and body else {}
    decoded_aliases = ops.decoded_field_aliases(body) if regex_fallbacks and body else {}
    direct_aliases = ops.direct_field_aliases(body) if regex_fallbacks and body else {}
    derived_aliases = ops.derived_local_aliases(body) if regex_fallbacks and body else {}
    checksum_fixups = ops.checksum_recompute_lines(body, aliases) if regex_fallbacks and body else []
    pointer_guard_setup = (
        ops.alias_pointer_guard_setup(body, aliases, type_catalog, set())
        if regex_fallbacks and body and type_catalog
        else []
    )
    macro_values = _numeric_macro_values(source_text or body) if regex_fallbacks else {}

    def add_candidate(name: str, setup: list[str], branch_facts: list[BranchFact] | None = None) -> None:
        safe = ops.safe_c_name(name)
        append_candidate(
            BranchCandidate(safe, setup, origin="regex", branch_facts=branch_facts or []),
            skip_if_ir_setup_seen=True,
        )

    def append_candidate(
        candidate: BranchCandidate,
        *,
        prefer_by_setup: bool = False,
        skip_if_ir_setup_seen: bool = False,
        default_origin: str | None = None,
    ) -> bool:
        if candidate.name in seen_names:
            return False
        setup_key = tuple(candidate.setup)
        if skip_if_ir_setup_seen and setup_key and setup_key in ir_preferred_setups:
            return False
        semantic_facts = _normalized_semantic_facts(candidate.semantic_facts(), macro_values)
        if skip_if_ir_setup_seen and semantic_facts:
            if any(fact in ir_preferred_facts for fact in semantic_facts):
                return False
        if candidate.origin is None:
            candidate.origin = default_origin
        seen_names.add(candidate.name)
        if prefer_by_setup and setup_key:
            ir_preferred_setups.add(setup_key)
        if prefer_by_setup:
            ir_preferred_facts.update(_normalized_semantic_facts(candidate.semantic_facts(), macro_values))
        candidates.append(candidate)
        return True

    def rhs_visible_in_harness(rhs: str) -> bool:
        if "->" not in rhs:
            if rhs in params or rhs in aliases:
                return True
            return ops.literal_or_macro_value(rhs)
        base = rhs.split("->", 1)[0].strip()
        return base in params or base in aliases

    ir_condition_emitted = False
    if function_ir is not None and "branch-conditions" in shaping_features:
        for candidate in ops.ir_condition_candidates(function_ir):
            ir_condition_emitted = append_candidate(candidate, prefer_by_setup=True, default_origin="ir") or ir_condition_emitted

    if function_ir is not None and "function-pointers" in shaping_features:
        for candidate in ops.ir_callback_candidates(function_ir, func, type_catalog):
            append_candidate(candidate, prefer_by_setup=True, default_origin="ir")

    if function_ir is not None and "callee-success" in shaping_features:
        callee_source_text = source_text if regex_fallbacks else None
        for candidate in ops.ir_callee_candidates(function_ir, callee_source_text, type_catalog):
            append_candidate(candidate, prefer_by_setup=True, default_origin="ir")

    if function_ir is not None and "parser-headers" in shaping_features:
        for candidate in ops.ir_parser_candidates(function_ir):
            append_candidate(candidate, prefer_by_setup=True, default_origin="ir")

    if function_ir is not None and "state-switches" in shaping_features:
        for candidate in ops.ir_state_switch_candidates(function_ir):
            append_candidate(candidate, prefer_by_setup=True, default_origin="ir")

    if function_ir is not None and "loop-tables" in shaping_features:
        for candidate in ops.ir_table_candidates(function_ir):
            append_candidate(candidate, prefer_by_setup=True, default_origin="ir")

    ir_lookup_emitted = False
    if function_ir is not None and "fallback-lookups" in shaping_features:
        for candidate in ops.ir_lookup_candidates(function_ir):
            ir_lookup_emitted = append_candidate(candidate, prefer_by_setup=True, default_origin="ir") or ir_lookup_emitted

    if regex_fallbacks and body and "casted-fields" in shaping_features:
        for alias, (cast_type, expr) in aliases.items():
            backing_setup = [*pointer_guard_setup, *ops.cast_alias_backing_setup(alias, cast_type, expr, params)]
            for m in re.finditer(rf"switch\s*\(\s*{re.escape(alias)}->(\w+)\s*\)", body):
                field = m.group(1)
                for case in re.findall(r"\bcase\s+([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*:", body[m.end():]):
                    setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = {case};"]
                    if re.search(rf"{re.escape(alias)}->code\s*==\s*0", body):
                        setup.append(f"(({cast_type} *){expr})->code = 0;")
                    setup.extend(checksum_fixups)
                    add_candidate(
                        f"source_case_{case}",
                        setup,
                        [BranchFact(ops.cast_field_expr(cast_type, expr, field), "case", case)],
                    )
                setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = 255;"]
                setup.extend(checksum_fixups)
                add_candidate(
                    f"source_default_{field}",
                    setup,
                    [BranchFact(ops.cast_field_expr(cast_type, expr, field), "default", "255")],
                )

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(
                    f"source_{alias}_{field}_{value}",
                    setup,
                    [BranchFact(ops.cast_field_expr(cast_type, expr, field), "==", value)],
                )

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(
                    f"source_{alias}_{field}_eq_{value}",
                    setup,
                    [BranchFact(ops.cast_field_expr(cast_type, expr, field), "==", value)],
                )
                setup = [*backing_setup, f"{ops.cast_field_expr(cast_type, expr, field)} = {ops.nonmatching_value(value)};"]
                setup.extend(checksum_fixups)
                add_candidate(
                    f"source_{alias}_{field}_ne_{value}",
                    setup,
                    [BranchFact(ops.cast_field_expr(cast_type, expr, field), "!=", value)],
                )

    if regex_fallbacks and body and "byte-order" in shaping_features and not ir_condition_emitted:
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
                    [BranchFact(target, op, rhs)],
                )
                add_candidate(
                    f"source_{local}_not_{ops.safe_c_name(op)}_{ops.safe_c_name(rhs)}",
                    [f"{target} = {encode_fn}({false_value});"],
                    [BranchFact(target, negated_relation(op), rhs)],
                )

    if regex_fallbacks and body:
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
                        [BranchFact(f"{p.name}->{field}", "==", value)],
                    )
                    add_candidate(
                        f"source_{p.name}_{field}_ne_{value}",
                        [*pointer_guard_setup, f"{p.name}->{field} = {ops.nonmatching_value(value)};"],
                        [BranchFact(f"{p.name}->{field}", "!=", value)],
                    )

            for field, value in re.findall(
                rf"\b{re.escape(p.name)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                add_candidate(
                    f"source_{p.name}_{field}_eq_{value}",
                    [*pointer_guard_setup, f"{p.name}->{field} = {value};"],
                    [BranchFact(f"{p.name}->{field}", "==", value)],
                )

        for candidate in ops.loop_table_candidates(body, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
            append_candidate(candidate, skip_if_ir_setup_seen=True, default_origin="regex")

    if regex_fallbacks and body:
        for candidate in ops.state_switch_candidates(body, source_text, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
            append_candidate(candidate, skip_if_ir_setup_seen=True, default_origin="regex")

    if regex_fallbacks and body and not ir_lookup_emitted:
        for candidate in ops.fallback_lookup_candidates(body, source_text, aliases, decoded_aliases, direct_aliases, derived_aliases, type_catalog, shaping_features):
            append_candidate(candidate, skip_if_ir_setup_seen=True, default_origin="regex")

    if regex_fallbacks and body:
        for candidate in ops.callee_success_candidates(body, source_text, type_catalog, set(params), shaping_features):
            append_candidate(candidate, skip_if_ir_setup_seen=True, default_origin="regex")

    return candidates


def _numeric_macro_values(source_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, value in re.findall(r"^\s*#\s*define\s+([A-Za-z_]\w*)\s+(0x[0-9a-fA-F]+|\d+)\b", source_text, flags=re.MULTILINE):
        values[name] = str(int(value, 0))
    return values


def _normalized_semantic_facts(
    facts:        tuple[SemanticFact, ...],
    macro_values: dict[str, str],
) -> tuple[SemanticFact, ...]:
    return tuple(_normalized_semantic_fact(fact, macro_values) for fact in facts)


def _normalized_semantic_fact(fact: SemanticFact, macro_values: dict[str, str]) -> SemanticFact:
    if isinstance(fact, BranchFact):
        return BranchFact(
            _normalize_macro_text(fact.target, macro_values),
            fact.relation,
            _normalize_macro_text(fact.value, macro_values),
        )
    if isinstance(fact, PostStateFact):
        return PostStateFact(
            _normalize_macro_text(fact.target, macro_values),
            fact.relation,
            _normalize_macro_text(fact.value, macro_values),
        )
    if isinstance(fact, NullnessFact):
        return NullnessFact(
            _normalize_macro_text(fact.target, macro_values),
            fact.state,
        )
    if isinstance(fact, ScalarIntervalFact):
        return ScalarIntervalFact(
            _normalize_macro_text(fact.target, macro_values),
            _normalize_macro_text(fact.lower, macro_values) if fact.lower is not None else None,
            _normalize_macro_text(fact.upper, macro_values) if fact.upper is not None else None,
            _normalize_macro_text(fact.exact, macro_values) if fact.exact is not None else None,
        )
    if isinstance(fact, OwnershipPathFact):
        return OwnershipPathFact(
            _normalize_macro_text(fact.target, macro_values),
            fact.action,
            _normalize_macro_text(fact.via, macro_values),
        )
    if isinstance(fact, HelperSideEffectFact):
        return HelperSideEffectFact(
            fact.kind,
            _normalize_macro_text(fact.target, macro_values),
            _normalize_macro_text(fact.value, macro_values) if fact.value is not None else None,
            _normalize_macro_text(fact.evidence, macro_values) if fact.evidence is not None else None,
        )
    if isinstance(fact, StateTransitionFact):
        return StateTransitionFact(
            _normalize_macro_text(fact.selector, macro_values),
            _normalize_macro_text(fact.source, macro_values),
            _normalize_macro_text(fact.target, macro_values),
            _normalize_macro_text(fact.guard, macro_values) if fact.guard is not None else None,
            _normalize_macro_text(fact.via, macro_values) if fact.via is not None else None,
        )
    if isinstance(fact, ObjectPathFact):
        return ObjectPathFact(
            _normalize_macro_text(fact.root, macro_values),
            tuple(_normalize_macro_text(part, macro_values) for part in fact.path),
            fact.root_type,
            fact.value_type,
        )
    if isinstance(fact, CallOutcomeFact):
        return fact
    return fact


def _normalize_macro_text(text: str, macro_values: dict[str, str]) -> str:
    out = text
    for name, value in sorted(macro_values.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(rf"\b{re.escape(name)}\b", value, out)
    return out
