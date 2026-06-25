"""
synth.py — `kleva synth` — ACSL-aware YAML synthesizer.

Generates a complete, production-ready kleva YAML config from a C header
with ACSL annotations.  Unlike `kleva init` (which left TODOs), this
module reads the contract annotations to produce:

  - Null-guard tests             for every behavior that assumes \\null
  - Valid-path tests             for every behavior that assumes \\valid
  - Output variables             inferred from `ensures` clauses (\result == N)
  - Cleanup patterns             inferred from return type and pointer params
  - Constructor-call setup       inferred from pointer type names (T * → T_create())

No manual TODO filling is required — the output is ready for `kleva all`.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .acsl import ACSLSpec, ACSLBehavior
from .acsl_contract import (
    extract_non_null_params as _extract_non_null_params,
    extract_nonzero_params as _extract_nonzero_params,
    extract_null_params as _extract_null_params,
    extract_result_value as _extract_result_value,
    extract_valid_params as _extract_valid_params,
    scalar_values_from_assumptions as _scalar_values_from_assumptions,
)
from .ast.model import CFunction, CParam, CTypeCatalog, DerivedLocal
from .ast.parser import (
    build_type_catalog,
    function_decl_map as _function_decl_map,
    parse_header,
    parse_param as _parse_param,
    split_call_args as _split_call_args,
    strip_comments as _strip_comments,
)
from .ast.source_query import (
    function_accepts_null_param as _function_accepts_null_param,
    function_body as _function_body,
    function_definition_body as _function_definition_body,
    function_frees_param as _function_frees_param,
    function_returns_owned_pointer as _function_returns_owned_pointer,
    function_takes_param_ownership as _function_takes_param_ownership,
)
from .bodygen import (
    BodyGenOps,
    append_return_field_outputs as _bodygen_append_return_field_outputs,
    append_source_witness_outputs as _bodygen_append_source_witness_outputs,
    field_expr as _bodygen_field_expr,
    gen_mixed_test as _bodygen_gen_mixed_test,
    gen_null_setup_body as _bodygen_gen_null_setup_body,
    gen_valid_setup_body as _bodygen_gen_valid_setup_body,
    is_observable_scalar_type as _bodygen_is_observable_scalar_type,
    param_ref_from_arg as _bodygen_param_ref_from_arg,
)
from .config import resolve_klee_clang, resolve_klee_include, resolve_llvm_link
from .fixtures.construction import (
    default_return_value as _default_return_value,
    default_scalar_value as _default_scalar_value,
    function_pointer_stub_name as _function_pointer_stub_name,
    function_pointer_stub_preamble as _function_pointer_stub_preamble,
    is_void_star as _is_void_star,
    lookup_constructor as _lookup_constructor,
    lookup_free_fn as _lookup_free_fn,
    pointer_argument_setup as _pointer_argument_setup,
    safe_c_name as _safe_c_name,
    unique_name as _unique_name,
)
from .source_discovery import (
    collect_source_include_headers as _collect_source_include_headers,
    collect_visible_headers as _collect_visible_headers,
    dedupe_paths as _dedupe_paths,
    source_include_names as _source_include_names,
    suggest_extra_sources as _suggest_extra_sources,
)
from .shaping.byte_order import host_to_network_fn as _byte_order_host_to_network_fn
from .shaping.branches import (
    BranchShapeOps,
    source_branch_candidates as _branch_shaper_source_branch_candidates,
)
from .shaping.assumptions import (
    assumption_setup_lines as _assumption_shaper_setup_lines,
)
from .shaping.candidates import BranchCandidate
from .shaping.callees import (
    CalleeSuccessOps,
    callee_success_candidates as _callee_shaper_success_candidates,
    callee_success_setup_for_call as _callee_shaper_success_setup_for_call,
    callee_success_setups_in_block as _callee_shaper_success_setups_in_block,
    invert_simple_return_guard as _callee_shaper_invert_simple_return_guard,
    return_guard_conditions as _callee_shaper_return_guard_conditions,
    source_guard_setup_before_call as _callee_shaper_source_guard_setup_before_call,
)
from .shaping.conditions import (
    ConditionSetupOps,
    FunctionPointerConditionOps,
    condition_function_pointer_setup as _condition_shaper_function_pointer_setup,
    condition_setup_lines as _condition_shaper_setup_lines,
    rewrite_result_expr as _condition_shaper_rewrite_result_expr,
    rewrite_source_alias_exprs as _condition_shaper_rewrite_source_alias_exprs,
    split_conjuncts as _condition_shaper_split_conjuncts,
    strip_outer_parens as _condition_shaper_strip_outer_parens,
)
from .shaping.lookups import (
    FallbackLookupOps,
    LookupInferOps,
    LookupSetupOps,
    LookupShape,
    fallback_lookup_candidates as _lookup_shaper_fallback_lookup_candidates,
    infer_lookup_shape as _lookup_shaper_infer_lookup_shape,
    infer_lookup_shape_for_call as _lookup_shaper_infer_lookup_shape_for_call,
    lookup_condition_setup as _lookup_shaper_condition_setup,
    lookup_miss_setup as _lookup_shaper_miss_setup,
)
from .shaping.source_aliases import (
    cast_alias_backing_setup as _source_aliases_cast_alias_backing_setup,
    cast_aliases as _source_aliases_cast_aliases,
    cast_field_expr as _source_aliases_cast_field_expr,
    checksum_recompute_lines as _source_aliases_checksum_recompute_lines,
    decoded_field_aliases as _source_aliases_decoded_field_aliases,
    derived_local_aliases as _source_aliases_derived_local_aliases,
    direct_field_aliases as _source_aliases_direct_field_aliases,
    expand_alias_expr as _source_aliases_expand_alias_expr,
    field_expr_from_ref as _source_aliases_field_expr_from_ref,
    good_path_setup_from_source as _source_aliases_good_path_setup_from_source,
    literal_or_macro_value as _source_aliases_literal_or_macro_value,
    propagate_local_aliases as _source_aliases_propagate_local_aliases,
    setup_local_bitwise_or as _source_aliases_setup_local_bitwise_or,
    setup_local_value as _source_aliases_setup_local_value,
    void_param_cast_types as _source_aliases_void_param_cast_types,
)
from .shaping.switches import (
    StateSwitchOps,
    state_switch_candidates as _switch_shaper_state_switch_candidates,
    switch_case_blocks as _switch_shaper_case_blocks,
)
from .shaping.tables import (
    TableShapeOps,
    loop_table_candidates as _table_shaper_loop_table_candidates,
)
from .yaml_emit import emit_yaml_function as _emit_yaml_function


# ── C type knowledge ──────────────────────────────────────────────────────────

# Default symbolic bounds for scalar types
_SCALAR_BOUNDS: dict[str, tuple[int, int]] = {
    "uint8_t":  (0, 255),
    "uint16_t": (0, 65535),
    "uint32_t": (0, 4294967295),
    "uint64_t": (0, 1000000),
    "int":      (0, 2147483647),
    "size_t":   (1, 268435455),
}

SHAPING_FEATURES = {
    "function-pointers",
    "quantified-arrays",
    "casted-fields",
    "byte-order",
    "loop-tables",
    "state-switches",
    "callee-success",
    "fallback-lookups",
}
DEFAULT_SHAPING_FEATURES = frozenset(SHAPING_FEATURES)


def normalize_shaping_features(
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
) -> set[str]:
    """Resolve CLI shaping flags into the enabled feature set."""
    enabled = set(DEFAULT_SHAPING_FEATURES)
    if shaping:
        enabled = set()
        for raw in shaping:
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                if item == "all":
                    enabled.update(SHAPING_FEATURES)
                elif item == "none":
                    enabled.clear()
                else:
                    enabled.add(item)

    for raw in no_shaping or []:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if item == "all":
                enabled.clear()
            elif item != "none":
                enabled.discard(item)

    unknown = enabled.difference(SHAPING_FEATURES)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown shaping feature(s): {names}")
    return enabled


def _struct_has_fields(type_catalog: CTypeCatalog | None, type_name: str, fields: set[str]) -> bool:
    if not type_catalog:
        return False
    available = set(type_catalog.struct_fields.get(type_name, {}))
    return fields.issubset(available)


def _needs_len_data_shape(
    func_name: str,
    param_name: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    param: CParam,
) -> bool:
    """
    Some C APIs use a pointer to a buffer object whose real payload extent is
    stored in `len` while bytes live at `data`. Constructors often allocate
    capacity but leave len at zero. If the target path reads that length or
    passes the object to a clone/copy helper, give synth a concrete payload.
    """
    if not _struct_has_fields(type_catalog, param.base_type, {"len", "data"}):
        return False

    body = _source_for_branch_shaping(source_text, func_name)
    if not body:
        return False

    if re.search(rf"\b{re.escape(param_name)}->len\b", body):
        return True
    if re.search(rf"\b\w*(?:clone|copy|send|transmit|write)\w*\s*\([^;]*\b{re.escape(param_name)}\b", body):
        return True
    return False


def _append_len_data_shape(lines: list[str], arg: str) -> None:
    if arg == "NULL" or not re.fullmatch(r"[A-Za-z_]\w*", arg):
        return
    lines.append(f"if ({arg}->len == 0) {arg}->len = 8;")
    lines.append(f"memset({arg}->data, 0, {arg}->len);")


def _is_literal_or_macro(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Z][A-Z0-9_]*", value))


def _append_unique(lines: list[str], line: str, seen: set[str]) -> None:
    if line not in seen:
        lines.append(line)
        seen.add(line)


def _assumption_setup_lines(
    assumes_exprs: list[str],
    params_by_name: dict[str, CParam],
    source_text: str | None,
    param_refs: dict[str, tuple[str, str]] | None = None,
    param_args: dict[str, str] | None = None,
    shaping_features: set[str] | None = None,
) -> list[str]:
    """
    Convert simple ACSL assumptions into concrete fixture setup.

    This is intentionally conservative: it never asserts an oracle. It only
    tries to build an input state closer to the behavior's preconditions.
    """
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _assumption_shaper_setup_lines(
        assumes_exprs,
        params_by_name,
        param_refs,
        param_args,
        shaping_features,
    )


def _source_for_branch_shaping(source_text: str | None, func_name: str) -> str:
    body = _function_body(source_text, func_name)
    if not body:
        return ""
    for callee in re.findall(r"\b(?:return\s+)?(\w+)\s*\(", body):
        if callee == func_name or callee in {"if", "while", "switch", "for", "return"}:
            continue
        callee_body = _function_body(source_text, callee)
        if callee_body:
            body += "\n" + callee_body
    return body


def _cast_aliases(body: str, params: dict[str, CParam]) -> dict[str, tuple[str, str]]:
    return _source_aliases_cast_aliases(body, params)


def _void_param_cast_types(body: str, func: CFunction) -> dict[str, str]:
    return _source_aliases_void_param_cast_types(body, func, _is_void_star)


def _rewrite_setup_with_param_args(setup: list[str], param_args: dict[str, str]) -> list[str]:
    """Rewrite source-derived setup so it uses generated harness variables."""
    rewritten: list[str] = []
    for line in setup:
        new_line = line
        for name, arg in sorted(param_args.items(), key=lambda item: len(item[0]), reverse=True):
            new_line = re.sub(
                rf"\b{re.escape(name)}->",
                f"({arg})->",
                new_line,
            )
            new_line = re.sub(rf"(?<![&\w]){re.escape(name)}\b", arg, new_line)
        rewritten.append(new_line)
    return rewritten


def _checksum_recompute_lines(body: str, aliases: dict[str, tuple[str, str]]) -> list[str]:
    return _source_aliases_checksum_recompute_lines(body, aliases, _append_unique)


def _cast_field_expr(cast_type: str, expr: str, field: str) -> str:
    return _source_aliases_cast_field_expr(cast_type, expr, field)


def _expand_alias_expr(expr: str, aliases: dict[str, tuple[str, str]]) -> str:
    return _source_aliases_expand_alias_expr(expr, aliases)


def _cast_alias_backing_setup(alias: str, cast_type: str, expr: str, params: dict[str, CParam]) -> list[str]:
    return _source_aliases_cast_alias_backing_setup(alias, cast_type, expr, params, _safe_c_name)


def _propagate_local_aliases(body: str, aliases: dict) -> dict:
    return _source_aliases_propagate_local_aliases(body, aliases)


def _decoded_field_aliases(body: str) -> dict[str, tuple[str, str, str]]:
    return _source_aliases_decoded_field_aliases(body)


def _direct_field_aliases(body: str) -> dict[str, tuple[str, str]]:
    return _source_aliases_direct_field_aliases(body)


def _derived_local_aliases(body: str) -> dict[str, DerivedLocal]:
    return _source_aliases_derived_local_aliases(body)


def _literal_or_macro_value(value: str) -> bool:
    return _source_aliases_literal_or_macro_value(value)


def _field_expr_from_ref(ref: str) -> str | None:
    return _source_aliases_field_expr_from_ref(ref)


def _setup_local_bitwise_or(
    local: str,
    value: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None,
) -> list[str]:
    return _source_aliases_setup_local_bitwise_or(
        local,
        value,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
    )


def _setup_local_value(
    local: str,
    value: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None = None,
) -> list[str]:
    return _source_aliases_setup_local_value(
        local,
        value,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
    )


def _good_path_setup_from_source(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None = None,
) -> list[str]:
    return _source_aliases_good_path_setup_from_source(
        body,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        _append_unique,
    )


def _loop_table_candidates(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _table_shaper_loop_table_candidates(
        body,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        type_catalog,
        shaping_features,
        TableShapeOps(
            _good_path_setup_from_source,
            _host_to_network_fn,
            _cast_field_expr,
            _function_pointer_stub_preamble,
            _function_pointer_stub_name,
            _safe_c_name,
        ),
    )


def _infer_lookup_shape(
    caller_body: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
) -> list[LookupShape]:
    return _lookup_shaper_infer_lookup_shape(
        caller_body,
        source_text,
        type_catalog,
        LookupInferOps(_function_decl_map, _function_body, _split_call_args),
    )


def _infer_lookup_shape_for_call(
    callee: str,
    result_var: str,
    args_raw: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
) -> LookupShape | None:
    return _lookup_shaper_infer_lookup_shape_for_call(
        callee,
        result_var,
        args_raw,
        source_text,
        type_catalog,
        LookupInferOps(_function_decl_map, _function_body, _split_call_args),
    )


def _split_conjuncts(expr: str) -> list[str]:
    return _condition_shaper_split_conjuncts(expr)


def _strip_outer_parens(expr: str) -> str:
    return _condition_shaper_strip_outer_parens(expr)


def _rewrite_result_expr(
    expr: str,
    result_var: str,
    result_expr: str,
) -> str:
    return _condition_shaper_rewrite_result_expr(expr, result_var, result_expr)


def _rewrite_source_alias_exprs(
    line: str,
    aliases: dict[str, tuple[str, str]],
    result_var: str | None = None,
    result_expr: str | None = None,
) -> str:
    return _condition_shaper_rewrite_source_alias_exprs(line, aliases, result_var, result_expr)


def _condition_setup_lines(
    condition: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    result_var: str | None = None,
    result_expr: str | None = None,
) -> list[str]:
    return _condition_shaper_setup_lines(
        condition,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        ConditionSetupOps(_setup_local_bitwise_or, _setup_local_value, _append_unique, _nonmatching_value),
        result_var,
        result_expr,
    )


def _condition_function_pointer_setup(
    condition: str,
    result_var: str,
    result_expr: str,
    result_type: str,
    type_catalog: CTypeCatalog | None,
) -> tuple[list[str], list[str]]:
    return _condition_shaper_function_pointer_setup(
        condition,
        result_var,
        result_expr,
        result_type,
        type_catalog,
        FunctionPointerConditionOps(
            _split_conjuncts,
            _strip_outer_parens,
            _append_unique,
            _function_pointer_stub_preamble,
            _function_pointer_stub_name,
        ),
    )


def _callee_success_ops() -> CalleeSuccessOps:
    return CalleeSuccessOps(
        _function_decl_map,
        _function_definition_body,
        _split_call_args,
        _append_unique,
        _nonmatching_value,
        _literal_or_macro_value,
        _safe_c_name,
    )


def _callee_success_setups_in_block(
    block: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
) -> tuple[list[str], list[str]]:
    return _callee_shaper_success_setups_in_block(block, source_text, type_catalog, _callee_success_ops())


def _switch_case_blocks(body: str, switch_start: int) -> list[tuple[str, str]]:
    return _switch_shaper_case_blocks(body, switch_start)


def _lookup_container_setup(
    shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
) -> list[str]:
    raw_expr = shape.container_expr.strip()
    expr = _expand_alias_expr(raw_expr, aliases)
    storage = _safe_c_name(f"kleva_{shape.result_var}_{shape.array_field}_owner")
    setup = [
        f"{shape.container_type} {storage};",
        f"memset(&{storage}, 0, sizeof({storage}));",
    ]

    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", raw_expr)
    if m and m.group(1) in aliases:
        alias, field = m.groups()
        cast_type, cast_expr = aliases[alias]
        backing = _cast_alias_backing_setup(alias, cast_type, cast_expr, {})
        setup.extend(backing)
        setup.append(f"{_cast_field_expr(cast_type, cast_expr, field)} = &{storage};")
        return setup

    if re.fullmatch(r"[A-Za-z_]\w*", expr):
        setup.append(f"{expr} = &{storage};")
    return setup


def _alias_pointer_guard_setup(
    body: str,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
    skip_exprs: set[str] | None = None,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    skip_exprs = skip_exprs or set()

    for alias, (cast_type, cast_expr) in aliases.items():
        for field in re.findall(rf"!\s*{re.escape(alias)}->(\w+)", body):
            raw_expr = f"{alias}->{field}"
            if raw_expr in skip_exprs:
                continue

            field_param = type_catalog.field_type(cast_type, field)
            if not field_param or not field_param.is_pointer:
                continue

            target = _cast_field_expr(cast_type, cast_expr, field)
            if type_catalog.is_complete_struct(field_param.base_type):
                storage = _safe_c_name(f"kleva_{alias}_{field}_guard")
                _append_unique(setup, f"{field_param.base_type} {storage};", seen)
                _append_unique(setup, f"memset(&{storage}, 0, sizeof({storage}));", seen)
                _append_unique(setup, f"{target} = &{storage};", seen)
            else:
                _append_unique(setup, f"{target} = ({field_param.base_type} *)1;", seen)

    return setup


def _lookup_condition_setup(
    shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
) -> list[str]:
    return _lookup_shaper_condition_setup(
        shape,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        LookupSetupOps(_expand_alias_expr, _append_unique, _setup_local_value, _nonmatching_value),
    )


def _lookup_miss_setup(
    exact_shape: LookupShape,
    hit_shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
) -> list[str]:
    return _lookup_shaper_miss_setup(
        exact_shape,
        hit_shape,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        LookupSetupOps(_expand_alias_expr, _append_unique, _setup_local_value, _nonmatching_value),
    )


def _state_switch_candidates(
    body: str,
    source_text: str | None,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _switch_shaper_state_switch_candidates(
        body,
        source_text,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        type_catalog,
        shaping_features,
        StateSwitchOps(
            _infer_lookup_shape,
            _good_path_setup_from_source,
            _alias_pointer_guard_setup,
            _lookup_container_setup,
            _lookup_condition_setup,
            _expand_alias_expr,
            _condition_setup_lines,
            _condition_function_pointer_setup,
            _callee_success_setups_in_block,
            _rewrite_source_alias_exprs,
            _safe_c_name,
        ),
    )


def _fallback_lookup_candidates(
    body: str,
    source_text: str | None,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _lookup_shaper_fallback_lookup_candidates(
        body,
        source_text,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
        type_catalog,
        shaping_features,
        LookupInferOps(_function_decl_map, _function_body, _split_call_args),
        FallbackLookupOps(
            _strip_comments,
            _good_path_setup_from_source,
            _alias_pointer_guard_setup,
            _condition_setup_lines,
            _lookup_container_setup,
            _lookup_condition_setup,
            _lookup_miss_setup,
            _safe_c_name,
        ),
    )


def _is_assignable_expr(expr: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_]\w*(?:->\w+)*", expr.strip()))


def _assign_nonzero_if_lvalue(expr: str) -> list[str]:
    expr = expr.strip()
    if "->" not in expr or not _is_assignable_expr(expr):
        return []
    return [f"if ({expr} == 0) {expr} = 1;"]


def _callee_success_setup_for_call(
    callee: str,
    args: list[str],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
) -> tuple[list[str], list[str]]:
    return _callee_shaper_success_setup_for_call(callee, args, source_text, type_catalog, _callee_success_ops())


def _return_guard_conditions(prefix: str) -> list[str]:
    return _callee_shaper_return_guard_conditions(prefix)


def _invert_simple_return_guard(condition: str, visible_roots: set[str]) -> list[str]:
    return _callee_shaper_invert_simple_return_guard(condition, visible_roots, _append_unique, _nonmatching_value)


def _source_guard_setup_before_call(body: str, call_pos: int, visible_roots: set[str]) -> list[str]:
    return _callee_shaper_source_guard_setup_before_call(body, call_pos, visible_roots, _append_unique, _nonmatching_value)


def _callee_success_candidates(
    body: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    visible_roots: set[str],
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _callee_shaper_success_candidates(
        body,
        source_text,
        type_catalog,
        visible_roots,
        shaping_features,
        _callee_success_ops(),
    )


def _host_to_network_fn(decode_fn: str) -> str:
    return _byte_order_host_to_network_fn(decode_fn)


def _nonmatching_value(value: str) -> str:
    if re.fullmatch(r"0|0x0+", value):
        return "1"
    return "0"


def _source_branch_candidates(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None = None,
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    """
    Generate static source-shaped path candidates from the function body.

    These are not tests yet. They are extra fixture variants that must still
    pass KLEE/EVA/native certification before unit tests are emitted.
    """
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    return _branch_shaper_source_branch_candidates(
        func,
        source_text,
        type_catalog,
        shaping_features,
        BranchShapeOps(
            _source_for_branch_shaping,
            _cast_aliases,
            _decoded_field_aliases,
            _direct_field_aliases,
            _derived_local_aliases,
            _checksum_recompute_lines,
            _alias_pointer_guard_setup,
            _cast_alias_backing_setup,
            _cast_field_expr,
            _host_to_network_fn,
            _nonmatching_value,
            _literal_or_macro_value,
            _safe_c_name,
            _is_void_star,
            _loop_table_candidates,
            _state_switch_candidates,
            _fallback_lookup_candidates,
            _callee_success_candidates,
        ),
    )


def _is_assigns_nothing(assigns: str) -> bool:
    """Check if assigns clause is \\nothing."""
    return "nothing" in assigns or "\\nothing" in assigns


def _bodygen_ops() -> BodyGenOps:
    return BodyGenOps(
        _SCALAR_BOUNDS,
        DEFAULT_SHAPING_FEATURES,
        _scalar_values_from_assumptions,
        _extract_result_value,
        _extract_non_null_params,
        _extract_nonzero_params,
        _extract_null_params,
        _extract_valid_params,
        _is_void_star,
        _pointer_argument_setup,
        _needs_len_data_shape,
        _append_len_data_shape,
        _param_ref_from_arg,
        _function_frees_param,
        _function_takes_param_ownership,
        _function_returns_owned_pointer,
        _lookup_free_fn,
        _assumption_setup_lines,
        _source_for_branch_shaping,
        _void_param_cast_types,
        _unique_name,
        _function_pointer_stub_preamble,
        _function_pointer_stub_name,
        _rewrite_setup_with_param_args,
        _safe_c_name,
    )


def _param_ref_from_arg(arg: str) -> tuple[str, str] | None:
    return _bodygen_param_ref_from_arg(arg)


def _gen_null_setup_body(
    func: CFunction,
    null_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    shaping_features: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    return _bodygen_gen_null_setup_body(
        func,
        null_params,
        behavior,
        source_text,
        type_catalog,
        function_decls,
        shaping_features,
        _bodygen_ops(),
    )


def _append_return_field_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    out_var: str,
    param_args: dict[str, str],
    type_catalog: CTypeCatalog | None,
) -> None:
    _bodygen_append_return_field_outputs(
        lines,
        outputs,
        func,
        out_var,
        param_args,
        type_catalog,
        _SCALAR_BOUNDS,
    )


def _is_observable_scalar_type(type_name: str, type_catalog: CTypeCatalog | None) -> bool:
    return _bodygen_is_observable_scalar_type(type_name, type_catalog, _SCALAR_BOUNDS)


def _field_expr(base_expr: str, access: str, field_name: str) -> str:
    return _bodygen_field_expr(base_expr, access, field_name)


def _append_source_witness_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
) -> None:
    _bodygen_append_source_witness_outputs(
        lines,
        outputs,
        func,
        active_params,
        param_refs,
        source_text,
        type_catalog,
        _bodygen_ops(),
    )


def _gen_valid_setup_body(
    func: CFunction,
    valid_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    extra_setup: list[str] | None = None,
    shaping_features: set[str] | None = None,
    source_shape_oracle: bool = False,
    source_shape_witnesses: bool = False,
) -> tuple[list[str], list[str], list[str], list[str]]:
    return _bodygen_gen_valid_setup_body(
        func,
        valid_params,
        behavior,
        source_text,
        type_catalog,
        function_decls,
        extra_setup,
        shaping_features,
        source_shape_oracle,
        source_shape_witnesses,
        _bodygen_ops(),
    )


def _gen_mixed_test(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    shaping_features: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    return _bodygen_gen_mixed_test(
        func,
        behavior,
        source_text,
        type_catalog,
        function_decls,
        shaping_features,
        _bodygen_ops(),
    )


# ── Main generator ────────────────────────────────────────────────────────────

def generate_yaml_from_header(
    header_path: str,
    source_path: str | None = None,
    include_dir: str | None = None,
    extra_includes: list[str] | None = None,
    extra_sources: list[str] | None = None,
    output_path: str | None = None,
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
) -> str:
    """
    Generate a complete kleva YAML config from a C header with ACSL annotations.

    Unlike `kleva init`, this:
      - Reads ACSL contracts to produce complete body/cleanup/outputs
      - No TODOs — output is ready for `kleva all`
    """
    header_path_obj = Path(header_path)
    module_name = header_path_obj.stem
    src_path = source_path or f"../src/{module_name}.c"
    inc_dir = include_dir or str(header_path_obj.parent)
    out_path = output_path or f"kleva/{module_name}.yaml"
    extra_includes = extra_includes or []
    extra_sources = extra_sources or []
    try:
        shaping_features = normalize_shaping_features(shaping, no_shaping)
    except ValueError as exc:
        print(f"kleva synth: {exc}", file=sys.stderr)
        sys.exit(1)

    header_text = header_path_obj.read_text()

    # Parse header for function declarations
    funcs = parse_header(header_path_obj)

    # Parse ACSL annotations
    from .acsl import parse_acsl
    acsl_specs = parse_acsl(header_path)

    # Read visible declarations/definitions for type and helper-function detection.
    include_roots = [Path(inc_dir), *(Path(p) for p in extra_includes)]
    for suggested in _suggest_extra_sources(header_path_obj, include_roots, src_path):
        if suggested not in extra_sources:
            extra_sources.append(suggested)
    extra_sources = _dedupe_paths(extra_sources)

    visible_text_parts = _collect_visible_headers(header_path_obj, include_roots)
    visible_text_parts.extend(_collect_source_include_headers(src_path, include_roots))
    if not visible_text_parts:
        visible_text_parts = [header_text]
    for candidate in [src_path, *extra_sources]:
        try:
            visible_text_parts.append(Path(candidate).read_text())
        except FileNotFoundError:
            pass
    source_text = "\n".join(visible_text_parts)
    source_include_names = _source_include_names(src_path)
    type_catalog = build_type_catalog(source_text)
    function_decls = _function_decl_map(source_text)

    klee_clang = resolve_klee_clang()
    llvm_link = resolve_llvm_link()
    klee_include = resolve_klee_include()

    # Build the YAML
    lines: list[str] = [
        f"# kleva YAML — auto-synthesized by `kleva synth` from ACSL annotations",
        f"# Headers: {header_path_obj.name}",
        f"# Shaping: {', '.join(sorted(shaping_features)) if shaping_features else 'none'}",
        f"#",
        f"# Usage (from your tests/ directory):",
        f"#   kleva klee {module_name}.yaml --base-dir .",
        f"#   kleva gen  {module_name}.yaml --base-dir .",
        f"#   kleva all  {module_name}.yaml --base-dir .",
        "",
        "module:",
        f"  name:        {module_name}",
        f"  header:      {header_path_obj.name}",
        f"  source:      {src_path}",
        f"  include_dir: {inc_dir}",
    ]

    if extra_includes:
        lines.append("  extra_includes:")
        for inc in extra_includes:
            lines.append(f"    - {inc}")

    if extra_sources:
        lines.append("  extra_sources:")
        for src in extra_sources:
            lines.append(f"    - {src}")

    lines += [
        "",
        "tools:",
        "  ktest_tool:   ktest-tool",
        "  klee:         klee",
        f"  klee_clang:   {klee_clang}",
        f"  llvm_link:    {llvm_link}",
        f"  klee_include: {klee_include}",
        "  framac:       frama-c",
        "",
        "eva:",
        "  precision: 7",
        "  extra_flags:",
        "    - -eva-no-alloc-returns-null",
        "    - -eva-auto-loop-unroll",
        "    - \"20\"",
        "",
        "klee:",
        "  output_base: klee_build",
        "  max_time:    60",
        "  macros:",
        '    - "__assert_fail(e,f,l,fn)=__assert_rtn(fn,f,l,e)"',
        "",
        "output:",
        f"  probe_file: eva/eva_{module_name}_kleva.c",
        f"  unit_file:  unit/test_{module_name}_kleva.c",
        "",
        "functions:",
    ]

    # For each function, generate tests based on ACSL behaviors
    for func in funcs:
        spec = acsl_specs.get(func.name)

        if spec and spec.behaviors:
            lines.append("")
            lines.append(f"  # {'─' * 74}")
            lines.append(f"  # {func.name} ({len(spec.behaviors)} ACSL behaviors)")
            lines.append(f"  # {'─' * 74}")

            for behavior in spec.behaviors:
                test_suffix = behavior.name  # "null", "valid", etc.
                null_params = _extract_null_params(behavior.assumes)
                valid_params = list(dict.fromkeys([
                    *_extract_valid_params(behavior.assumes),
                    *_extract_non_null_params(behavior.assumes),
                ]))

                # Determine the test case name
                test_name = f"{func.name}_{test_suffix}"
                ktest_dir = f"klee_build/klee_out_{test_name}"

                if null_params and not valid_params:
                    # Pure null-guard: generate null body
                    body, outputs, cleanup, preamble = _gen_null_setup_body(
                        func, null_params, behavior, source_text, type_catalog, function_decls, shaping_features
                    )
                elif not null_params:
                    # Valid/scalar-only path: generate a concrete call using
                    # object constructors and scalar assumptions.
                    body, outputs, cleanup, preamble = _gen_valid_setup_body(
                        func, valid_params, behavior, source_text, type_catalog, function_decls, shaping_features=shaping_features
                    )
                else:
                    # Mixed or unknown: handle gracefully
                    body, outputs, cleanup, preamble = _gen_mixed_test(
                        func, behavior, source_text, type_catalog, function_decls, shaping_features
                    )

                lines.extend(_emit_yaml_function(
                    func, behavior, body, outputs, cleanup, ktest_dir, preamble, source_include_names
                ))

            branch_seed: ACSLBehavior | None = None
            branch_seed_valid_params: list[str] = []
            for behavior in spec.behaviors:
                null_params = _extract_null_params(behavior.assumes)
                valid_params = list(dict.fromkeys([
                    *_extract_valid_params(behavior.assumes),
                    *_extract_non_null_params(behavior.assumes),
                ]))
                if null_params or not valid_params:
                    continue
                if branch_seed is None:
                    branch_seed = behavior
                    branch_seed_valid_params = valid_params
                    continue
                current_score = (
                    _extract_result_value(behavior.ensures) is None,
                    len(behavior.assumes),
                )
                best_score = (
                    _extract_result_value(branch_seed.ensures) is None,
                    len(branch_seed.assumes),
                )
                if current_score > best_score:
                    branch_seed = behavior
                    branch_seed_valid_params = valid_params

            if branch_seed is not None:
                candidates = _source_branch_candidates(func, branch_seed, source_text, type_catalog, shaping_features)
                if candidates:
                    lines.append("")
                    lines.append(f"  # {func.name} — source-shaped branch candidates")
                    for candidate in candidates:
                        test_name = f"{func.name}_{candidate.name}"
                        ktest_dir = f"klee_build/klee_out_{test_name}"
                        shaped_behavior = ACSLBehavior(
                            name=candidate.name,
                            assumes=branch_seed.assumes,
                            ensures=branch_seed.ensures,
                            assigns=branch_seed.assigns,
                        )
                        body, outputs, cleanup, preamble = _gen_valid_setup_body(
                            func,
                            branch_seed_valid_params,
                            shaped_behavior,
                            source_text,
                            type_catalog,
                            function_decls,
                            extra_setup=candidate.setup,
                            shaping_features=shaping_features,
                            source_shape_oracle=candidate.oracle,
                            source_shape_witnesses=candidate.witness_outputs,
                        )
                        preamble = [*preamble, *candidate.preamble]
                        lines.extend(_emit_yaml_function(
                            func,
                            shaped_behavior,
                            body,
                            outputs,
                            cleanup,
                            ktest_dir,
                            preamble,
                            source_include_names,
                            candidate=True,
                        ))
        else:
            # No ACSL spec: emit a basic test with just function call
            lines.append("")
            lines.append(f"  # {'─' * 74}")
            lines.append(f"  # {func.name} (no ACSL — basic stub)")
            lines.append(f"  # {'─' * 74}")

            # Generate a simple null-guard test only when the source has a
            # recognizable null guard. A pointer parameter alone is not a
            # promise that NULL is a valid input.
            pointer_params = [p for p in func.params if p.is_pointer]
            nullable_params = [
                p for p in pointer_params
                if _function_accepts_null_param(source_text, func.name, p.name)
            ]
            if nullable_params:
                # Null test for first pointer
                np = nullable_params[0]
                body, outputs, cleanup, preamble = _gen_null_setup_body(
                    func, [np.name],
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    source_text,
                    type_catalog,
                    function_decls,
                    shaping_features,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    body, outputs, cleanup,
                    f"klee_build/klee_out_{func.name}_null",
                    preamble,
                    source_include_names,
                ))

            # Valid test with constructors for all pointer params
            if func.params:
                valid_names = [p.name for p in func.params if p.is_pointer and p.base_type != "char"]
                body, outputs, cleanup, preamble = _gen_valid_setup_body(
                    func, valid_names or ([] if not pointer_params else [pointer_params[0].name]),
                    ACSLBehavior(name="valid", assumes=[]),
                    source_text,
                    type_catalog,
                    function_decls,
                    shaping_features=shaping_features,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="valid", assumes=[]),
                    body, outputs, cleanup,
                    f"klee_build/klee_out_{func.name}_valid",
                    preamble,
                    source_include_names,
                ))

    return "\n".join(lines) + "\n"


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_synth(
    header: str,
    source: str | None = None,
    include_dir: str | None = None,
    out: str | None = None,
    extra_includes: list[str] | None = None,
    extra_sources: list[str] | None = None,
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
) -> None:
    """
    `kleva synth` entry point: generate YAML from header + ACSL.
    """
    header_path = Path(header)
    if not header_path.exists():
        print(f"kleva synth: header not found: {header_path}", file=sys.stderr)
        sys.exit(1)

    module_name = header_path.stem
    src_path = source or f"../src/{module_name}.c"
    inc_dir = include_dir or str(header_path.parent)
    out_path = out or f"kleva/{module_name}.yaml"

    # Parse header for display
    funcs = parse_header(header_path)
    print(f"kleva synth: found {len(funcs)} function(s) in {header_path.name}", file=sys.stderr)
    for f in funcs:
        print(f"  {f.return_type} {f.name}(...)", file=sys.stderr)

    # Parse ACSL
    from .acsl import parse_acsl
    acsl_specs = parse_acsl(header_path)
    acsl_count = sum(1 for s in acsl_specs.values() if s.behaviors)
    if acsl_count:
        print(f"kleva synth: found ACSL contracts for {acsl_count}/{len(funcs)} function(s)", file=sys.stderr)

    yaml_text = generate_yaml_from_header(
        header_path=str(header_path),
        source_path=src_path,
        include_dir=inc_dir,
        extra_includes=extra_includes or [],
        extra_sources=extra_sources or [],
        output_path=out_path,
        shaping=shaping,
        no_shaping=no_shaping,
    )

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(yaml_text)
    print(f"kleva synth: wrote {out_file}", file=sys.stderr)
    print(f"Next: kleva all {module_name}.yaml --base-dir .", file=sys.stderr)
