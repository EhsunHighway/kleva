from __future__ import annotations

import re

from .acsl import ACSLBehavior
from .acsl_contract import (
    extract_non_null_params as _extract_non_null_params,
    extract_nonzero_params as _extract_nonzero_params,
    extract_null_params as _extract_null_params,
    extract_result_value as _extract_result_value,
    extract_valid_params as _extract_valid_params,
    scalar_values_from_assumptions as _scalar_values_from_assumptions,
)
from .ast.model import CFunction, CParam, CTypeCatalog, DerivedLocal
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
from .compat.source_fallbacks import fallback_function_accepts_null_param as _fallback_function_accepts_null_param
from .compat.source_fallbacks import fallback_function_body as _fallback_function_body
from .compat.source_fallbacks import fallback_function_decl_map as _fallback_function_decl_map
from .compat.source_fallbacks import fallback_function_definition_body as _fallback_function_definition_body
from .compat.source_fallbacks import fallback_function_frees_param as _fallback_function_frees_param
from .compat.source_fallbacks import fallback_function_returns_owned_pointer as _fallback_function_returns_owned_pointer
from .compat.source_fallbacks import fallback_function_takes_param_ownership as _fallback_function_takes_param_ownership
from .compat.source_fallbacks import fallback_split_call_args as _fallback_split_call_args
from .compat.source_fallbacks import fallback_strip_comments as _fallback_strip_comments
from .fixtures.buffers import (
    append_len_data_shape as _buffer_fixture_append_len_data_shape,
    needs_len_data_shape as _buffer_fixture_needs_len_data_shape,
    struct_has_fields as _buffer_fixture_struct_has_fields,
)
from .fixtures.construction import (
    function_pointer_stub_name as _function_pointer_stub_name,
    function_pointer_stub_preamble as _function_pointer_stub_preamble,
    is_void_star as _is_void_star,
    lookup_free_fn as _lookup_free_fn,
    pointer_argument_setup as _pointer_argument_setup,
    safe_c_name as _safe_c_name,
    unique_name as _unique_name,
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
    condition_setup_lines as _condition_shaper_setup_lines,
    rewrite_result_expr as _condition_shaper_rewrite_result_expr,
    rewrite_source_alias_exprs as _condition_shaper_rewrite_source_alias_exprs,
    split_conjuncts as _condition_shaper_split_conjuncts,
    strip_outer_parens as _condition_shaper_strip_outer_parens,
)
from .shaping.ir_conditions import (
    IrConditionOps,
    condition_candidates_from_ir as _ir_condition_candidates,
)
from .shaping.ir_byte_order import decoded_field_aliases_from_ir as _ir_decoded_field_aliases
from .shaping.ir_callbacks import callback_candidates_from_ir as _ir_callback_candidates
from .shaping.ir_callees import callee_candidates_from_ir as _ir_callee_candidates
from .shaping.ir_ownership import classify_ownership_from_ir as _ir_classify_ownership
from .shaping.ir_parsers import (
    HelperCallRule,
    IrParserOps,
    parser_candidates_from_ir as _ir_parser_candidates,
)
from .shaping.ir_lookups import fallback_lookup_candidates_from_ir as _ir_lookup_candidates
from .shaping.ir_tables import table_candidates_from_ir as _ir_table_candidates
from .shaping.lookups import (
    FallbackLookupOps,
    LookupFixtureOps,
    LookupInferOps,
    LookupSetupOps,
    LookupShape,
    alias_pointer_guard_setup as _lookup_shaper_alias_pointer_guard_setup,
    fallback_lookup_candidates as _lookup_shaper_fallback_lookup_candidates,
    infer_lookup_shape as _lookup_shaper_infer_lookup_shape,
    infer_lookup_shape_for_call as _lookup_shaper_infer_lookup_shape_for_call,
    lookup_condition_setup as _lookup_shaper_condition_setup,
    lookup_container_setup as _lookup_shaper_container_setup,
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
from .shaping.ir_switches import state_switch_candidates_from_ir as _ir_state_switch_candidates
from .shaping.tables import (
    TableShapeOps,
    loop_table_candidates as _table_shaper_loop_table_candidates,
)
from .synth_config import DEFAULT_SHAPING_FEATURES, SCALAR_BOUNDS


_SCALAR_BOUNDS = SCALAR_BOUNDS

def _struct_has_fields(type_catalog: CTypeCatalog | None, type_name: str, fields: set[str]) -> bool:
    return _buffer_fixture_struct_has_fields(type_catalog, type_name, fields)


def _needs_len_data_shape(
    func_name: str,
    param_name: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    param: CParam,
) -> bool:
    return _buffer_fixture_needs_len_data_shape(
        func_name,
        param_name,
        source_text,
        type_catalog,
        param,
        _source_for_branch_shaping,
    )


def _append_len_data_shape(lines: list[str], arg: str) -> None:
    _buffer_fixture_append_len_data_shape(lines, arg)


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
    return _fallback_function_body(source_text, func_name)


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
            if arg.startswith("&") and re.fullmatch(r"&[A-Za-z_]\w*", arg):
                obj = arg[1:]
                new_line = re.sub(
                    rf"\b{re.escape(name)}->",
                    f"{obj}.",
                    new_line,
                )
                new_line = re.sub(rf"(?<![&\w]){re.escape(name)}\b(?!\.)", arg, new_line)
                continue
            new_line = re.sub(
                rf"\b{re.escape(name)}->",
                f"({arg})->",
                new_line,
            )
            new_line = re.sub(rf"(?<![&\w]){re.escape(name)}\b(?!\.)", arg, new_line)
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
        LookupInferOps(_fallback_function_decl_map, _fallback_function_body, _fallback_split_call_args),
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
        LookupInferOps(_fallback_function_decl_map, _fallback_function_body, _fallback_split_call_args),
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


def _callee_success_ops() -> CalleeSuccessOps:
    return CalleeSuccessOps(
        _fallback_function_decl_map,
        _fallback_function_definition_body,
        _fallback_split_call_args,
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


def _lookup_fixture_ops() -> LookupFixtureOps:
    return LookupFixtureOps(
        _expand_alias_expr,
        _cast_alias_backing_setup,
        _cast_field_expr,
        _append_unique,
        _safe_c_name,
    )


def _lookup_container_setup(
    shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
) -> list[str]:
    return _lookup_shaper_container_setup(shape, aliases, type_catalog, _lookup_fixture_ops())


def _alias_pointer_guard_setup(
    body: str,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
    skip_exprs: set[str] | None = None,
) -> list[str]:
    return _lookup_shaper_alias_pointer_guard_setup(
        body,
        aliases,
        type_catalog,
        skip_exprs,
        _lookup_fixture_ops(),
    )


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
        LookupInferOps(_fallback_function_decl_map, _fallback_function_body, _fallback_split_call_args),
        FallbackLookupOps(
            _fallback_strip_comments,
            _expand_alias_expr,
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


def _ir_condition_shape_candidates(function_ir) -> list[BranchCandidate]:
    return _ir_condition_candidates(
        function_ir,
        _ir_condition_ops(function_ir),
    )


def _ir_condition_ops(function_ir) -> IrConditionOps:
    return IrConditionOps(_safe_c_name, _nonmatching_value, _ir_decoded_field_aliases(function_ir), _host_to_network_fn)


def _ir_state_switch_shape_candidates(
    function_ir,
    helper_irs: dict | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    return _ir_state_switch_candidates(function_ir, _ir_condition_ops(function_ir), helper_irs, helper_params)


def _ir_callback_shape_candidates(function_ir, func: CFunction, type_catalog: CTypeCatalog | None) -> list[BranchCandidate]:
    return _ir_callback_candidates(
        function_ir,
        func,
        type_catalog,
        _function_pointer_stub_preamble,
        _function_pointer_stub_name,
    )


def _ir_callee_shape_candidates(
    function_ir,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    helper_irs: dict | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    def setup_for_call(callee: str, args: list[str]) -> tuple[list[str], list[str]]:
        return _callee_success_setup_for_call(callee, args, source_text, type_catalog)

    return _ir_callee_candidates(function_ir, setup_for_call, helper_irs, helper_params)


def _ir_parser_shape_candidates(
    function_ir,
    helper_call_rules: tuple[HelperCallRule, ...] = (),
    helper_irs: dict | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    return _ir_parser_candidates(function_ir, IrParserOps(_safe_c_name, helper_call_rules, helper_irs, helper_params))


def _ir_table_shape_candidates(function_ir) -> list[BranchCandidate]:
    return _ir_table_candidates(function_ir)


def _ir_lookup_shape_candidates(
    function_ir,
    helper_irs: dict | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    return _ir_lookup_candidates(function_ir, _ir_condition_ops(function_ir), helper_irs, helper_params)


def _ownership_summary_from_ir(func: CFunction, function_ir):
    if function_ir is None:
        return None
    return _ir_classify_ownership(
        function_ir,
        {p.name for p in func.params if p.is_pointer},
        void_param_names={p.name for p in func.params if _is_void_star(p)},
    )


def _source_branch_candidates(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None = None,
    shaping_features: set[str] | None = None,
    function_ir = None,
    helper_call_rules: tuple[HelperCallRule, ...] = (),
    helper_irs: dict | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    """
    Generate static implementation-shaped path candidates.

    These are not tests yet. They are extra fixture variants that must still
    pass KLEE/EVA/native certification before unit tests are emitted.
    """
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)

    def ir_callee_candidates(function_ir, source_text, type_catalog):
        return _ir_callee_shape_candidates(function_ir, source_text, type_catalog, helper_irs, helper_params)

    def ir_parser_candidates(function_ir):
        return _ir_parser_shape_candidates(function_ir, helper_call_rules, helper_irs, helper_params)

    def ir_state_switch_candidates(function_ir):
        return _ir_state_switch_shape_candidates(function_ir, helper_irs, helper_params)

    def ir_lookup_candidates(function_ir):
        return _ir_lookup_shape_candidates(function_ir, helper_irs, helper_params)

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
            _ir_condition_shape_candidates,
            _ir_callback_shape_candidates,
            ir_callee_candidates,
            ir_parser_candidates,
            _ir_table_shape_candidates,
            _loop_table_candidates,
            _state_switch_candidates,
            ir_state_switch_candidates,
            _fallback_lookup_candidates,
            _callee_success_candidates,
            ir_lookup_candidates,
        ),
        function_ir,
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
        _fallback_function_frees_param,
        _fallback_function_takes_param_ownership,
        _fallback_function_accepts_null_param,
        _fallback_function_returns_owned_pointer,
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
    function_ir = None,
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
        _ownership_summary_from_ir(func, function_ir),
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
    function_ir = None,
    object_paths = None,
    call_arg_overrides = None,
    witness_setup = None,
    extra_outputs = None,
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
        _ownership_summary_from_ir(func, function_ir),
        object_paths,
        call_arg_overrides,
        witness_setup,
        extra_outputs,
    )


def _gen_mixed_test(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    shaping_features: set[str] | None = None,
    function_ir = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    return _bodygen_gen_mixed_test(
        func,
        behavior,
        source_text,
        type_catalog,
        function_decls,
        shaping_features,
        _bodygen_ops(),
        _ownership_summary_from_ir(func, function_ir),
    )
