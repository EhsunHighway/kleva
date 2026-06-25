from __future__ import annotations

import re

from ..ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from ..ast.source_query import camel_to_snake, lower_first, visible_function


def safe_c_name(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_") or "tmp"


def unique_name(preferred: str, used_names: set[str]) -> str:
    clean = safe_c_name(preferred)
    if clean not in used_names:
        used_names.add(clean)
        return clean
    i = 2
    while f"{clean}_{i}" in used_names:
        i += 1
    name = f"{clean}_{i}"
    used_names.add(name)
    return name


def is_void_star(p: CFunction | CParam) -> bool:
    return p.base_type == "void" and getattr(p, "is_pointer", False)


def lookup_constructor(
    base_type: str,
    function_decls: dict[str, CFunction] | None = None,
) -> CFunction | None:
    """
    Find a visible factory-style constructor for a type.

    This intentionally does not return a guessed fallback. Inventing
    T_create() for arbitrary domains makes synthesized YAML non-portable.
    """
    function_decls = function_decls or {}
    lower_type = lower_first(base_type)
    snake_type = camel_to_snake(base_type)
    candidates = [
        f"{lower_type}_create",
        f"{snake_type}_create",
        f"{lower_type}_new",
        f"{snake_type}_new",
        f"create_{lower_type}",
        f"create_{snake_type}",
    ]
    for candidate in candidates:
        decl = function_decls.get(candidate)
        if decl and decl.return_is_pointer and decl.return_base == base_type:
            return decl
    return None


def lookup_free_fn(base_type: str, source_text: str | None = None) -> str | None:
    """
    Guess the free/destroy function for a type.
    Uses naming conventions: T_free(), T_destroy().
    """
    lower_type = lower_first(base_type)
    snake_type = camel_to_snake(base_type)
    candidates = [
        f"{lower_type}_free",
        f"{snake_type}_free",
        f"{lower_type}_destroy",
        f"{snake_type}_destroy",
        f"free_{lower_type}",
        f"free_{snake_type}",
    ]
    for candidate in candidates:
        if visible_function(candidate, source_text):
            return candidate
    return None


def default_scalar_value(p: CParam) -> str:
    name = p.name.lower()
    if "mtu" in name:
        return "1500"
    if "prefix" in name:
        return "24"
    if "capacity" in name or "cap" in name or "size" in name:
        return "64"
    if "max" in name or "count" in name:
        return "4"
    if "protocol" in name:
        return "17"
    if "ip" in name:
        return "0x0100A8C0"
    if p.base_type in ("size_t", "uint64_t", "uint32_t", "uint16_t", "uint8_t"):
        return "1"
    if p.base_type in ("int", "int64_t", "int32_t", "int16_t", "int8_t"):
        return "1"
    return "0"


def default_return_value(return_type: str) -> str:
    rt = return_type.strip()
    if rt == "void":
        return ""
    if "*" in rt:
        return "0"
    return "0"


def function_pointer_stub_name(alias: str) -> str:
    return f"kleva_stub_{safe_c_name(alias)}"


def function_pointer_stub_preamble(decl: CFunctionPointerTypedef) -> list[str]:
    params: list[str] = []
    for i, p in enumerate(decl.params):
        name = p.name or f"arg{i}"
        raw_type = p.raw_type.strip()
        if p.is_array:
            raw_type = re.sub(r"\[[^\]]*\]", f"*{name}", raw_type)
        elif not re.search(rf"\b{re.escape(name)}\b", raw_type):
            raw_type = f"{raw_type} {name}"
        params.append(raw_type)

    params_s = ", ".join(params) if params else "void"
    lines = [f"static {decl.return_type} {function_pointer_stub_name(decl.name)}({params_s}) {{"]
    for p in decl.params:
        lines.append(f"    (void){p.name};")
    ret = default_return_value(decl.return_type)
    if ret:
        lines.append(f"    return {ret};")
    lines.append("}")
    return lines


def constructor_arg_setup(
    p: CParam,
    owner_var: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    used_names: set[str],
    depth: int,
) -> tuple[list[str], str]:
    if p.is_array:
        size = p.array_size or 1
        var_name = unique_name(f"{owner_var}_{p.name}", used_names)
        zeros = ", ".join(["0"] * size)
        return [f"uint8_t {var_name}[{size}] = {{{zeros}}};"], var_name

    if p.is_pointer:
        if p.base_type == "char":
            return [], '"kleva"'
        if p.base_type == "void":
            return [], "NULL"
        setup, arg, _cleanup = pointer_argument_setup(
            p,
            source_text=source_text,
            type_catalog=type_catalog,
            function_decls=function_decls,
            owner_func=None,
            used_names=used_names,
            preferred_name=f"{owner_var}_{p.name}",
            depth=depth + 1,
        )
        return setup, arg

    return [], default_scalar_value(p)


def constructor_setup(
    decl: CFunction,
    base_type: str,
    var_name: str,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    used_names: set[str],
    depth: int,
) -> tuple[list[str], str, list[str]]:
    if depth > 3:
        return [], "NULL", []

    lines: list[str] = []
    args: list[str] = []
    for p in decl.params:
        setup, arg = constructor_arg_setup(
            p, var_name, source_text, type_catalog, function_decls, used_names, depth
        )
        lines.extend(setup)
        args.append(arg)

    args_str = ", ".join(args)
    lines.extend([
        f"{base_type} *{var_name} = {decl.name}({args_str});",
        f"__GUARD__({var_name})",
    ])

    cleanup: list[str] = []
    free_fn = lookup_free_fn(base_type, source_text)
    if free_fn:
        cleanup.append(f"{free_fn}({var_name});")
    return lines, var_name, cleanup


def pointer_argument_setup(
    p: CParam,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    owner_func: str | None = None,
    used_names: set[str] | None = None,
    preferred_name: str | None = None,
    depth: int = 0,
) -> tuple[list[str], str, list[str]]:
    """Generate setup lines, call argument, and cleanup for one pointer param."""
    used_names = used_names or set()
    if is_void_star(p):
        return [], "NULL", []

    var_name = unique_name(preferred_name or p.name, used_names)
    if p.base_type == "uint8_t":
        buf_name = unique_name(f"{var_name}_buf", used_names)
        return (
            [
                f"uint8_t {buf_name}[64];",
                f"memset({buf_name}, 0, sizeof({buf_name}));",
            ],
            buf_name,
            [],
        )

    constructor = lookup_constructor(p.base_type, function_decls)
    if constructor:
        lines, arg, cleanup = constructor_setup(
            constructor,
            p.base_type,
            var_name,
            source_text,
            type_catalog,
            function_decls,
            used_names,
            depth,
        )
        if cleanup and cleanup[0].startswith(f"{owner_func}("):
            cleanup = []
        return lines, arg, cleanup

    if type_catalog and type_catalog.is_complete_struct(p.base_type):
        return (
            [
                f"{p.base_type} {var_name};",
                f"memset(&{var_name}, 0, sizeof({var_name}));",
            ],
            f"&{var_name}",
            [],
        )

    return (
        [f"/* kleva synth: no visible allocation strategy for {p.base_type} *{p.name}; using NULL */"],
        "NULL",
        [],
    )
