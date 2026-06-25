from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Tuple

from .acsl import ACSLBehavior
from .ast.model import CFunction, CParam, CTypeCatalog


BodyResult = Tuple[List[str], List[str], List[str], List[str]]


@dataclass(frozen=True)
class BodyGenOps:
    scalar_bounds: dict[str, tuple[int, int]]
    default_shaping_features: frozenset[str]
    scalar_values_from_assumptions: Callable[[list[str]], dict[str, str]]
    extract_result_value: Callable[[list[str]], int | None]
    extract_non_null_params: Callable[[list[str]], list[str]]
    extract_nonzero_params: Callable[[list[str]], list[str]]
    extract_null_params: Callable[[list[str]], list[str]]
    extract_valid_params: Callable[[list[str]], list[str]]
    is_void_star: Callable[[CParam], bool]
    pointer_argument_setup: Callable[..., tuple[list[str], str, list[str]]]
    needs_len_data_shape: Callable[..., bool]
    append_len_data_shape: Callable[[list[str], str], None]
    param_ref_from_arg: Callable[[str], tuple[str, str] | None]
    function_frees_param: Callable[[str | None, str, str], bool]
    function_takes_param_ownership: Callable[[str | None, str, str], bool]
    function_returns_owned_pointer: Callable[[CFunction], bool]
    lookup_free_fn: Callable[[str, str | None], str | None]
    assumption_setup_lines: Callable[..., list[str]]
    source_for_branch_shaping: Callable[[str | None, str], str]
    void_param_cast_types: Callable[[str, CFunction], dict[str, str]]
    unique_name: Callable[[str, set[str]], str]
    function_pointer_stub_preamble: Callable[..., list[str]]
    function_pointer_stub_name: Callable[[str], str]
    rewrite_setup_with_param_args: Callable[[list[str], dict[str, str]], list[str]]
    safe_c_name: Callable[[str], str]


def param_ref_from_arg(arg: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"&([A-Za-z_]\w*)", arg)
    if m:
        return (m.group(1), ".")
    if arg != "NULL" and re.fullmatch(r"[A-Za-z_]\w*", arg):
        return (arg, "->")
    return None


def append_return_field_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    out_var: str,
    param_args: dict[str, str],
    type_catalog: CTypeCatalog | None,
    scalar_bounds: dict[str, tuple[int, int]],
) -> None:
    if not type_catalog or not type_catalog.is_complete_struct(func.return_base):
        return

    for field_name, field_param in type_catalog.struct_fields.get(func.return_base, {}).items():
        if field_name not in param_args:
            continue
        arg = param_args[field_name]
        out_name = f"out_{field_name}"
        if field_param.is_pointer or type_catalog.function_pointer(field_param.base_type):
            lines.append(f"int {out_name}_same = ({out_var} != NULL && {out_var}->{field_name} == {arg});")
            outputs.append(f"{out_name}_same")
        elif field_param.base_type in scalar_bounds or field_param.base_type in ("EventType", "uint64_t", "size_t"):
            if field_param.base_type in scalar_bounds or field_param.base_type in ("uint64_t", "size_t"):
                lines.append(f"{field_param.base_type} {out_name} = {out_var} ? {out_var}->{field_name} : 0;")
                outputs.append(out_name)
            else:
                lines.append(f"int {out_name}_same = ({out_var} != NULL && {out_var}->{field_name} == {arg});")
                outputs.append(f"{out_name}_same")


def is_observable_scalar_type(
    type_name: str,
    type_catalog: CTypeCatalog | None,
    scalar_bounds: dict[str, tuple[int, int]],
) -> bool:
    if type_name in scalar_bounds or type_name in ("uint64_t", "size_t", "EventType"):
        return True
    if type_catalog and type_catalog.is_complete_struct(type_name):
        return False
    if type_catalog and type_catalog.function_pointer(type_name):
        return False
    return bool(re.fullmatch(r"[A-Za-z_]\w*", type_name))


def field_expr(base_expr: str, access: str, field_name: str) -> str:
    return f"{base_expr}{access}{field_name}"


def append_source_witness_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: BodyGenOps,
) -> None:
    """
    Add generic post-call witnesses for mutable pointer parameters.

    These are intentionally structural rather than domain-specific. For a
    source-shaped candidate, fields on complete structs are often the clearest
    evidence that the intended side effect happened. KLEVA records shallow
    scalar/pointer fields, plus scalar/pointer fields one nested struct deep.
    """
    if not type_catalog:
        return

    seen: set[str] = set(outputs)
    for p in func.params:
        if not p.is_pointer or p.is_const or ops.is_void_star(p):
            continue
        if p.name not in active_params or p.name not in param_refs:
            continue
        if ops.function_frees_param(source_text, func.name, p.name):
            continue
        if not type_catalog.is_complete_struct(p.base_type):
            continue

        base_expr, access = param_refs[p.name]
        for field_name, field_param in type_catalog.struct_fields.get(p.base_type, {}).items():
            out_name = ops.safe_c_name(f"out_{p.name}_{field_name}")
            expr = field_expr(base_expr, access, field_name)

            if field_param.is_pointer or type_catalog.function_pointer(field_param.base_type):
                if out_name not in seen:
                    lines.append(f"int {out_name}_nonnull = ({expr} != NULL);")
                    outputs.append(f"{out_name}_nonnull")
                    seen.add(out_name)
                continue

            if is_observable_scalar_type(field_param.base_type, type_catalog, ops.scalar_bounds):
                if out_name not in seen:
                    c_type = field_param.base_type if field_param.base_type in ops.scalar_bounds else "int"
                    lines.append(f"{c_type} {out_name} = {expr};")
                    outputs.append(out_name)
                    seen.add(out_name)
                continue

            if "[" in field_param.raw_type or not type_catalog.is_complete_struct(field_param.base_type):
                continue

            nested_access = f"{access}{field_name}."
            for nested_name, nested_param in type_catalog.struct_fields.get(field_param.base_type, {}).items():
                nested_out = ops.safe_c_name(f"out_{p.name}_{field_name}_{nested_name}")
                nested_expr = field_expr(base_expr, nested_access, nested_name)
                if nested_param.is_pointer or type_catalog.function_pointer(nested_param.base_type):
                    if nested_out not in seen:
                        lines.append(f"int {nested_out}_nonnull = ({nested_expr} != NULL);")
                        outputs.append(f"{nested_out}_nonnull")
                        seen.add(nested_out)
                    continue
                if is_observable_scalar_type(nested_param.base_type, type_catalog, ops.scalar_bounds) and nested_out not in seen:
                    c_type = nested_param.base_type if nested_param.base_type in ops.scalar_bounds else "int"
                    lines.append(f"{c_type} {nested_out} = {nested_expr};")
                    outputs.append(nested_out)
                    seen.add(nested_out)


def gen_null_setup_body(
    func: CFunction,
    null_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    shaping_features: set[str] | None,
    ops: BodyGenOps,
) -> BodyResult:
    """
    Generate (body_lines, output_vars, cleanup_lines) for a null-guard test.

    For each function parameter that isn't the null param under test,
    provide a concrete value (constructor for pointers, scalar for others).
    `void *` params are always passed as NULL.
    """
    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)

    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif ops.is_void_star(p):
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if ops.needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not ops.function_frees_param(source_text, func.name, p.name) and not ops.function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in enabled_features and type_catalog.function_pointer(p.base_type):
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name, str(lo))
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(ops.assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

    args_str = ", ".join(call_args)
    result_val = ops.extract_result_value(behavior.ensures)

    if func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        outputs.append(out_var)
        if result_val is not None:
            sentinel = "out_sentinel"
            lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
            outputs.append(sentinel)
    else:
        lines.append(f"{func.name}({args_str});")
        out_var = "out_ok"
        lines.append(f"int {out_var} = 1;")
        outputs.append(out_var)

    return lines, outputs, cleanup, preamble


def gen_valid_setup_body(
    func: CFunction,
    valid_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    extra_setup: list[str] | None,
    shaping_features: set[str] | None,
    source_shape_oracle: bool,
    source_shape_witnesses: bool,
    ops: BodyGenOps,
) -> BodyResult:
    """
    Generate (body_lines, output_vars, cleanup_lines) for a valid-path test.

    Creates proper objects for pointer parameters, uses symbolic scalars.
    """
    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)
    void_cast_types = ops.void_param_cast_types(ops.source_for_branch_shaping(source_text, func.name), func)
    non_null_params = set(ops.extract_non_null_params(behavior.assumes))
    nonzero_params = set(ops.extract_nonzero_params(behavior.assumes))
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)
    object_params = set(valid_params) | non_null_params

    for p in func.params:
        if p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif ops.is_void_star(p):
            cast_type = void_cast_types.get(p.name)
            if p.name in object_params and cast_type and type_catalog and type_catalog.is_complete_struct(cast_type):
                var_name = ops.unique_name(f"{p.name}_{cast_type}", used_names)
                lines.append(f"{cast_type} {var_name};")
                lines.append(f"memset(&{var_name}, 0, sizeof({var_name}));")
                call_args.append(f"&{var_name}")
                param_args[p.name] = f"&{var_name}"
                param_refs[p.name] = (var_name, ".")
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.is_pointer and p.name in valid_params:
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if ops.needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not ops.function_frees_param(source_text, func.name, p.name) and not ops.function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif p.is_pointer:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif type_catalog and "function-pointers" in enabled_features and (fp_decl := type_catalog.function_pointer(p.base_type)):
            preamble.extend(ops.function_pointer_stub_preamble(fp_decl))
            stub_name = ops.function_pointer_stub_name(fp_decl.name)
            call_args.append(stub_name)
            param_args[p.name] = stub_name
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(ops.assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))
    if extra_setup:
        lines.extend(ops.rewrite_setup_with_param_args(extra_setup, param_args))

    args_str = ", ".join(call_args)

    if source_shape_oracle:
        lines.append(f"{func.name}({args_str});")
        if source_shape_witnesses:
            append_source_witness_outputs(
                lines,
                outputs,
                func,
                active_params,
                param_refs,
                source_text,
                type_catalog,
                ops,
            )
        if not outputs:
            out_var = "out_ok"
            lines.append(f"int {out_var} = 1;")
            outputs.append(out_var)
    elif func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        if func.return_is_pointer:
            nonnull_var = f"{out_var}_nonnull"
            lines.append(f"int {nonnull_var} = ({out_var} != NULL);")
            outputs.append(nonnull_var)
            append_return_field_outputs(lines, outputs, func, out_var, param_args, type_catalog, ops.scalar_bounds)
            if ops.function_returns_owned_pointer(func):
                free_fn = ops.lookup_free_fn(func.return_base, source_text)
                if free_fn:
                    cleanup.insert(0, f"if ({out_var}) {free_fn}({out_var});")
        else:
            outputs.append(out_var)
        result_val = ops.extract_result_value(behavior.ensures)
        if result_val is not None:
            sentinel = "out_sentinel"
            lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
            outputs.append(sentinel)
    else:
        lines.append(f"{func.name}({args_str});")
        out_var = "out_ok"
        lines.append(f"int {out_var} = 1;")
        outputs.append(out_var)

    return lines, outputs, cleanup, preamble


def gen_mixed_test(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    shaping_features: set[str] | None,
    ops: BodyGenOps,
) -> BodyResult:
    """
    Generate body for a mixed behavior where some params are null
    and some are valid. Common for functions with multiple pointer params
    where the contract covers all-null or specific combos.
    """
    null_params = ops.extract_null_params(behavior.assumes)
    valid_params = ops.extract_valid_params(behavior.assumes)

    if null_params and not valid_params:
        return gen_null_setup_body(
            func, null_params, behavior, source_text, type_catalog, function_decls, shaping_features, ops
        )

    if valid_params and not null_params:
        return gen_valid_setup_body(
            func, valid_params, behavior, source_text, type_catalog, function_decls, None, shaping_features, False, False, ops
        )

    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)
    non_null_params = set(ops.extract_non_null_params(behavior.assumes))
    nonzero_params = set(ops.extract_nonzero_params(behavior.assumes))
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if ops.needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not ops.function_frees_param(source_text, func.name, p.name) and not ops.function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in enabled_features and (fp_decl := type_catalog.function_pointer(p.base_type)):
            if p.name in non_null_params:
                preamble.extend(ops.function_pointer_stub_preamble(fp_decl))
                stub_name = ops.function_pointer_stub_name(fp_decl.name)
                call_args.append(stub_name)
                param_args[p.name] = stub_name
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(ops.assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

    args_str = ", ".join(call_args)

    if func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        outputs.append(out_var)
    else:
        lines.append(f"{func.name}({args_str});")
        out_var = "out_ok"
        lines.append(f"int {out_var} = 1;")
        outputs.append(out_var)

    return lines, outputs, cleanup, preamble
