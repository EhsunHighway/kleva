from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CTypeCatalog, DerivedLocal
from .candidates import BranchCandidate


@dataclass
class LookupShape:
    callee:         str
    result_var:     str
    element_type:   str
    element_alias:  str
    container_type: str
    container_expr: str
    array_field:    str
    param_args:     dict[str, str]
    conditions:     list[str]


@dataclass(frozen=True)
class LookupInferOps:
    function_decl_map: Callable[[str], dict]
    function_body:     Callable[[str, str], str]
    split_call_args:   Callable[[str], list[str]]


@dataclass(frozen=True)
class LookupSetupOps:
    expand_alias_expr: Callable[[str, dict[str, tuple[str, str]]], str]
    append_unique:     Callable[[list[str], str, set[str]], None]
    setup_local_value: Callable[
        [str, str, dict[str, tuple[str, str]], dict[str, tuple[str, str, str]], dict[str, tuple[str, str]], dict[str, DerivedLocal]],
        list[str],
    ]
    nonmatching_value: Callable[[str], str]


@dataclass(frozen=True)
class LookupFixtureOps:
    expand_alias_expr:        Callable[[str, dict[str, tuple[str, str]]], str]
    cast_alias_backing_setup: Callable[[str, str, str, dict], list[str]]
    cast_field_expr:          Callable[[str, str, str], str]
    append_unique:            Callable[[list[str], str, set[str]], None]
    safe_c_name:              Callable[[str], str]


@dataclass(frozen=True)
class FallbackLookupOps:
    strip_comments:                Callable[[str], str]
    good_path_setup_from_source:    Callable[..., list[str]]
    alias_pointer_guard_setup:      Callable[..., list[str]]
    condition_setup_lines:          Callable[..., list[str]]
    lookup_container_setup:         Callable[..., list[str]]
    lookup_condition_setup:         Callable[..., list[str]]
    lookup_miss_setup:              Callable[..., list[str]]
    safe_c_name:                    Callable[[str], str]


def infer_lookup_shape(
    caller_body: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: LookupInferOps,
) -> list[LookupShape]:
    if not source_text or not type_catalog:
        return []

    shapes: list[LookupShape] = []
    function_decls = ops.function_decl_map(source_text)

    call_pat = re.compile(
        r"\b([A-Za-z_]\w*)\s*\*\s*([A-Za-z_]\w*)\s*=\s*"
        r"([A-Za-z_]\w*)\s*\(([^;]*)\)\s*;"
    )
    for m in call_pat.finditer(caller_body):
        result_type, result_var, callee, args_raw = m.groups()
        if not re.search(rf"\bswitch\s*\(\s*{re.escape(result_var)}->\w+\s*\)", caller_body):
            continue

        shape = infer_lookup_shape_for_call(callee, result_var, args_raw, source_text, type_catalog, ops)
        if shape and shape.element_type == result_type:
            shapes.append(shape)

    return shapes


def infer_lookup_shape_for_call(
    callee: str,
    result_var: str,
    args_raw: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: LookupInferOps,
) -> LookupShape | None:
    if not source_text or not type_catalog:
        return None

    function_decls = ops.function_decl_map(source_text)
    callee_body = ops.function_body(source_text, callee)
    callee_decl = function_decls.get(callee)
    if not callee_body or not callee_decl:
        return None

    args = ops.split_call_args(args_raw)
    if len(args) != len(callee_decl.params):
        return None
    param_args = {p.name: a for p, a in zip(callee_decl.params, args)}

    return_type = callee_decl.return_base
    alias_pat = re.compile(
        r"\b([A-Za-z_]\w*)\s*\*\s*([A-Za-z_]\w*)\s*=\s*&"
        r"\s*([A-Za-z_]\w*)->([A-Za-z_]\w*)\s*\[\s*[A-Za-z_]\w*\s*\]\s*;"
    )
    for m in alias_pat.finditer(callee_body):
        element_type, element_alias, container_param, array_field = m.groups()
        if element_type != return_type:
            continue
        if not re.search(rf"\breturn\s+{re.escape(element_alias)}\s*;", callee_body):
            continue
        container_decl = next((p for p in callee_decl.params if p.name == container_param), None)
        if not container_decl:
            continue
        container_expr = param_args.get(container_param)
        if not container_expr:
            continue

        conditions = [
            cond.strip()
            for cond in re.findall(rf"{re.escape(element_alias)}->[^{{;]+", callee_body)
        ]
        return LookupShape(
            callee=callee,
            result_var=result_var,
            element_type=element_type,
            element_alias=element_alias,
            container_type=container_decl.base_type,
            container_expr=container_expr,
            array_field=array_field,
            param_args=param_args,
            conditions=conditions,
        )

    return None


def lookup_condition_setup(
    shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    ops: LookupSetupOps,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    container_expr = ops.expand_alias_expr(shape.container_expr, aliases)
    elem = f"{container_expr}->{shape.array_field}[0]"

    for cond in shape.conditions:
        for field, rhs in re.findall(
            rf"{re.escape(shape.element_alias)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            cond,
        ):
            value = "1"
            if rhs in shape.param_args:
                arg = shape.param_args[rhs]
                ops.append_unique(setup, f"{elem}.{field} = {value};", seen)
                for line in ops.setup_local_value(arg, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                    ops.append_unique(setup, line, seen)
            else:
                ops.append_unique(setup, f"{elem}.{field} = {rhs};", seen)

        for field in re.findall(
            rf"{re.escape(shape.element_alias)}->(\w+)(?:\s*&&|\s*\)|\s*$)",
            cond,
        ):
            ops.append_unique(setup, f"{elem}.{field} = 1;", seen)

        for field, rhs in re.findall(
            rf"{re.escape(shape.element_alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            cond,
        ):
            ops.append_unique(setup, f"{elem}.{field} = {ops.nonmatching_value(rhs)};", seen)

    return setup


def lookup_miss_setup(
    exact_shape: LookupShape,
    hit_shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    ops: LookupSetupOps,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    container_expr = ops.expand_alias_expr(exact_shape.container_expr, aliases)
    elem = f"{container_expr}->{exact_shape.array_field}[0]"
    hit_conditions = "\n".join(hit_shape.conditions)

    for cond in exact_shape.conditions:
        for field, rhs in re.findall(
            rf"{re.escape(exact_shape.element_alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            cond,
        ):
            if re.search(rf"{re.escape(hit_shape.element_alias)}->{field}\s*!=", hit_conditions):
                continue
            value = rhs
            if rhs in exact_shape.param_args:
                value = exact_shape.param_args[rhs]
            ops.append_unique(setup, f"{elem}.{field} = {value};", seen)
            return setup

    for cond in exact_shape.conditions:
        for field, rhs in re.findall(
            rf"{re.escape(exact_shape.element_alias)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            cond,
        ):
            if re.search(rf"{re.escape(hit_shape.element_alias)}->{field}\s*==", hit_conditions):
                continue
            if rhs in exact_shape.param_args:
                arg = exact_shape.param_args[rhs]
                ops.append_unique(setup, f"{elem}.{field} = 0;", seen)
                for line in ops.setup_local_value(arg, "1", aliases, decoded_aliases, direct_aliases, derived_aliases):
                    ops.append_unique(setup, line, seen)
            else:
                ops.append_unique(setup, f"{elem}.{field} = {ops.nonmatching_value(rhs)};", seen)
            return setup

    return setup


def lookup_container_setup(
    shape: LookupShape,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
    ops: LookupFixtureOps,
) -> list[str]:
    raw_expr = shape.container_expr.strip()
    expr = ops.expand_alias_expr(raw_expr, aliases)
    storage = ops.safe_c_name(f"kleva_{shape.result_var}_{shape.array_field}_owner")
    setup = [
        f"{shape.container_type} {storage};",
        f"memset(&{storage}, 0, sizeof({storage}));",
    ]

    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", raw_expr)
    if m and m.group(1) in aliases:
        alias, field = m.groups()
        cast_type, cast_expr = aliases[alias]
        backing = ops.cast_alias_backing_setup(alias, cast_type, cast_expr, {})
        setup.extend(backing)
        setup.append(f"{ops.cast_field_expr(cast_type, cast_expr, field)} = &{storage};")
        return setup

    if re.fullmatch(r"[A-Za-z_]\w*", expr):
        setup.append(f"{expr} = &{storage};")
    return setup


def alias_pointer_guard_setup(
    body: str,
    aliases: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog,
    skip_exprs: set[str] | None,
    ops: LookupFixtureOps,
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

            target = ops.cast_field_expr(cast_type, cast_expr, field)
            if type_catalog.is_complete_struct(field_param.base_type):
                storage = ops.safe_c_name(f"kleva_{alias}_{field}_guard")
                ops.append_unique(setup, f"{field_param.base_type} {storage};", seen)
                ops.append_unique(setup, f"memset(&{storage}, 0, sizeof({storage}));", seen)
                ops.append_unique(setup, f"{target} = &{storage};", seen)
            else:
                ops.append_unique(setup, f"{target} = ({field_param.base_type} *)1;", seen)

    return setup


def fallback_lookup_candidates(
    body: str,
    source_text: str | None,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str],
    infer_ops: LookupInferOps,
    fallback_ops: FallbackLookupOps,
) -> list[BranchCandidate]:
    if "fallback-lookups" not in shaping_features or not type_catalog:
        return []

    candidates: list[BranchCandidate] = []
    seen: set[str] = set()
    match_body = fallback_ops.strip_comments(body)
    pattern = re.compile(
        r"\bif\s*\(\s*!\s*([A-Za-z_]\w*)\s*&&\s*([^)]*)\)\s*"
        r"\{\s*([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(([^;{}]*)\)\s*;\s*\}"
        r"\s*if\s*\(\s*\3\s*\)",
        flags=re.DOTALL,
    )

    for m in pattern.finditer(match_body):
        exact_var, condition, fallback_var, callee, args_raw = m.groups()
        shape = infer_lookup_shape_for_call(callee, fallback_var, args_raw, source_text, type_catalog, infer_ops)
        if not shape:
            continue

        setup: list[str] = []
        setup.extend(fallback_ops.good_path_setup_from_source(body, aliases, decoded_aliases, direct_aliases, derived_aliases))
        setup.extend(fallback_ops.alias_pointer_guard_setup(body, aliases, type_catalog, {shape.container_expr}))
        setup.extend(fallback_ops.condition_setup_lines(
            condition,
            aliases,
            decoded_aliases,
            direct_aliases,
            derived_aliases,
        ))
        setup.extend(fallback_ops.lookup_container_setup(shape, aliases, type_catalog))
        setup.extend(fallback_ops.lookup_condition_setup(shape, aliases, decoded_aliases, direct_aliases, derived_aliases))

        exact_calls = list(re.finditer(
            rf"(?:\b[A-Za-z_]\w*\s*\*\s*)?{re.escape(exact_var)}\s*=\s*([A-Za-z_]\w*)\s*\(([^;{{}}]*)\)\s*;",
            match_body[:m.start()],
        ))
        if exact_calls:
            exact_callee, exact_args_raw = exact_calls[-1].groups()
            exact_shape = infer_lookup_shape_for_call(exact_callee, exact_var, exact_args_raw, source_text, type_catalog, infer_ops)
            if exact_shape:
                setup.extend(fallback_ops.lookup_miss_setup(
                    exact_shape,
                    shape,
                    aliases,
                    decoded_aliases,
                    direct_aliases,
                    derived_aliases,
                ))

        name = fallback_ops.safe_c_name(f"source_{fallback_var}_lookup_hit")
        if name in seen:
            continue
        seen.add(name)
        candidates.append(BranchCandidate(name, setup))

    return candidates
