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
from .shaping.byte_order import (
    decoded_field_aliases as _byte_order_decoded_field_aliases,
    host_to_network_fn as _byte_order_host_to_network_fn,
    propagate_local_aliases as _byte_order_propagate_local_aliases,
)
from .shaping.branches import (
    BranchShapeOps,
    source_branch_candidates as _branch_shaper_source_branch_candidates,
)
from .shaping.assumptions import (
    assumption_setup_lines as _assumption_shaper_setup_lines,
)
from .shaping.candidates import BranchCandidate
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
from .shaping.switches import (
    StateSwitchOps,
    state_switch_candidates as _switch_shaper_state_switch_candidates,
    switch_case_blocks as _switch_shaper_case_blocks,
)


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
    aliases: dict[str, tuple[str, str]] = {}
    for m in re.finditer(
        r"\b([A-Za-z_]\w*)\s*\*\s*(\w+)\s*=\s*\(\s*\1\s*\*\s*\)\s*([^;]+);",
        body,
    ):
        cast_type, alias, expr = m.groups()
        if any(re.search(rf"\b{re.escape(p)}\b", expr) for p in params):
            aliases[alias] = (cast_type, expr.strip())
    return aliases


def _void_param_cast_types(body: str, func: CFunction) -> dict[str, str]:
    """Find source patterns like `Type *alias = (Type *)ctx;` for void * params."""
    void_params = {p.name for p in func.params if _is_void_star(p)}
    if not void_params:
        return {}

    casts: dict[str, str] = {}
    for cast_type, _alias, expr in re.findall(
        r"\b([A-Za-z_]\w*)\s*\*\s*(\w+)\s*=\s*\(\s*\1\s*\*\s*\)\s*([^;]+);",
        body,
    ):
        expr = expr.strip()
        if expr in void_params:
            casts.setdefault(expr, cast_type)
    return casts


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
    lines: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b(\w*checksum\w*)\s*\(\s*(\w+)->data\s*,\s*\2->len\s*\)\s*!=\s*0", body):
        fn, obj = m.groups()
        for cast_type, expr in aliases.values():
            if expr == f"{obj}->data":
                _append_unique(lines, f"(({cast_type} *){obj}->data)->checksum = 0;", seen)
                _append_unique(lines, f"(({cast_type} *){obj}->data)->checksum = {fn}({obj}->data, {obj}->len);", seen)
    return lines


def _cast_field_expr(cast_type: str, expr: str, field: str) -> str:
    return f"(({cast_type} *){expr})->{field}"


def _expand_alias_expr(expr: str, aliases: dict[str, tuple[str, str]]) -> str:
    expanded = expr.strip()
    for alias, (cast_type, cast_expr) in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = re.sub(
            rf"\b{re.escape(alias)}\b",
            f"(({cast_type} *){cast_expr})",
            expanded,
        )
    return expanded


def _cast_alias_backing_setup(alias: str, cast_type: str, expr: str, params: dict[str, CParam]) -> list[str]:
    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", expr.strip())
    if not m:
        return []
    param_name, field_name = m.groups()
    if param_name not in params:
        return []

    storage = _safe_c_name(f"kleva_{alias}_{field_name}_storage")
    return [
        f"{cast_type} {storage};",
        f"memset(&{storage}, 0, sizeof({storage}));",
        f"{param_name}->{field_name} = &{storage};",
    ]


def _propagate_local_aliases(body: str, aliases: dict) -> dict:
    return _byte_order_propagate_local_aliases(body, aliases)


def _decoded_field_aliases(body: str) -> dict[str, tuple[str, str, str]]:
    return _byte_order_decoded_field_aliases(body)


def _direct_field_aliases(body: str) -> dict[str, tuple[str, str]]:
    direct: dict[str, tuple[str, str]] = {}
    for m in re.finditer(
        r"\b(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)\s+(\w+)\s*=\s*(\w+)->(\w+)\s*;",
        body,
    ):
        local, alias, field = m.groups()
        direct[local] = (alias, field)
    return _propagate_local_aliases(body, direct)


def _derived_local_aliases(body: str) -> dict[str, DerivedLocal]:
    derived: dict[str, DerivedLocal] = {}
    scalar = r"(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)"

    for m in re.finditer(
        rf"\b{scalar}\s+(\w+)\s*=\s*(\w+)\s*>>\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*;",
        body,
    ):
        local, base, shift = m.groups()
        derived[local] = DerivedLocal("shr", base, shift)

    for m in re.finditer(
        rf"\b{scalar}\s+(\w+)\s*=\s*(\w+)\s*&\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*;",
        body,
    ):
        local, base, mask = m.groups()
        derived[local] = DerivedLocal("and", base, mask)

    for m in re.finditer(
        rf"\b{scalar}\s+(\w+)\s*=\s*(\w+)->(\w+)\s*-\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*;",
        body,
    ):
        local, obj, field, rhs = m.groups()
        derived[local] = DerivedLocal("field_sub", f"{obj}->{field}", rhs)

    changed = True
    while changed:
        changed = False
        for m in re.finditer(r"\b(\w+)\s*=\s*(\w+)\s*;", body):
            dst, src = m.groups()
            if src in derived and dst not in derived:
                derived[dst] = derived[src]
                changed = True
    return derived


def _literal_or_macro_value(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+", value)) or value.upper() == value


def _field_expr_from_ref(ref: str) -> str | None:
    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", ref.strip())
    if m:
        return f"{m.group(1)}->{m.group(2)}"
    return None


def _setup_local_bitwise_or(
    local: str,
    value: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None,
) -> list[str]:
    derived = (derived_aliases or {}).get(local)
    if derived and derived.kind == "and":
        base = derived.base
        decoded = decoded_aliases.get(base)
        if decoded:
            decode_fn, alias, field = decoded
            if alias in aliases:
                encode_fn = _host_to_network_fn(decode_fn)
                if encode_fn:
                    cast_type, expr = aliases[alias]
                    target = _cast_field_expr(cast_type, expr, field)
                    return [f"{target} = {encode_fn}({decode_fn}({target}) | ({value}));"]

        direct = direct_aliases.get(base)
        if direct and direct[0] in aliases:
            alias, field = direct
            cast_type, expr = aliases[alias]
            target = _cast_field_expr(cast_type, expr, field)
            return [f"{target} |= ({value});"]

        return _setup_local_value(base, value, aliases, decoded_aliases, direct_aliases, derived_aliases)

    return _setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, None)


def _setup_local_value(
    local: str,
    value: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None = None,
) -> list[str]:
    derived = (derived_aliases or {}).get(local)
    if derived:
        if derived.kind == "shr":
            return _setup_local_value(
                derived.base,
                f"(({value}) << {derived.arg})",
                aliases,
                decoded_aliases,
                direct_aliases,
                derived_aliases,
            )
        if derived.kind == "and":
            return _setup_local_bitwise_or(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases)
        if derived.kind == "field_sub":
            target = _field_expr_from_ref(derived.base)
            if target:
                return [f"{target} = ({value}) + {derived.arg};"]

    decoded = decoded_aliases.get(local)
    if decoded:
        decode_fn, alias, field = decoded
        if alias in aliases:
            encode_fn = _host_to_network_fn(decode_fn)
            if encode_fn:
                cast_type, expr = aliases[alias]
                return [f"{_cast_field_expr(cast_type, expr, field)} = {encode_fn}({value});"]

    direct = direct_aliases.get(local)
    if direct:
        alias, field = direct
        if alias in aliases:
            cast_type, expr = aliases[alias]
            return [f"{_cast_field_expr(cast_type, expr, field)} = {value};"]

    return []


def _good_path_setup_from_source(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None = None,
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    for alias, (cast_type, expr) in aliases.items():
        for field, value in re.findall(
            rf"{re.escape(alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            _append_unique(lines, f"{_cast_field_expr(cast_type, expr, field)} = {value};", seen)

    for local, (decode_fn, alias, field) in decoded_aliases.items():
        if alias not in aliases:
            continue
        encode_fn = _host_to_network_fn(decode_fn)
        if not encode_fn:
            continue
        cast_type, expr = aliases[alias]
        for op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(<|>)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not _literal_or_macro_value(rhs):
                continue
            false_value = rhs
            _append_unique(lines, f"{_cast_field_expr(cast_type, expr, field)} = {encode_fn}({false_value});", seen)

    for local in [*decoded_aliases.keys(), *direct_aliases.keys()]:
        for op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(!=|==)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not _literal_or_macro_value(rhs):
                continue
            value = rhs if op == "!=" else rhs
            for line in _setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(lines, line, seen)

    for local in derived_aliases or {}:
        for op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(!=|==)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not _literal_or_macro_value(rhs):
                continue
            value = rhs
            for line in _setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(lines, line, seen)

    return lines


def _loop_table_candidates(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if not type_catalog:
        return []
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    if "loop-tables" not in shaping_features:
        return []

    candidates: list[BranchCandidate] = []
    good_setup = _good_path_setup_from_source(body, aliases, decoded_aliases, direct_aliases, derived_aliases)

    for alias, (cast_type, expr) in aliases.items():
        pattern = (
            rf"{re.escape(alias)}->(\w+)->(\w+)\s*\[\s*(\w+)\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|\d+)"
            rf"\s*&&\s*{re.escape(alias)}->\1->\2\s*\[\s*\3\s*\]\.(\w+)\s*==\s*(\w+)"
        )
        for m in re.finditer(pattern, body):
            container_field, array_field, _idx, match_field_a, match_value_a, match_field_b, match_value_b = m.groups()
            container_param = type_catalog.field_type(cast_type, container_field)
            if not container_param:
                continue
            container_type = container_param.base_type
            array_param = type_catalog.field_type(container_type, array_field)
            if not array_param:
                continue
            element_type = array_param.base_type
            element_fields = type_catalog.struct_fields.get(element_type, {})

            preamble: list[str] = []
            setup = list(good_setup)
            state_var = _safe_c_name(f"kleva_{alias}_{container_field}")
            setup.extend([
                f"{container_type} {state_var};",
                f"memset(&{state_var}, 0, sizeof({state_var}));",
                f"(({cast_type} *){expr})->{container_field} = &{state_var};",
            ])

            decoded_match = decoded_aliases.get(match_value_b)
            if decoded_match:
                decode_fn, decoded_alias, decoded_field = decoded_match
                if decoded_alias in aliases:
                    decoded_cast, decoded_expr = aliases[decoded_alias]
                    encode_fn = _host_to_network_fn(decode_fn)
                    if encode_fn:
                        setup.append(f"{_cast_field_expr(decoded_cast, decoded_expr, decoded_field)} = {encode_fn}(1);")
                        match_value_b = "1"

            setup.extend([
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_a} = {match_value_a};",
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_b} = {match_value_b};",
            ])

            for field_name, field_param in element_fields.items():
                fp_decl = type_catalog.function_pointer(field_param.base_type)
                if fp_decl and "function-pointers" in shaping_features:
                    preamble.extend(_function_pointer_stub_preamble(fp_decl))
                    setup.append(
                        f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{field_name} = "
                        f"{_function_pointer_stub_name(fp_decl.name)};"
                    )

            candidates.append(BranchCandidate(
                _safe_c_name(f"source_{alias}_{array_field}_match"),
                setup,
                preamble,
            ))

            miss_setup = list(good_setup)
            miss_setup.extend([
                f"{container_type} {state_var};",
                f"memset(&{state_var}, 0, sizeof({state_var}));",
                f"(({cast_type} *){expr})->{container_field} = &{state_var};",
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_a} = 0;",
            ])
            candidates.append(BranchCandidate(
                _safe_c_name(f"source_{alias}_{array_field}_miss"),
                miss_setup,
                [],
            ))

    return candidates


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


def _setup_decoded_local(
    local: str,
    value: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
) -> list[str]:
    decoded = decoded_aliases.get(local)
    if not decoded:
        return []
    decode_fn, alias, field = decoded
    if alias not in aliases:
        return []
    encode_fn = _host_to_network_fn(decode_fn)
    if not encode_fn:
        return []
    cast_type, expr = aliases[alias]
    return [f"{_cast_field_expr(cast_type, expr, field)} = {encode_fn}({value});"]


def _split_conjuncts(expr: str) -> list[str]:
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


def _strip_outer_parens(expr: str) -> str:
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


def _rewrite_result_expr(
    expr: str,
    result_var: str,
    result_expr: str,
) -> str:
    return re.sub(rf"\b{re.escape(result_var)}->", f"{result_expr}.", expr.strip())


def _rewrite_source_alias_exprs(
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


def _condition_setup_lines(
    condition: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    result_var: str | None = None,
    result_expr: str | None = None,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()

    for raw_part in _split_conjuncts(condition):
        part = _strip_outer_parens(raw_part)

        m = re.fullmatch(r"(\w+)\s*&\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, value = m.groups()
            for line in _setup_local_bitwise_or(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(setup, line, seen)
            continue

        m = re.fullmatch(r"!\s*\(\s*(\w+)\s*&\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*\)", part)
        if m:
            local, _value = m.groups()
            for line in _setup_local_value(local, "0", aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(setup, line, seen)
            continue

        m = re.fullmatch(
            r"(\w+)\s*(==|!=|>|>=|<|<=)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
            part,
        )
        if m:
            local, op, rhs = m.groups()
            if result_var and result_expr:
                rhs = _rewrite_result_expr(rhs, result_var, result_expr)
            if op == "==":
                value = rhs
            elif op == "!=":
                value = _nonmatching_value(rhs)
            elif op == ">":
                value = f"(({rhs}) + 1)"
            elif op == ">=":
                value = rhs
            elif op == "<":
                value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
            else:
                value = rhs
            for line in _setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(setup, line, seen)
            continue

        m = re.fullmatch(r"(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
        if m:
            local, value = m.groups()
            for line in _setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases):
                _append_unique(setup, line, seen)
            continue

    return setup


def _condition_function_pointer_setup(
    condition: str,
    result_var: str,
    result_expr: str,
    result_type: str,
    type_catalog: CTypeCatalog | None,
) -> tuple[list[str], list[str]]:
    """Shape `if (obj->callback)` style guards for function-pointer fields."""
    if not type_catalog:
        return [], []

    setup: list[str] = []
    preamble: list[str] = []
    seen_setup: set[str] = set()
    seen_preamble: set[str] = set()

    for raw_part in _split_conjuncts(condition):
        part = _strip_outer_parens(raw_part)
        m = re.fullmatch(rf"{re.escape(result_var)}->([A-Za-z_]\w*)", part)
        if not m:
            continue

        field = m.group(1)
        field_param = type_catalog.field_type(result_type, field)
        if not field_param:
            continue
        fp_decl = type_catalog.function_pointer(field_param.base_type)
        if not fp_decl:
            continue

        for line in _function_pointer_stub_preamble(fp_decl):
            _append_unique(preamble, line, seen_preamble)
        _append_unique(
            setup,
            f"{result_expr}.{field} = {_function_pointer_stub_name(fp_decl.name)};",
            seen_setup,
        )

    return setup, preamble


def _callee_success_setups_in_block(
    block: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
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
        callee_setup, callee_preamble = _callee_success_setup_for_call(
            callee,
            _split_call_args(args_raw),
            source_text,
            type_catalog,
        )
        for line in callee_setup:
            _append_unique(setup, line, seen_setup)
        for line in callee_preamble:
            _append_unique(preamble, line, seen_preamble)

    return setup, preamble


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
    if not source_text or not type_catalog:
        return [], []

    function_decls = _function_decl_map(source_text)
    callee_decl = function_decls.get(callee)
    callee_body = _function_definition_body(source_text, callee)
    if not callee_decl or not callee_body or len(args) != len(callee_decl.params):
        return [], []

    arg_by_param = {p.name: a for p, a in zip(callee_decl.params, args)}
    visible_roots = set(arg_by_param.values())
    setup: list[str] = []
    seen: set[str] = set()
    for condition in _return_guard_conditions(callee_body):
        rewritten = condition
        for param, arg in sorted(arg_by_param.items(), key=lambda item: len(item[0]), reverse=True):
            rewritten = re.sub(rf"\b{re.escape(param)}\b", arg, rewritten)
        for line in _invert_simple_return_guard(rewritten, visible_roots):
            _append_unique(setup, line, seen)
    return setup, []


def _return_guard_conditions(prefix: str) -> list[str]:
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


def _invert_simple_return_guard(condition: str, visible_roots: set[str]) -> list[str]:
    if "||" in condition:
        return []

    setup: list[str] = []
    seen: set[str] = set()
    not_equals_by_field: dict[tuple[str, str], list[str]] = {}
    parts = [_strip_outer_parens(p) for p in _split_conjuncts(condition)]

    for part in parts:
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
                _append_unique(setup, f"{obj}->{field} = {_nonmatching_value(rhs)};", seen)
            continue

        m = re.fullmatch(r"!\s*([A-Za-z_]\w*)->([A-Za-z_]\w*)", part)
        if m:
            obj, field = m.groups()
            if obj in visible_roots:
                _append_unique(setup, f"{obj}->{field} = 1;", seen)
            continue

        m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", part)
        if m:
            obj, field = m.groups()
            if obj in visible_roots:
                _append_unique(setup, f"{obj}->{field} = 0;", seen)
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
            _append_unique(setup, f"{obj}->{field} = {value};", seen)

    for (obj, field), values in not_equals_by_field.items():
        _append_unique(setup, f"{obj}->{field} = {values[0]};", seen)

    return setup


def _source_guard_setup_before_call(body: str, call_pos: int, visible_roots: set[str]) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    for condition in _return_guard_conditions(body[:call_pos]):
        for line in _invert_simple_return_guard(condition, visible_roots):
            _append_unique(setup, line, seen)
    return setup


def _callee_success_candidates(
    body: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    visible_roots: set[str],
    shaping_features: set[str] | None = None,
) -> list[BranchCandidate]:
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)
    if "callee-success" not in shaping_features:
        return []

    candidates: list[BranchCandidate] = []
    seen: set[str] = set()

    def visible(expr: str) -> bool:
        expr = expr.strip()
        if _literal_or_macro_value(expr):
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
        args = _split_call_args(args_raw)
        if not all(visible(arg) for arg in args[:3]):
            continue
        setup, preamble = _callee_success_setup_for_call(callee, args, source_text, type_catalog)
        if not setup:
            continue
        guard_setup = _source_guard_setup_before_call(body, m.start(), visible_roots)
        name = _safe_c_name(f"source_{callee}_success")
        if name in seen:
            continue
        seen.add(name)
        candidates.append(BranchCandidate(
            name,
            [*guard_setup, *setup],
            preamble,
            witness_outputs=True,
        ))

    for m in patterns[1].finditer(body):
        callee, args_raw = m.groups()
        args = _split_call_args(args_raw)
        if not all(visible(arg) for arg in args[:3]):
            continue
        setup, preamble = _callee_success_setup_for_call(callee, args, source_text, type_catalog)
        if not setup:
            continue
        guard_setup = _source_guard_setup_before_call(body, m.start(), visible_roots)
        name = _safe_c_name(f"source_{callee}_success")
        if name in seen:
            continue
        seen.add(name)
        candidates.append(BranchCandidate(
            name,
            [*guard_setup, *setup],
            preamble,
            witness_outputs=True,
        ))

    return candidates


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


def _param_ref_from_arg(arg: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"&([A-Za-z_]\w*)", arg)
    if m:
        return (m.group(1), ".")
    if arg != "NULL" and re.fullmatch(r"[A-Za-z_]\w*", arg):
        return (arg, "->")
    return None


def _gen_null_setup_body(
    func: CFunction,
    null_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    shaping_features: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
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
    scalar_values = _scalar_values_from_assumptions(behavior.assumes)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif _is_void_star(p):
            # void * — always pass NULL
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            setup, arg, cleanup_for_param = _pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if _needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                _append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = _param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not _function_frees_param(source_text, func.name, p.name) and not _function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in (shaping_features if shaping_features is not None else set(DEFAULT_SHAPING_FEATURES)) and type_catalog.function_pointer(p.base_type):
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.base_type in _SCALAR_BOUNDS:
            lo, _ = _SCALAR_BOUNDS[p.base_type]
            value = scalar_values.get(p.name, str(lo))
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(_assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

    # Build the function call
    args_str = ", ".join(call_args)
    result_val = _extract_result_value(behavior.ensures)

    if func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        outputs.append(out_var)
        if result_val is not None:
            # Create sentinel to express the proven value
            sentinel = f"out_sentinel"
            lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
            outputs.append(sentinel)
    else:
        lines.append(f"{func.name}({args_str});")
        out_var = "out_ok"
        lines.append(f"int {out_var} = 1;")
        outputs.append(out_var)

    return lines, outputs, cleanup, preamble


def _append_return_field_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    out_var: str,
    param_args: dict[str, str],
    type_catalog: CTypeCatalog | None,
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
        elif field_param.base_type in _SCALAR_BOUNDS or field_param.base_type in ("EventType", "uint64_t", "size_t"):
            if field_param.base_type in _SCALAR_BOUNDS or field_param.base_type in ("uint64_t", "size_t"):
                lines.append(f"{field_param.base_type} {out_name} = {out_var} ? {out_var}->{field_name} : 0;")
                outputs.append(out_name)
            else:
                lines.append(f"int {out_name}_same = ({out_var} != NULL && {out_var}->{field_name} == {arg});")
                outputs.append(f"{out_name}_same")


def _is_observable_scalar_type(type_name: str, type_catalog: CTypeCatalog | None) -> bool:
    if type_name in _SCALAR_BOUNDS or type_name in ("uint64_t", "size_t", "EventType"):
        return True
    if type_catalog and type_catalog.is_complete_struct(type_name):
        return False
    if type_catalog and type_catalog.function_pointer(type_name):
        return False
    return bool(re.fullmatch(r"[A-Za-z_]\w*", type_name))


def _field_expr(base_expr: str, access: str, field_name: str) -> str:
    return f"{base_expr}{access}{field_name}"


def _append_source_witness_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
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
        if not p.is_pointer or p.is_const or _is_void_star(p):
            continue
        if p.name not in active_params or p.name not in param_refs:
            continue
        if _function_frees_param(source_text, func.name, p.name):
            continue
        if not type_catalog.is_complete_struct(p.base_type):
            continue

        base_expr, access = param_refs[p.name]
        for field_name, field_param in type_catalog.struct_fields.get(p.base_type, {}).items():
            out_name = _safe_c_name(f"out_{p.name}_{field_name}")
            expr = _field_expr(base_expr, access, field_name)

            if field_param.is_pointer or type_catalog.function_pointer(field_param.base_type):
                if out_name not in seen:
                    lines.append(f"int {out_name}_nonnull = ({expr} != NULL);")
                    outputs.append(f"{out_name}_nonnull")
                    seen.add(out_name)
                continue

            if _is_observable_scalar_type(field_param.base_type, type_catalog):
                if out_name not in seen:
                    c_type = field_param.base_type if field_param.base_type in _SCALAR_BOUNDS else "int"
                    lines.append(f"{c_type} {out_name} = {expr};")
                    outputs.append(out_name)
                    seen.add(out_name)
                continue

            if "[" in field_param.raw_type or not type_catalog.is_complete_struct(field_param.base_type):
                continue

            nested_access = f"{access}{field_name}."
            for nested_name, nested_param in type_catalog.struct_fields.get(field_param.base_type, {}).items():
                nested_out = _safe_c_name(f"out_{p.name}_{field_name}_{nested_name}")
                nested_expr = _field_expr(base_expr, nested_access, nested_name)
                if nested_param.is_pointer or type_catalog.function_pointer(nested_param.base_type):
                    if nested_out not in seen:
                        lines.append(f"int {nested_out}_nonnull = ({nested_expr} != NULL);")
                        outputs.append(f"{nested_out}_nonnull")
                        seen.add(nested_out)
                    continue
                if _is_observable_scalar_type(nested_param.base_type, type_catalog) and nested_out not in seen:
                    c_type = nested_param.base_type if nested_param.base_type in _SCALAR_BOUNDS else "int"
                    lines.append(f"{c_type} {nested_out} = {nested_expr};")
                    outputs.append(nested_out)
                    seen.add(nested_out)


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
    void_cast_types = _void_param_cast_types(_source_for_branch_shaping(source_text, func.name), func)
    non_null_params = set(_extract_non_null_params(behavior.assumes))
    nonzero_params = set(_extract_nonzero_params(behavior.assumes))
    scalar_values = _scalar_values_from_assumptions(behavior.assumes)
    object_params = set(valid_params) | non_null_params

    for p in func.params:
        if p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif _is_void_star(p):
            cast_type = void_cast_types.get(p.name)
            if p.name in object_params and cast_type and type_catalog and type_catalog.is_complete_struct(cast_type):
                var_name = _unique_name(f"{p.name}_{cast_type}", used_names)
                lines.append(f"{cast_type} {var_name};")
                lines.append(f"memset(&{var_name}, 0, sizeof({var_name}));")
                call_args.append(f"&{var_name}")
                param_args[p.name] = f"&{var_name}"
                param_refs[p.name] = (var_name, ".")
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.is_pointer and p.name in valid_params:
            setup, arg, cleanup_for_param = _pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if _needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                _append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = _param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not _function_frees_param(source_text, func.name, p.name) and not _function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif p.is_pointer:
            # Not a valid param — use NULL (will be an uninteresting branch)
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif type_catalog and "function-pointers" in (shaping_features if shaping_features is not None else set(DEFAULT_SHAPING_FEATURES)) and (fp_decl := type_catalog.function_pointer(p.base_type)):
            preamble.extend(_function_pointer_stub_preamble(fp_decl))
            stub_name = _function_pointer_stub_name(fp_decl.name)
            call_args.append(stub_name)
            param_args[p.name] = stub_name
        elif p.base_type in _SCALAR_BOUNDS:
            # Use a concrete value
            lo, _ = _SCALAR_BOUNDS[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(_assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))
    if extra_setup:
        lines.extend(_rewrite_setup_with_param_args(extra_setup, param_args))

    args_str = ", ".join(call_args)

    if source_shape_oracle:
        lines.append(f"{func.name}({args_str});")
        if source_shape_witnesses:
            _append_source_witness_outputs(
                lines,
                outputs,
                func,
                active_params,
                param_refs,
                source_text,
                type_catalog,
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
            _append_return_field_outputs(lines, outputs, func, out_var, param_args, type_catalog)
            if _function_returns_owned_pointer(func):
                free_fn = _lookup_free_fn(func.return_base, source_text)
                if free_fn:
                    cleanup.insert(0, f"if ({out_var}) {free_fn}({out_var});")
        else:
            outputs.append(out_var)
        # Check if there's a result value to verify
        result_val = _extract_result_value(behavior.ensures)
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


def _gen_mixed_test(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    shaping_features: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """
    Generate body for a mixed behavior where some params are null
    and some are valid. Common for functions with multiple pointer params
    where the contract covers all-null or specific combos.
    """
    null_params = _extract_null_params(behavior.assumes)
    valid_params = _extract_valid_params(behavior.assumes)

    # If it's purely null, use null body
    if null_params and not valid_params:
        return _gen_null_setup_body(
            func, null_params, behavior, source_text, type_catalog, function_decls, shaping_features
        )

    # If it's purely valid, use valid body
    if valid_params and not null_params:
        return _gen_valid_setup_body(
            func, valid_params, behavior, source_text, type_catalog, function_decls, shaping_features=shaping_features
        )

    # Mixed: some null, some valid
    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    non_null_params = set(_extract_non_null_params(behavior.assumes))
    nonzero_params = set(_extract_nonzero_params(behavior.assumes))
    scalar_values = _scalar_values_from_assumptions(behavior.assumes)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            setup, arg, cleanup_for_param = _pointer_argument_setup(
                p, source_text, type_catalog, function_decls, func.name, used_names
            )
            lines.extend(setup)
            if _needs_len_data_shape(func.name, p.name, source_text, type_catalog, p):
                _append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = _param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not _function_frees_param(source_text, func.name, p.name) and not _function_takes_param_ownership(source_text, func.name, p.name):
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in (shaping_features if shaping_features is not None else set(DEFAULT_SHAPING_FEATURES)) and (fp_decl := type_catalog.function_pointer(p.base_type)):
            if p.name in non_null_params:
                preamble.extend(_function_pointer_stub_preamble(fp_decl))
                stub_name = _function_pointer_stub_name(fp_decl.name)
                call_args.append(stub_name)
                param_args[p.name] = stub_name
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.base_type in _SCALAR_BOUNDS:
            lo, _ = _SCALAR_BOUNDS[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(_assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

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


# ── YAML emitter ──────────────────────────────────────────────────────────────

def _emit_str_list(lines: list[str], indent_n: int = 6) -> str:
    pad = " " * indent_n
    if not lines:
        return "[]"
    result = "\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace('"', '\\"')
        result += f'{pad}- "{escaped}"\n'
    return result.rstrip("\n")


def _emit_output_list(outputs: list[str], indent_n: int = 6) -> str:
    pad = " " * indent_n
    if not outputs:
        return "[]"
    return "[" + ", ".join(outputs) + "]"


def _emit_yaml_function(
    func: CFunction,
    behavior: ACSLBehavior,
    body: list[str],
    outputs: list[str],
    cleanup: list[str],
    ktest_dir: str,
    preamble: list[str] | None = None,
    source_include_names: list[str] | None = None,
    candidate: bool = False,
) -> list[str]:
    """Emit YAML lines for one function test entry."""
    preamble = preamble or []
    source_include_names = source_include_names or []
    body_text = "\n".join(body)
    for include_name in source_include_names:
        stem = Path(include_name).stem
        type_token = _safe_c_name(stem).title().replace("_", "")
        if re.search(rf"\b{re.escape(stem)}_", body_text) or re.search(rf"\b{re.escape(type_token)}\b", body_text):
            include_line = f'#include "{include_name}"'
            if include_line not in preamble:
                preamble = [include_line, *preamble]
    lines: list[str] = [
        "",
        f"  # {func.name} — behavior: {behavior.name}",
        f"  - name:      {ktest_dir.replace('klee_build/klee_out_', '')}",
        f"    ktest_dir: {ktest_dir}",
        "    inputs:    []",
    ]
    if preamble:
        lines.append(f"    preamble:  {_emit_str_list(preamble)}")
    lines.extend([
        f"    body:      {_emit_str_list(body)}",
        f"    outputs:   {_emit_output_list(outputs)}",
    ])
    if cleanup:
        lines.append(f"    cleanup:   {_emit_str_list(cleanup)}")
    else:
        lines.append("    cleanup:   []")
    if candidate:
        lines.append("    candidate: true")
    return lines


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
