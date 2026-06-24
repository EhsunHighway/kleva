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
import os
from dataclasses import dataclass, field
from pathlib import Path

from .acsl import ACSLSpec, ACSLBehavior
from .config import resolve_klee_clang, resolve_klee_include, resolve_llvm_link


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


# ── Data model (moved from init.py) ───────────────────────────────────────────

@dataclass
class CParam:
    name:        str
    raw_type:    str   # exactly as written, e.g. "const uint8_t *"
    base_type:   str   # e.g. "uint8_t", "Buffer"
    is_pointer:  bool
    is_const:    bool
    is_array:    bool  # e.g. "const uint8_t mac[6]"
    array_size:  int   # 0 if not an array


@dataclass
class CFunction:
    name:              str
    return_type:       str   # e.g. "Link *", "int", "void"
    return_base:       str   # stripped, e.g. "Link", "int"
    return_is_pointer: bool
    params:            list[CParam]


@dataclass
class BranchCandidate:
    """A source-derived path goal that can be added to a valid fixture."""
    name:     str
    setup:    list[str]
    preamble: list[str] = field(default_factory=list)


@dataclass
class CFunctionPointerTypedef:
    """A typedef'd function pointer such as `typedef void (*Cb)(int);`."""
    name:        str
    return_type: str
    params:      list[CParam]


@dataclass
class CTypeCatalog:
    """Facts inferred from visible C declarations."""
    complete_structs: set[str] = field(default_factory=set)
    opaque_structs:   set[str] = field(default_factory=set)
    function_pointers: dict[str, CFunctionPointerTypedef] = field(default_factory=dict)
    struct_fields: dict[str, dict[str, CParam]] = field(default_factory=dict)

    def is_complete_struct(self, type_name: str) -> bool:
        return type_name in self.complete_structs

    def function_pointer(self, type_name: str) -> CFunctionPointerTypedef | None:
        return self.function_pointers.get(type_name)

    def field_type(self, type_name: str, field_name: str) -> CParam | None:
        return self.struct_fields.get(type_name, {}).get(field_name)


# ── C header parser (moved from init.py) ──────────────────────────────────────

def _strip_comments(text: str) -> str:
    """Remove ACSL annotations and C comments."""
    text = re.sub(r"/\*@.*?\*/", "", text, flags=re.DOTALL)  # ACSL
    text = re.sub(r"/\*.*?\*/",  "", text, flags=re.DOTALL)  # block comments
    text = re.sub(r"//[^\n]*",   "", text)                    # line comments
    return text


def _parse_param(raw: str, index: int = 0) -> CParam | None:
    raw = raw.strip()
    if not raw or raw in ("void", "..."):
        return None

    is_const   = bool(re.search(r'\bconst\b', raw))
    is_pointer = "*" in raw
    is_array   = bool(re.search(r'\[\d*\]', raw))
    array_size = 0
    if is_array:
        m = re.search(r'\[(\d+)\]', raw)
        array_size = int(m.group(1)) if m else 0

    # Normalise: remove qualifiers and decorators to get tokens
    clean = re.sub(r'\bconst\b|\bvolatile\b|\brestrict\b', '', raw)
    clean = re.sub(r'\[[^\]]*\]', ' ', clean)
    clean = clean.replace('*', ' ')
    tokens = clean.split()

    if not tokens:
        return None

    type_words = {
        "void", "char", "int", "float", "double", "size_t", "ssize_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "unsigned", "signed", "long", "short", "struct",
    }

    # Parameter names are optional in C declarations.
    if len(tokens) == 1 or tokens[-1] in type_words:
        name = f"arg{index}"
        type_toks = tokens
    else:
        name = tokens[-1]
        type_toks = tokens[:-1]

    # base_type: skip qualifiers and 'struct', pick first real type token
    base_type = next(
        (t for t in type_toks if t not in ("unsigned", "signed", "long", "short", "struct")),
        type_toks[0] if type_toks else "int",
    )

    return CParam(
        name=name,
        raw_type=raw.strip(),
        base_type=base_type,
        is_pointer=is_pointer,
        is_const=is_const,
        is_array=is_array,
        array_size=array_size,
    )


def parse_header(header_path: Path) -> list[CFunction]:
    """Extract public function declarations from a C header file."""
    return _parse_function_decls(header_path.read_text())


def _parse_function_decls(raw_text: str) -> list[CFunction]:
    """Extract function declarations from C text."""
    text = _strip_comments(raw_text)
    text = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", text)

    # Remove preprocessor, typedef structs, struct forward decls
    text = re.sub(r"#[^\n]*",                                          "", text)
    text = re.sub(r"typedef\s+struct\s*\w*\s*\{[^}]*\}\s*\w+\s*;",   "", text, flags=re.DOTALL)
    text = re.sub(r"typedef\s+[^;]+;",                                 "", text, flags=re.DOTALL)
    text = re.sub(r"struct\s+\w+\s*;",                                 "", text)
    text = re.sub(r"\s+",                                              " ", text)

    funcs: list[CFunction] = []

    # Match:  <return_type>  <name>  ( <params> )  ;
    pattern = re.compile(
        r"((?:const\s+)?(?:struct\s+)?\w+(?:\s*\*+)?)"  # return type: one word + optional *
        r"\s*"                                            # optional space
        r"(\w+)\s*"                                       # function name
        r"\(([^)]*)\)\s*;"                                # ( params ) ;
    )

    for m in pattern.finditer(text):
        ret_raw  = m.group(1).strip()
        fname    = m.group(2).strip()
        args_raw = m.group(3).strip()

        # Skip obvious non-function matches
        if fname.upper() == fname or fname.startswith("_"):
            continue

        params: list[CParam] = []
        if args_raw and args_raw != "void":
            for i, p_raw in enumerate(args_raw.split(",")):
                p = _parse_param(p_raw, i)
                if p:
                    params.append(p)

        ret_is_ptr  = "*" in ret_raw
        ret_clean   = re.sub(r"[\*\s]", "", ret_raw.replace("const", "").replace("struct", "")).strip()

        funcs.append(CFunction(
            name=fname,
            return_type=ret_raw,
            return_base=ret_clean,
            return_is_pointer=ret_is_ptr,
            params=params,
        ))

    return funcs


def _function_decl_map(text: str) -> dict[str, CFunction]:
    return {f.name: f for f in _parse_function_decls(text)}


# ── Constructor / free pattern inference ──────────────────────────────────────

def _lower_first(s: str) -> str:
    """Lowercase the first character of a string."""
    return s[0].lower() + s[1:] if s else s


def _camel_to_snake(s: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def build_type_catalog(text: str) -> CTypeCatalog:
    """
    Infer complete-vs-opaque struct types from visible C declarations.

    Complete structs can be stack allocated. Opaque structs must be handled
    through visible constructors or passed as NULL.
    """
    catalog = CTypeCatalog()
    text = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", text)
    field_text = _strip_comments(text)

    for m in re.finditer(
        r"\btypedef\s+struct\s+(\w+)?\s*\{[^}]*\}\s*(\w+)\s*;",
        text,
        flags=re.DOTALL,
    ):
        tag, alias = m.group(1), m.group(2)
        if tag:
            catalog.complete_structs.add(tag)
        catalog.complete_structs.add(alias)

    for m in re.finditer(
        r"\btypedef\s+struct\s+(\w+)?\s*\{(?P<body>[^}]*)\}\s*(?P<alias>\w+)\s*;",
        field_text,
        flags=re.DOTALL,
    ):
        tag = m.group(1)
        alias = m.group("alias")
        fields: dict[str, CParam] = {}
        for i, field_raw in enumerate(m.group("body").split(";")):
            field_raw = field_raw.strip()
            if not field_raw:
                continue
            p = _parse_param(field_raw, i)
            if p:
                fields[p.name] = p
        catalog.struct_fields[alias] = fields
        if tag:
            catalog.struct_fields[tag] = fields

    for m in re.finditer(
        r"\bstruct\s+(\w+)\s*\{[^}]*\}\s*;",
        text,
        flags=re.DOTALL,
    ):
        catalog.complete_structs.add(m.group(1))

    for m in re.finditer(
        r"\bstruct\s+(?P<tag>\w+)\s*\{(?P<body>[^}]*)\}\s*;",
        field_text,
        flags=re.DOTALL,
    ):
        fields: dict[str, CParam] = {}
        for i, field_raw in enumerate(m.group("body").split(";")):
            field_raw = field_raw.strip()
            if not field_raw:
                continue
            p = _parse_param(field_raw, i)
            if p:
                fields[p.name] = p
        catalog.struct_fields[m.group("tag")] = fields

    for m in re.finditer(r"\btypedef\s+struct\s+(\w+)\s+(\w+)\s*;", text):
        tag, alias = m.group(1), m.group(2)
        catalog.opaque_structs.update({tag, alias})

    for m in re.finditer(r"\bstruct\s+(\w+)\s*;", text):
        catalog.opaque_structs.add(m.group(1))

    for m in re.finditer(
        r"\btypedef\s+([^;()]+?)\s*\(\s*\*\s*(\w+)\s*\)\s*\(([^;]*)\)\s*;",
        text,
        flags=re.DOTALL,
    ):
        return_type, name, params_raw = m.groups()
        params: list[CParam] = []
        params_raw = params_raw.strip()
        if params_raw and params_raw != "void":
            for i, raw_param in enumerate(params_raw.split(",")):
                p = _parse_param(raw_param, i)
                if p:
                    params.append(p)
        catalog.function_pointers[name] = CFunctionPointerTypedef(
            name=name,
            return_type=" ".join(return_type.split()),
            params=params,
        )

    catalog.opaque_structs.difference_update(catalog.complete_structs)
    return catalog


def _visible_function(name: str, source_text: str | None) -> bool:
    if not source_text:
        return False
    return bool(re.search(rf"\b{re.escape(name)}\s*\(", source_text))


def _function_body(source_text: str | None, func_name: str) -> str:
    if not source_text:
        return ""
    m = re.search(rf"\b\w+(?:\s*\*)?\s+{re.escape(func_name)}\s*\([^)]*\)\s*\{{", source_text)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    while i < len(source_text) and depth:
        if source_text[i] == "{":
            depth += 1
        elif source_text[i] == "}":
            depth -= 1
        i += 1
    return source_text[start:i - 1]


def _function_frees_param(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Detect simple ownership transfer: the target function calls some *_free(param)
    or free(param), so generated tests should not free that parameter again.
    """
    body = _function_body(source_text, func_name)
    if not body:
        return False
    return bool(re.search(rf"\b(?:\w+_free|free)\s*\(\s*{re.escape(param_name)}\s*\)", body))


def _function_accepts_null_param(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Decide whether a no-ACSL pointer parameter is safe to test as NULL.

    A pointer type alone is not a contract. Without an explicit ACSL null
    behavior, synth only emits a NULL case when the source has a recognizable
    null guard for that parameter.
    """
    body = _function_body(source_text, func_name)
    if not body:
        return False
    name = re.escape(param_name)
    if re.search(rf"\bif\s*\(\s*!\s*{name}\s*\)", body):
        return True
    if re.search(rf"\bif\s*\(\s*{name}\s*==\s*NULL\s*\)", body):
        return True
    if re.search(rf"\bif\s*\(\s*NULL\s*==\s*{name}\s*\)", body):
        return True
    if func_name.endswith(("_free", "_destroy")) and re.search(rf"\bif\s*\(\s*{name}\s*\)", body):
        return True
    return False


def _function_takes_param_ownership(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Detect simple enqueue/ownership transfer patterns.

    If the function stores a pointer into an owner object or queue, a generated
    cleanup that also frees that pointer can double-free after the owner is
    destroyed. This is intentionally conservative: leaks in generated tests are
    better than invalid cleanup.
    """
    body = _function_body(source_text, func_name)
    if not body:
        return False
    name = re.escape(param_name)
    return bool(re.search(rf"\b\w*(?:add|push|insert|append|schedule|enqueue)\w*\s*\([^;]*\b{name}\b(?!\s*->)", body))


def _function_returns_owned_pointer(func: CFunction) -> bool:
    if not func.return_is_pointer:
        return False
    snake_type = _camel_to_snake(func.return_base)
    lower_type = _lower_first(func.return_base)
    constructor_names = {
        f"{lower_type}_create",
        f"{snake_type}_create",
        f"{lower_type}_new",
        f"{snake_type}_new",
        f"create_{lower_type}",
        f"create_{snake_type}",
    }
    return func.name in constructor_names or bool(re.search(r"(?:create|new|alloc)$", func.name))


def _lookup_constructor(
    base_type: str,
    function_decls: dict[str, CFunction] | None = None,
) -> CFunction | None:
    """
    Find a visible factory-style constructor for a type.

    This intentionally does not return a guessed fallback. Inventing
    T_create() for arbitrary domains makes synthesized YAML non-portable.
    """
    function_decls = function_decls or {}
    lower_type = _lower_first(base_type)
    snake_type = _camel_to_snake(base_type)
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


def _lookup_free_fn(base_type: str, source_text: str | None = None) -> str | None:
    """
    Guess the free/destroy function for a type.
    Uses naming conventions: T_free(), T_destroy().
    """
    lower_type = _lower_first(base_type)
    snake_type = _camel_to_snake(base_type)
    candidates = [
        f"{lower_type}_free",
        f"{snake_type}_free",
        f"{lower_type}_destroy",
        f"{snake_type}_destroy",
        f"free_{lower_type}",
        f"free_{snake_type}",
    ]
    for candidate in candidates:
        if _visible_function(candidate, source_text):
            return candidate
    return None


def _unique_name(preferred: str, used_names: set[str]) -> str:
    clean = re.sub(r"\W+", "_", preferred).strip("_") or "tmp"
    if clean not in used_names:
        used_names.add(clean)
        return clean
    i = 2
    while f"{clean}_{i}" in used_names:
        i += 1
    name = f"{clean}_{i}"
    used_names.add(name)
    return name


def _default_scalar_value(p: CParam) -> str:
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


def _default_return_value(return_type: str) -> str:
    rt = return_type.strip()
    if rt == "void":
        return ""
    if "*" in rt:
        return "0"
    return "0"


def _function_pointer_stub_name(alias: str) -> str:
    return f"kleva_stub_{_safe_c_name(alias)}"


def _function_pointer_stub_preamble(decl: CFunctionPointerTypedef) -> list[str]:
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
    lines = [f"static {decl.return_type} {_function_pointer_stub_name(decl.name)}({params_s}) {{"]
    for p in decl.params:
        lines.append(f"    (void){p.name};")
    ret = _default_return_value(decl.return_type)
    if ret:
        lines.append(f"    return {ret};")
    lines.append("}")
    return lines


def _constructor_arg_setup(
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
        var_name = _unique_name(f"{owner_var}_{p.name}", used_names)
        zeros = ", ".join(["0"] * size)
        return [f"uint8_t {var_name}[{size}] = {{{zeros}}};"], var_name

    if p.is_pointer:
        if p.base_type == "char":
            return [], '"kleva"'
        if p.base_type == "void":
            return [], "NULL"
        setup, arg, _cleanup = _pointer_argument_setup(
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

    return [], _default_scalar_value(p)


def _constructor_setup(
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
        setup, arg = _constructor_arg_setup(
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
    free_fn = _lookup_free_fn(base_type, source_text)
    if free_fn:
        cleanup.append(f"{free_fn}({var_name});")
    return lines, var_name, cleanup


def _collect_visible_headers(
    header_path: Path,
    include_dirs: list[Path] | None = None,
    seen: set[Path] | None = None,
) -> list[str]:
    """Read a header and recursively read local quoted includes."""
    include_dirs = include_dirs or []
    seen = seen or set()

    try:
        resolved = header_path.resolve()
    except FileNotFoundError:
        return []

    if resolved in seen or not resolved.exists():
        return []
    seen.add(resolved)

    text = resolved.read_text()
    parts = [text]
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [resolved.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                parts.extend(_collect_visible_headers(candidate, include_dirs, seen))
                break
    return parts


def _collect_visible_header_paths(
    header_path: Path,
    include_dirs: list[Path] | None = None,
    seen: set[Path] | None = None,
) -> list[Path]:
    """Return a header and recursively discovered local quoted includes."""
    include_dirs = include_dirs or []
    seen = seen or set()

    try:
        resolved = header_path.resolve()
    except FileNotFoundError:
        return []

    if resolved in seen or not resolved.exists():
        return []
    seen.add(resolved)

    paths = [resolved]
    text = resolved.read_text()
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [resolved.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                paths.extend(_collect_visible_header_paths(candidate, include_dirs, seen))
                break
    return paths


def _collect_source_include_headers(
    source_path: str | Path,
    include_dirs: list[Path] | None = None,
) -> list[str]:
    """Read local quoted headers included directly by a source file."""
    source = Path(source_path)
    if not source.exists():
        return []

    include_dirs = include_dirs or []
    parts: list[str] = []
    text = source.read_text()
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [source.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                parts.extend(_collect_visible_headers(candidate, include_dirs))
                break
    return parts


def _source_include_names(source_path: str | Path) -> list[str]:
    source = Path(source_path)
    if not source.exists():
        return []
    text = source.read_text()
    names: list[str] = []
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        name = Path(m.group(1)).name
        if name not in names:
            names.append(name)
    return names


def _format_path_for_yaml(path: Path) -> str:
    return os.path.relpath(path.resolve(), Path.cwd().resolve())


def _suggest_extra_sources(
    header_path: Path,
    include_dirs: list[Path],
    primary_source: str,
) -> list[str]:
    """Suggest .c files sitting next to recursively included project headers."""
    primary = Path(primary_source)
    try:
        primary_resolved = primary.resolve()
    except FileNotFoundError:
        primary_resolved = primary

    suggestions: list[str] = []
    seen: set[str] = set()
    for h in _collect_visible_header_paths(header_path, include_dirs):
        c_path = h.with_suffix(".c")
        if not c_path.exists():
            continue
        try:
            if c_path.resolve() == primary_resolved:
                continue
        except FileNotFoundError:
            pass
        formatted = _format_path_for_yaml(c_path)
        if formatted not in seen:
            seen.add(formatted)
            suggestions.append(formatted)
    return suggestions


def _dedupe_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        try:
            key = str(path.resolve())
        except FileNotFoundError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(raw)
    return result


def _pointer_argument_setup(
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
    if _is_void_star(p):
        return [], "NULL", []

    var_name = _unique_name(preferred_name or p.name, used_names)
    if p.base_type == "uint8_t":
        buf_name = _unique_name(f"{var_name}_buf", used_names)
        return (
            [
                f"uint8_t {buf_name}[64];",
                f"memset({buf_name}, 0, sizeof({buf_name}));",
            ],
            buf_name,
            [],
        )

    constructor = _lookup_constructor(p.base_type, function_decls)
    if constructor:
        lines, arg, cleanup = _constructor_setup(
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


# ─── ACSL expression analysis ────────────────────────────────────────────────

def _extract_null_params(assumes_exprs: list[str]) -> list[str]:
    """
    From ACSL assumes expressions like:
        "iface == \\null" or "iface == \\null || frame == \\null"
    Extract the parameter names that are asserted null.

    Returns list of param names, e.g. ["iface", "frame"]
    
    Note: The ACSL text contains literal \\null (single backslash in C syntax).
    In the parsed Python strings this becomes \null.
    """
    null_params: list[str] = []
    for expr in assumes_exprs:
        # Split on || and && to handle compound expressions
        parts = re.split(r'\|\||&&', expr)
        for part in parts:
            # Match: param == \null (single backslash, literal from ACSL)
            m = re.search(r'(\w+)\s*==\s*\\(?:null|NULL)\b', part.strip())
            if m:
                null_params.append(m.group(1))
            # Match: \null == param
            m = re.search(r'\\(?:null|NULL)\b\s*==\s*(\w+)', part.strip())
            if m:
                null_params.append(m.group(1))
    return null_params


def _extract_valid_params(assumes_exprs: list[str]) -> list[str]:
    """
    From ACSL assumes expressions like:
        "\\valid(iface)" or "\\valid(iface) && \\valid(frame)"
    Extract the parameter names that are asserted valid.

    Note: ACSL uses \valid(...) which in the parsed Python strings 
    appears as a single backslash.
    """
    valid_params: list[str] = []
    for expr in assumes_exprs:
        for m in re.finditer(r'\\(?:valid|valid_read)\((\w+)', expr):
            valid_params.append(m.group(1))
    return valid_params


def _extract_non_null_params(assumes_exprs: list[str]) -> list[str]:
    """Extract simple ACSL assumptions like `ctx != \null`."""
    params: list[str] = []
    for expr in assumes_exprs:
        parts = re.split(r'\|\||&&', expr)
        for part in parts:
            part = part.strip()
            m = re.search(r'(\w+)\s*!=\s*\\(?:null|NULL)\b', part)
            if m:
                params.append(m.group(1))
            m = re.search(r'\\(?:null|NULL)\b\s*!=\s*(\w+)', part)
            if m:
                params.append(m.group(1))
    return params


def _extract_nonzero_params(assumes_exprs: list[str]) -> list[str]:
    """Extract simple ACSL assumptions like `port != 0` or `bw > 0`."""
    params: list[str] = []
    for expr in assumes_exprs:
        for part in re.split(r'\|\||&&', expr):
            part = part.strip()
            m = re.search(r'(\w+)\s*!=\s*0\b', part)
            if m:
                params.append(m.group(1))
            m = re.search(r'\b0\s*!=\s*(\w+)', part)
            if m:
                params.append(m.group(1))
            m = re.search(r'(\w+)\s*>\s*0\b', part)
            if m:
                params.append(m.group(1))
            m = re.search(r'\b0\s*<\s*(\w+)', part)
            if m:
                params.append(m.group(1))
    return params


def _scalar_values_from_assumptions(assumes_exprs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for expr in assumes_exprs:
        for part in re.split(r'\|\||&&', expr):
            part = part.strip()
            m = re.fullmatch(r'(\w+)\s*==\s*(0x[0-9a-fA-F]+|\d+)', part)
            if m:
                values[m.group(1)] = m.group(2)
                continue
            m = re.fullmatch(r'(0x[0-9a-fA-F]+|\d+)\s*==\s*(\w+)', part)
            if m:
                values[m.group(2)] = m.group(1)
                continue
            m = re.fullmatch(r'(\w+)\s*>\s*0\b', part)
            if m:
                values[m.group(1)] = "1"
                continue
            m = re.fullmatch(r'\b0\s*<\s*(\w+)', part)
            if m:
                values[m.group(1)] = "1"
    return values


def _extract_result_value(ensures_exprs: list[str]) -> int | None:
    """
    From ACSL ensures expressions like:
        "\\result == -1" or "\\result == 0" or "\\result == 0xFFFF"
    Extract the integer value (decimal or hex).

    Returns the integer, or None if not found.

    Note: ACSL uses \result which appears as a single backslash.
    """
    values: set[int] = set()
    for expr in ensures_exprs:
        simple = expr.strip()
        while simple.startswith("(") and simple.endswith(")"):
            inner = simple[1:-1].strip()
            if not inner:
                break
            simple = inner

        m = re.fullmatch(r'\\result\s*==\s*(0x[0-9a-fA-F]+|-?\d+)', simple)
        if m:
            raw = m.group(1)
            values.add(int(raw, 16) if raw.lower().startswith("0x") else int(raw))
            continue

        m = re.fullmatch(r'(0x[0-9a-fA-F]+|-?\d+)\s*==\s*\\result', simple)
        if m:
            raw = m.group(1)
            values.add(int(raw, 16) if raw.lower().startswith("0x") else int(raw))
    if len(values) == 1:
        return next(iter(values))
    return None


def _safe_c_name(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_") or "tmp"


def _literal_for_relation(op: str, rhs: str) -> str:
    if op == "<":
        return f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
    if op == "<=":
        return rhs
    if op == ">":
        return f"(({rhs}) + 1)"
    return rhs


def _is_literal_or_macro(value: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Z][A-Z0-9_]*", value))


def _append_unique(lines: list[str], line: str, seen: set[str]) -> None:
    if line not in seen:
        lines.append(line)
        seen.add(line)


def _param_access(param: str, suffix: str, param_refs: dict[str, tuple[str, str]] | None) -> str:
    if param_refs and param in param_refs:
        base, sep = param_refs[param]
        return f"{base}{sep}{suffix}"
    return f"{param}->{suffix}"


def _rewrite_value(value: str, param_args: dict[str, str] | None) -> str:
    if param_args and value in param_args:
        return param_args[value]
    return value


def _setup_for_quantified_arrays(
    expr: str,
    param_refs: dict[str, tuple[str, str]] | None,
    param_args: dict[str, str] | None,
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    exists = re.search(
        r"\\exists\s+integer\s+(\w+)\s*;\s*0\s*<=\s*\1\s*<\s*([A-Za-z_]\w*|\d+)\s*&&\s*(.+)",
        expr,
        flags=re.DOTALL,
    )
    if exists:
        idx, _bound, body = exists.groups()
        for obj, arr, field, value in re.findall(
            rf"(\w+)->(\w+)\s*\[\s*{re.escape(idx)}\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            value = _rewrite_value(value, param_args)
            _append_unique(lines, f"{_param_access(obj, f'{arr}[0].{field}', param_refs)} = {value};", seen)
        return lines

    forall = re.search(
        r"\\forall\s+integer\s+(\w+)\s*;\s*0\s*<=\s*\1\s*<\s*([A-Za-z_]\w*|\d+)\s*==>\s*(.+)",
        expr,
        flags=re.DOTALL,
    )
    if forall:
        idx, bound, body = forall.groups()
        eq = re.search(
            rf"(\w+)->(\w+)\s*\[\s*{re.escape(idx)}\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        )
        if eq:
            obj, arr, field, value = eq.groups()
            value = _rewrite_value(value, param_args)
            target = _param_access(obj, f"{arr}[kleva_i].{field}", param_refs)
            _append_unique(lines, f"for (int kleva_i = 0; kleva_i < {bound}; kleva_i++) {target} = {value};", seen)
        return lines

    return lines


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
    lines: list[str] = []
    seen: set[str] = set()
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)

    for expr in assumes_exprs:
        if "quantified-arrays" in shaping_features:
            for line in _setup_for_quantified_arrays(expr, param_refs, param_args):
                _append_unique(lines, line, seen)

        for part in re.split(r'\s*&&\s*', expr):
            part = part.strip()

            # obj->field >= LIMIT, obj->capacity == 64, etc.
            m = re.fullmatch(r'(\w+)->(\w+)\s*(==|>=|>|<=|<)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)', part)
            if m:
                obj, field, op, rhs = m.groups()
                if obj in params_by_name:
                    rhs = _rewrite_value(rhs, param_args)
                    value = rhs if op == "==" else _literal_for_relation(op, rhs)
                    _append_unique(lines, f"{_param_access(obj, field, param_refs)} = {value};", seen)
                continue

            # src == link->end_a, or link->end_a == src.
            m = re.fullmatch(r'(\w+)\s*==\s*(\w+)->(\w+)', part)
            if m:
                lhs, obj, field = m.groups()
                if lhs in params_by_name and obj in params_by_name:
                    target = param_args.get(lhs, lhs)
                    source = _param_access(obj, field, param_refs)
                    _append_unique(lines, f"{target} = {source};", seen)
                continue

            m = re.fullmatch(r'(\w+)->(\w+)\s*==\s*(\w+)', part)
            if m:
                obj, field, rhs = m.groups()
                if rhs in params_by_name and obj in params_by_name:
                    target = param_args.get(rhs, rhs)
                    source = _param_access(obj, field, param_refs)
                    _append_unique(lines, f"{target} = {source};", seen)
                continue

            # link->up != 0, link->end_a->up != 0, etc.
            m = re.fullmatch(r'(\w+)->(\w+)(?:->(\w+))?\s*(==|!=)\s*(0x[0-9a-fA-F]+|\d+)', part)
            if m:
                obj, field1, field2, op, rhs = m.groups()
                if obj in params_by_name:
                    suffix = f"{field1}->{field2}" if field2 else field1
                    value = _nonmatching_value(rhs) if op == "!=" else rhs
                    _append_unique(lines, f"{_param_access(obj, suffix, param_refs)} = {value};", seen)
                continue

            # obj->field >= obj->base + N: make the relation true without
            # assuming domain-specific helper functions or type names.
            m = re.fullmatch(r'(\w+)->(\w+)\s*>=\s*(\w+)->(\w+)\s*\+\s*([A-Za-z_]\w*|\d+)', part)
            if m and m.group(1) == m.group(3) and m.group(1) in params_by_name:
                obj, field, _same_obj, base, offset = m.groups()
                offset = _rewrite_value(offset, param_args)
                _append_unique(lines, f"{_param_access(obj, field, param_refs)} = {_param_access(obj, base, param_refs)} + {offset};", seen)
                continue

            # obj->field < obj->base + N: make the relation true
            # without dereferencing outside the allocation.
            m = re.fullmatch(r'(\w+)->(\w+)\s*<\s*(\w+)->(\w+)\s*\+\s*([A-Za-z_]\w*|\d+)', part)
            if m and m.group(1) == m.group(3) and m.group(1) in params_by_name:
                obj, field, _same_obj, base, _offset = m.groups()
                _append_unique(lines, f"{_param_access(obj, field, param_refs)} = {_param_access(obj, base, param_refs)};", seen)
                continue

            # \valid_read(pkt->data + (0 .. pkt->len - 1))
            m = re.search(r'\\valid_read\(\s*(\w+)->data\s*\+\s*\(0\s*\.\.\s*\1->len\s*-\s*1\)\s*\)', part)
            if m:
                obj = m.group(1)
                if obj in params_by_name:
                    _append_unique(lines, f"if ({obj}->len == 0) {obj}->len = 1;", seen)
                    _append_unique(lines, f"memset({obj}->data, 0, {obj}->len);", seen)
                continue

            # ((Header *)obj->data)->type == CONST, generic for any casted
            # struct field at param->data.
            m = re.fullmatch(
                r'\(\(\s*([A-Za-z_]\w*)\s*\*\s*\)(\w+)->data\)->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)',
                part,
            )
            if m:
                cast_type, obj, field, value = m.groups()
                if obj in params_by_name:
                    value = _rewrite_value(value, param_args)
                    data_expr = _param_access(obj, "data", param_refs)
                    _append_unique(lines, f"(({cast_type} *){data_expr})->{field} = {value};", seen)
                continue

    return lines


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
            new_line = re.sub(rf"\b{re.escape(name)}\b", arg, new_line)
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


def _decoded_field_aliases(body: str) -> dict[str, tuple[str, str, str]]:
    decoded: dict[str, tuple[str, str, str]] = {}
    for m in re.finditer(
        r"\b(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)\s+(\w+)\s*=\s*(ns_ntohs|ntohs|ns_ntohl|ntohl)\s*\(\s*(\w+)->(\w+)\s*\)\s*;",
        body,
    ):
        local, fn, alias, field = m.groups()
        decoded[local] = (fn, alias, field)
    return decoded


def _good_path_setup_from_source(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
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
            false_value = rhs
            _append_unique(lines, f"{_cast_field_expr(cast_type, expr, field)} = {encode_fn}({false_value});", seen)

    return lines


def _loop_table_candidates(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
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
    good_setup = _good_path_setup_from_source(body, aliases, decoded_aliases)

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


def _host_to_network_fn(decode_fn: str) -> str:
    if decode_fn in {"ns_ntohs", "ntohs"}:
        return "ns_htons"
    if decode_fn in {"ns_ntohl", "ntohl"}:
        return "ns_htonl"
    return ""


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
    body = _source_for_branch_shaping(source_text, func.name)
    if not body:
        return []
    if shaping_features is None:
        shaping_features = set(DEFAULT_SHAPING_FEATURES)

    params = {p.name: p for p in func.params}
    aliases = _cast_aliases(body, params)
    decoded_aliases = _decoded_field_aliases(body)
    checksum_fixups = _checksum_recompute_lines(body, aliases)
    candidates: list[BranchCandidate] = []
    seen_names: set[str] = set()

    def add_candidate(name: str, setup: list[str]) -> None:
        safe = _safe_c_name(name)
        if safe in seen_names:
            return
        seen_names.add(safe)
        candidates.append(BranchCandidate(safe, setup))

    def rhs_visible_in_harness(rhs: str) -> bool:
        if "->" not in rhs:
            return True
        base = rhs.split("->", 1)[0].strip()
        return base in params or base in aliases

    if "casted-fields" in shaping_features:
        for alias, (cast_type, expr) in aliases.items():
            backing_setup = _cast_alias_backing_setup(alias, cast_type, expr, params)
            for m in re.finditer(rf"switch\s*\(\s*{re.escape(alias)}->(\w+)\s*\)", body):
                field = m.group(1)
                for case in re.findall(r"\bcase\s+([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*:", body[m.end():]):
                    setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = {case};"]
                    if re.search(rf"{re.escape(alias)}->code\s*==\s*0", body):
                        setup.append(f"(({cast_type} *){expr})->code = 0;")
                    setup.extend(checksum_fixups)
                    add_candidate(f"source_case_{case}", setup)
                setup = [*backing_setup, f"(({cast_type} *){expr})->{field} = 255;"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_default_{field}", setup)

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{_cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_{value}", setup)

            for field, value in re.findall(
                rf"{re.escape(alias)}->(\w+)\s*!=\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                setup = [*backing_setup, f"{_cast_field_expr(cast_type, expr, field)} = {value};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_eq_{value}", setup)
                setup = [*backing_setup, f"{_cast_field_expr(cast_type, expr, field)} = {_nonmatching_value(value)};"]
                setup.extend(checksum_fixups)
                add_candidate(f"source_{alias}_{field}_ne_{value}", setup)

    if "byte-order" in shaping_features:
        for local, (decode_fn, alias, field) in decoded_aliases.items():
            if alias not in aliases:
                continue
            cast_type, expr = aliases[alias]
            encode_fn = _host_to_network_fn(decode_fn)
            if not encode_fn:
                continue
            for op, rhs in re.findall(
                rf"\b{re.escape(local)}\s*(<|>|<=|>=|==|!=)\s*([A-Za-z_]\w*(?:->\w+)?|0x[0-9a-fA-F]+|\d+)",
                body,
            ):
                if not rhs_visible_in_harness(rhs):
                    continue
                if op == "<":
                    true_value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
                    false_value = rhs
                elif op == ">":
                    true_value = f"(({rhs}) + 1)"
                    false_value = rhs
                elif op == "!=":
                    true_value = _nonmatching_value(rhs)
                    false_value = rhs
                elif op == "==":
                    true_value = rhs
                    false_value = _nonmatching_value(rhs)
                elif op == "<=":
                    true_value = rhs
                    false_value = f"(({rhs}) + 1)"
                else:
                    true_value = rhs
                    false_value = f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
                target = _cast_field_expr(cast_type, expr, field)
                add_candidate(
                    f"source_{local}_{_safe_c_name(op)}_{_safe_c_name(rhs)}",
                    [f"{target} = {encode_fn}({true_value});"],
                )
                add_candidate(
                    f"source_{local}_not_{_safe_c_name(op)}_{_safe_c_name(rhs)}",
                    [f"{target} = {encode_fn}({false_value});"],
                )

    for candidate in _loop_table_candidates(body, aliases, decoded_aliases, type_catalog, shaping_features):
        if candidate.name in seen_names:
            continue
        seen_names.add(candidate.name)
        candidates.append(candidate)

    return candidates


def _is_assigns_nothing(assigns: str) -> bool:
    """Check if assigns clause is \\nothing."""
    return "nothing" in assigns or "\\nothing" in assigns


# ── Body generators ───────────────────────────────────────────────────────────

def _is_void_star(p: CFunction | CParam) -> bool:
    """Check if a param is `void *`."""
    return p.base_type == 'void' and p.is_pointer


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


def _gen_valid_setup_body(
    func: CFunction,
    valid_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None = None,
    type_catalog: CTypeCatalog | None = None,
    function_decls: dict[str, CFunction] | None = None,
    extra_setup: list[str] | None = None,
    shaping_features: set[str] | None = None,
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

    if func.return_type.strip() not in ("void", ""):
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
                        )
                        preamble = [*preamble, *candidate.preamble]
                        lines.extend(_emit_yaml_function(
                            func, shaped_behavior, body, outputs, cleanup, ktest_dir, preamble, source_include_names
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
