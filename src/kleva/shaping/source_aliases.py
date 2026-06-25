from __future__ import annotations

import re
from typing import Callable

from ..ast.model import CFunction, CParam, DerivedLocal
from .byte_order import decoded_field_aliases as byte_order_decoded_field_aliases
from .byte_order import host_to_network_fn
from .byte_order import propagate_local_aliases


def cast_aliases(body: str, params: dict[str, CParam]) -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    for m in re.finditer(
        r"\b([A-Za-z_]\w*)\s*\*\s*(\w+)\s*=\s*\(\s*\1\s*\*\s*\)\s*([^;]+);",
        body,
    ):
        cast_type, alias, expr = m.groups()
        if any(re.search(rf"\b{re.escape(p)}\b", expr) for p in params):
            aliases[alias] = (cast_type, expr.strip())
    return aliases


def void_param_cast_types(
    body: str,
    func: CFunction,
    is_void_star: Callable[[CParam], bool],
) -> dict[str, str]:
    """Find source patterns like `Type *alias = (Type *)ctx;` for void * params."""
    void_params = {p.name for p in func.params if is_void_star(p)}
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


def checksum_recompute_lines(
    body: str,
    aliases: dict[str, tuple[str, str]],
    append_unique: Callable[[list[str], str, set[str]], None],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b(\w*checksum\w*)\s*\(\s*(\w+)->data\s*,\s*\2->len\s*\)\s*!=\s*0", body):
        fn, obj = m.groups()
        for cast_type, expr in aliases.values():
            if expr == f"{obj}->data":
                append_unique(lines, f"(({cast_type} *){obj}->data)->checksum = 0;", seen)
                append_unique(lines, f"(({cast_type} *){obj}->data)->checksum = {fn}({obj}->data, {obj}->len);", seen)
    return lines


def cast_field_expr(cast_type: str, expr: str, field: str) -> str:
    return f"(({cast_type} *){expr})->{field}"


def expand_alias_expr(expr: str, aliases: dict[str, tuple[str, str]]) -> str:
    expanded = expr.strip()
    for alias, (cast_type, cast_expr) in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        expanded = re.sub(
            rf"\b{re.escape(alias)}\b",
            f"(({cast_type} *){cast_expr})",
            expanded,
        )
    return expanded


def cast_alias_backing_setup(
    alias: str,
    cast_type: str,
    expr: str,
    params: dict[str, CParam],
    safe_c_name: Callable[[str], str],
) -> list[str]:
    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", expr.strip())
    if not m:
        return []
    param_name, field_name = m.groups()
    if param_name not in params:
        return []

    storage = safe_c_name(f"kleva_{alias}_{field_name}_storage")
    return [
        f"{cast_type} {storage};",
        f"memset(&{storage}, 0, sizeof({storage}));",
        f"{param_name}->{field_name} = &{storage};",
    ]


def direct_field_aliases(body: str) -> dict[str, tuple[str, str]]:
    direct: dict[str, tuple[str, str]] = {}
    for m in re.finditer(
        r"\b(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)\s+(\w+)\s*=\s*(\w+)->(\w+)\s*;",
        body,
    ):
        local, alias, field = m.groups()
        direct[local] = (alias, field)
    return propagate_local_aliases(body, direct)


def decoded_field_aliases(body: str) -> dict[str, tuple[str, str, str]]:
    return byte_order_decoded_field_aliases(body)


def derived_local_aliases(body: str) -> dict[str, DerivedLocal]:
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


def literal_or_macro_value(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+", value)) or value.upper() == value


def field_expr_from_ref(ref: str) -> str | None:
    m = re.fullmatch(r"([A-Za-z_]\w*)->([A-Za-z_]\w*)", ref.strip())
    if m:
        return f"{m.group(1)}->{m.group(2)}"
    return None


def setup_local_bitwise_or(
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
                encode_fn = host_to_network_fn(decode_fn)
                if encode_fn:
                    cast_type, expr = aliases[alias]
                    target = cast_field_expr(cast_type, expr, field)
                    return [f"{target} = {encode_fn}({decode_fn}({target}) | ({value}));"]

        direct = direct_aliases.get(base)
        if direct and direct[0] in aliases:
            alias, field = direct
            cast_type, expr = aliases[alias]
            target = cast_field_expr(cast_type, expr, field)
            return [f"{target} |= ({value});"]

        return setup_local_value(base, value, aliases, decoded_aliases, direct_aliases, derived_aliases)

    return setup_local_value(local, value, aliases, decoded_aliases, direct_aliases, None)


def setup_local_value(
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
            return setup_local_value(
                derived.base,
                f"(({value}) << {derived.arg})",
                aliases,
                decoded_aliases,
                direct_aliases,
                derived_aliases,
            )
        if derived.kind == "and":
            return setup_local_bitwise_or(local, value, aliases, decoded_aliases, direct_aliases, derived_aliases)
        if derived.kind == "field_sub":
            target = field_expr_from_ref(derived.base)
            if target:
                return [f"{target} = ({value}) + {derived.arg};"]

    decoded = decoded_aliases.get(local)
    if decoded:
        decode_fn, alias, field = decoded
        if alias in aliases:
            encode_fn = host_to_network_fn(decode_fn)
            if encode_fn:
                cast_type, expr = aliases[alias]
                return [f"{cast_field_expr(cast_type, expr, field)} = {encode_fn}({value});"]

    direct = direct_aliases.get(local)
    if direct:
        alias, field = direct
        if alias in aliases:
            cast_type, expr = aliases[alias]
            return [f"{cast_field_expr(cast_type, expr, field)} = {value};"]

    return []


def good_path_setup_from_source(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal] | None,
    append_unique: Callable[[list[str], str, set[str]], None],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    for alias, (cast_type, expr) in aliases.items():
        for field, value in re.findall(
            rf"{re.escape(alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            append_unique(lines, f"{cast_field_expr(cast_type, expr, field)} = {value};", seen)

    for local, (decode_fn, alias, field) in decoded_aliases.items():
        if alias not in aliases:
            continue
        encode_fn = host_to_network_fn(decode_fn)
        if not encode_fn:
            continue
        cast_type, expr = aliases[alias]
        for _op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(<|>)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not literal_or_macro_value(rhs):
                continue
            append_unique(lines, f"{cast_field_expr(cast_type, expr, field)} = {encode_fn}({rhs});", seen)

    for local in [*decoded_aliases.keys(), *direct_aliases.keys()]:
        for _op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(!=|==)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not literal_or_macro_value(rhs):
                continue
            for line in setup_local_value(local, rhs, aliases, decoded_aliases, direct_aliases, derived_aliases):
                append_unique(lines, line, seen)

    for local in derived_aliases or {}:
        for _op, rhs in re.findall(
            rf"\b{re.escape(local)}\s*(!=|==)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            if not literal_or_macro_value(rhs):
                continue
            for line in setup_local_value(local, rhs, aliases, decoded_aliases, direct_aliases, derived_aliases):
                append_unique(lines, line, seen)

    return lines
