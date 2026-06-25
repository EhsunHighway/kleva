from __future__ import annotations

import re
from pathlib import Path

from .model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog


def strip_comments(text: str) -> str:
    """Remove ACSL annotations and C comments."""
    text = re.sub(r"/\*@.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def split_call_args(args: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in args:
        if ch == "," and depth == 0:
            part = "".join(cur).strip()
            if part:
                out.append(part)
            cur = []
            continue
        cur.append(ch)
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def parse_param(raw: str, index: int = 0) -> CParam | None:
    raw = raw.strip()
    if not raw or raw in ("void", "..."):
        return None

    is_const = bool(re.search(r"\bconst\b", raw))
    is_pointer = "*" in raw
    is_array = bool(re.search(r"\[\d*\]", raw))
    array_size = 0
    if is_array:
        m = re.search(r"\[(\d+)\]", raw)
        array_size = int(m.group(1)) if m else 0

    clean = re.sub(r"\bconst\b|\bvolatile\b|\brestrict\b", "", raw)
    clean = re.sub(r"\[[^\]]*\]", " ", clean)
    clean = clean.replace("*", " ")
    tokens = clean.split()

    if not tokens:
        return None

    type_words = {
        "void", "char", "int", "float", "double", "size_t", "ssize_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "unsigned", "signed", "long", "short", "struct",
    }

    if len(tokens) == 1 or tokens[-1] in type_words:
        name = f"arg{index}"
        type_toks = tokens
    else:
        name = tokens[-1]
        type_toks = tokens[:-1]

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
    return parse_function_decls(header_path.read_text())


def parse_function_decls(raw_text: str) -> list[CFunction]:
    """Extract function declarations from C text."""
    text = strip_comments(raw_text)
    text = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", text)

    text = re.sub(r"#[^\n]*", "", text)
    text = re.sub(r"typedef\s+struct\s*\w*\s*\{[^}]*\}\s*\w+\s*;", "", text, flags=re.DOTALL)
    text = re.sub(r"typedef\s+[^;]+;", "", text, flags=re.DOTALL)
    text = re.sub(r"struct\s+\w+\s*;", "", text)
    text = re.sub(r"\s+", " ", text)

    funcs: list[CFunction] = []
    pattern = re.compile(
        r"((?:const\s+)?(?:struct\s+)?\w+(?:\s*\*+)?)"
        r"\s*"
        r"(\w+)\s*"
        r"\(([^)]*)\)\s*;"
    )

    for m in pattern.finditer(text):
        ret_raw = m.group(1).strip()
        fname = m.group(2).strip()
        args_raw = m.group(3).strip()

        if fname.upper() == fname or fname.startswith("_"):
            continue

        params: list[CParam] = []
        if args_raw and args_raw != "void":
            for i, p_raw in enumerate(args_raw.split(",")):
                p = parse_param(p_raw, i)
                if p:
                    params.append(p)

        ret_is_ptr = "*" in ret_raw
        ret_clean = re.sub(r"[\*\s]", "", ret_raw.replace("const", "").replace("struct", "")).strip()

        funcs.append(CFunction(
            name=fname,
            return_type=ret_raw,
            return_base=ret_clean,
            return_is_pointer=ret_is_ptr,
            params=params,
        ))

    return funcs


def function_decl_map(text: str) -> dict[str, CFunction]:
    decls = {f.name: f for f in parse_function_decls(text)}
    decls.update(parse_function_definitions(text))
    return decls


def parse_function_definitions(raw_text: str) -> dict[str, CFunction]:
    text = strip_comments(raw_text)
    text = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", text)
    pattern = re.compile(
        r"\b(?:static\s+)?(?:inline\s+)?"
        r"((?:const\s+)?(?:struct\s+)?\w+(?:\s*\*+)?)"
        r"\s*(\w+)\s*\(([^)]*)\)\s*\{",
        flags=re.DOTALL,
    )

    funcs: dict[str, CFunction] = {}
    for m in pattern.finditer(text):
        ret_raw, fname, args_raw = m.groups()
        if fname in {"if", "for", "while", "switch"}:
            continue
        params: list[CParam] = []
        args_raw = args_raw.strip()
        if args_raw and args_raw != "void":
            for i, p_raw in enumerate(split_call_args(args_raw)):
                p = parse_param(p_raw, i)
                if p:
                    params.append(p)

        ret_is_ptr = "*" in ret_raw
        ret_clean = re.sub(r"[\*\s]", "", ret_raw.replace("const", "").replace("struct", "")).strip()
        funcs[fname] = CFunction(
            name=fname,
            return_type=ret_raw.strip(),
            return_base=ret_clean,
            return_is_pointer=ret_is_ptr,
            params=params,
        )
    return funcs


def build_type_catalog(text: str) -> CTypeCatalog:
    """
    Infer complete-vs-opaque struct types from visible C declarations.

    Complete structs can be stack allocated. Opaque structs must be handled
    through visible constructors or passed as NULL.
    """
    catalog = CTypeCatalog()
    text = re.sub(r"__attribute__\s*\(\([^)]*\)\)", "", text)
    field_text = strip_comments(text)

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
            p = parse_param(field_raw, i)
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
            p = parse_param(field_raw, i)
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
                p = parse_param(raw_param, i)
                if p:
                    params.append(p)
        catalog.function_pointers[name] = CFunctionPointerTypedef(
            name=name,
            return_type=" ".join(return_type.split()),
            params=params,
        )

    catalog.opaque_structs.difference_update(catalog.complete_structs)
    return catalog
