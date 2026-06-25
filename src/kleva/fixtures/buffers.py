from __future__ import annotations

import re
from typing import Callable

from ..ast.model import CParam, CTypeCatalog


def struct_has_fields(type_catalog: CTypeCatalog | None, type_name: str, fields: set[str]) -> bool:
    if not type_catalog:
        return False
    available = set(type_catalog.struct_fields.get(type_name, {}))
    return fields.issubset(available)


def needs_len_data_shape(
    func_name: str,
    param_name: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    param: CParam,
    source_for_branch_shaping: Callable[[str | None, str], str],
) -> bool:
    """
    Detect generic buffer objects whose readable bytes are tracked by `len`
    and `data` fields, and whose target function is likely to read them.
    """
    if not struct_has_fields(type_catalog, param.base_type, {"len", "data"}):
        return False

    body = source_for_branch_shaping(source_text, func_name)
    if not body:
        return False

    if re.search(rf"\b{re.escape(param_name)}->len\b", body):
        return True
    return bool(re.search(
        rf"\b\w*(?:clone|copy|send|transmit|write)\w*\s*\([^;]*\b{re.escape(param_name)}\b",
        body,
    ))


def append_len_data_shape(lines: list[str], arg: str) -> None:
    if arg == "NULL":
        return

    if re.fullmatch(r"[A-Za-z_]\w*", arg):
        stem = arg
        len_expr = f"{arg}->len"
        data_expr = f"{arg}->data"
    elif re.fullmatch(r"&[A-Za-z_]\w*", arg):
        stem = arg[1:]
        len_expr = f"{stem}.len"
        data_expr = f"{stem}.data"
    else:
        return

    lines.append(f"uint8_t {stem}_data[64];")
    lines.append(f"memset({stem}_data, 0, sizeof({stem}_data));")
    lines.append(f"if ({data_expr} == NULL) {data_expr} = {stem}_data;")
    lines.append(f"if ({len_expr} == 0) {len_expr} = 8;")
    lines.append(f"memset({data_expr}, 0, {len_expr});")
