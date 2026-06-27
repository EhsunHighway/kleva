"""Explicit source-text fallback helpers.

The normal KLEVA synthesis path is Clang AST / typed IR backed. Functions in
this module wrap older source-text parsers and scanners that remain available
only for compatibility modes, IR extraction failures, or opt-in
`regex-fallbacks` shaping.
"""
from __future__ import annotations

from pathlib import Path

from ..ast.model import CFunction, CTypeCatalog
from ..ast.parser import (
    build_type_catalog,
    function_decl_map,
    parse_header,
    split_call_args,
    strip_comments,
)
from ..ast.source_query import (
    function_accepts_null_param,
    function_body,
    function_definition_body,
    function_frees_param,
    function_returns_owned_pointer,
    function_takes_param_ownership,
)


def fallback_parse_header(header_path: str | Path) -> list[CFunction]:
    return parse_header(Path(header_path))


def fallback_build_type_catalog(source_text: str) -> CTypeCatalog:
    return build_type_catalog(source_text)


def fallback_function_decl_map(source_text: str) -> dict[str, CFunction]:
    return function_decl_map(source_text)


def fallback_split_call_args(args: str) -> list[str]:
    return split_call_args(args)


def fallback_strip_comments(source_text: str) -> str:
    return strip_comments(source_text)


def fallback_function_body(source_text: str | None, func_name: str) -> str:
    return function_body(source_text, func_name)


def fallback_function_definition_body(source_text: str | None, func_name: str) -> str:
    return function_definition_body(source_text, func_name)


def fallback_function_frees_param(
    source_text: str | None,
    func_name: str,
    param_name: str,
) -> bool:
    return function_frees_param(source_text, func_name, param_name)


def fallback_function_accepts_null_param(
    source_text: str | None,
    func_name: str,
    param_name: str,
) -> bool:
    return function_accepts_null_param(source_text, func_name, param_name)


def fallback_function_takes_param_ownership(
    source_text: str | None,
    func_name: str,
    param_name: str,
) -> bool:
    return function_takes_param_ownership(source_text, func_name, param_name)


def fallback_function_returns_owned_pointer(func: CFunction) -> bool:
    return function_returns_owned_pointer(func)

