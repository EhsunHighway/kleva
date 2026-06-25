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


def function_prototype(decl: CFunction) -> str:
    params: list[str] = []
    for i, p in enumerate(decl.params):
        raw_type = p.raw_type.strip()
        if p.name and re.search(rf"\b{re.escape(p.name)}\b", raw_type):
            params.append(raw_type)
            continue
        name = p.name or f"arg{i}"
        params.append(f"{raw_type} {name}")

    params_s = ", ".join(params) if params else "void"
    return f"{decl.return_type} {decl.name}({params_s});"


def _needs_forward_typedef(base_type: str) -> bool:
    builtins = {
        "void", "char", "int", "float", "double", "size_t", "ssize_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "unsigned", "signed", "long", "short",
    }
    return bool(base_type) and base_type not in builtins


def forward_typedefs_for_function(decl: CFunction) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    if decl.return_is_pointer and _needs_forward_typedef(decl.return_base):
        names.append(decl.return_base)
        seen.add(decl.return_base)
    for p in decl.params:
        if p.is_pointer and _needs_forward_typedef(p.base_type) and p.base_type not in seen:
            names.append(p.base_type)
            seen.add(p.base_type)
    return [f"typedef struct {name} {name};" for name in names]


def default_scalar_value(p: CParam) -> str:
    name = p.name.lower()
    if "prefix" in name:
        return "24"
    if "capacity" in name or "cap" in name or "size" in name:
        return "64"
    if "max" in name or "count" in name:
        return "4"
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
    suppress_guard: bool = False,
) -> tuple[list[str], str, list[str]]:
    if depth > 3:
        return [], "NULL", []

    lines: list[str] = [
        *forward_typedefs_for_function(decl),
        function_prototype(decl),
    ]
    args: list[str] = []
    for p in decl.params:
        setup, arg = constructor_arg_setup(
            p, var_name, source_text, type_catalog, function_decls, used_names, depth
        )
        lines.extend(setup)
        args.append(arg)

    args_str = ", ".join(args)
    lines.append(f"{base_type} *{var_name} = {decl.name}({args_str});")
    if not suppress_guard:
        lines.append(f"__GUARD__({var_name})")

    cleanup: list[str] = []
    free_fn = lookup_free_fn(base_type, source_text)
    if free_fn:
        if _needs_forward_typedef(base_type):
            lines.append(f"typedef struct {base_type} {base_type};")
        lines.append(f"void {free_fn}({base_type} *arg0);")
        cleanup.append(f"{free_fn}({var_name});")
    return lines, var_name, cleanup


def complete_struct_setup(
    base_type: str,
    var_name: str,
    type_catalog: CTypeCatalog,
    used_names: set[str],
    depth: int = 0,
) -> list[str]:
    lines = [
        f"{base_type} {var_name};",
        f"memset(&{var_name}, 0, sizeof({var_name}));",
    ]
    if depth >= 2:
        return lines

    for field_name, field_param in type_catalog.struct_fields.get(base_type, {}).items():
        if not field_param.is_pointer:
            continue
        if field_param.base_type == base_type:
            continue
        if not type_catalog.is_complete_struct(field_param.base_type):
            continue

        nested_name = unique_name(f"{var_name}_{field_name}", used_names)
        lines.extend(complete_struct_setup(
            field_param.base_type,
            nested_name,
            type_catalog,
            used_names,
            depth + 1,
        ))
        pointer_depth = field_param.raw_type.count("*")
        if pointer_depth >= 2:
            slot_name = unique_name(f"{nested_name}_slot", used_names)
            lines.append(f"{field_param.base_type} *{slot_name} = &{nested_name};")
            lines.append(f"{var_name}.{field_name} = &{slot_name};")
        else:
            lines.append(f"{var_name}.{field_name} = &{nested_name};")

    return lines


def pointer_argument_setup(
    p: CParam,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    owner_func: str | None = None,
    used_names: set[str] | None = None,
    preferred_name: str | None = None,
    depth: int = 0,
    prefer_constructor: bool = False,
    suppress_constructor_guard: bool = False,
    prefer_raw_heap: bool = False,
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

    if prefer_raw_heap and type_catalog and type_catalog.is_complete_struct(p.base_type):
        return (
            [
                "void *malloc(size_t size);",
                f"{p.base_type} *{var_name} = malloc(sizeof({p.base_type}));",
                f"if ({var_name}) memset({var_name}, 0, sizeof(*{var_name}));",
            ],
            var_name,
            [],
        )

    constructor = lookup_constructor(p.base_type, function_decls)
    if prefer_constructor and constructor:
        lines, arg, cleanup = constructor_setup(
            constructor,
            p.base_type,
            var_name,
            source_text,
            type_catalog,
            function_decls,
            used_names,
            depth,
            suppress_constructor_guard,
        )
        if cleanup and cleanup[0].startswith(f"{owner_func}("):
            cleanup = []
        return lines, arg, cleanup

    if type_catalog and type_catalog.is_complete_struct(p.base_type):
        return (
            complete_struct_setup(p.base_type, var_name, type_catalog, used_names),
            f"&{var_name}",
            [],
        )

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
            suppress_constructor_guard,
        )
        if cleanup and cleanup[0].startswith(f"{owner_func}("):
            cleanup = []
        return lines, arg, cleanup

    return (
        [f"/* kleva synth: no visible allocation strategy for {p.base_type} *{p.name}; using NULL */"],
        "NULL",
        [],
    )
