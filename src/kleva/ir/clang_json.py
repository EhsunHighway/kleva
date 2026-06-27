from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from .model import (
    BinaryOp,
    CallExpr,
    CastExpr,
    DeclarationStmt,
    Expr,
    ExprStmt,
    FieldAccess,
    ArraySubscript,
    FunctionIR,
    IfStmt,
    IntLiteral,
    AddressOf,
    AssignmentStmt,
    Dereference,
    LoopStmt,
    ReturnStmt,
    SourceLocation,
    Stmt,
    SwitchCase,
    SwitchStmt,
    UnaryOp,
    UnknownExpr,
    VarRef,
)


def parse_translation_unit(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> dict[str, FunctionIR]:
    """
    Parse C with clang's JSON AST dump and return KLEVA's small typed IR.

    This is intentionally a narrow extractor, not a second C parser. Clang owns
    syntax and type parsing; KLEVA only translates the facts shapers need.
    """
    ast = _load_ast(source_path, include_dirs, extra_args, clang)
    return function_irs_from_ast(ast, source_path)


def parse_function_decl_map(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> dict[str, CFunction]:
    ast = _load_ast(source_path, include_dirs, extra_args, clang)
    return function_decl_map_from_ast(ast)


def parse_header_function_decls(
    header_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> list[CFunction]:
    ast = _load_ast(header_path, include_dirs, extra_args, clang)
    return header_function_decls_from_ast(ast, header_path)


def parse_type_catalog(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> CTypeCatalog:
    ast = _load_ast(source_path, include_dirs, extra_args, clang)
    return type_catalog_from_ast(ast)


def parse_translation_unit_with_decls(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> tuple[dict[str, FunctionIR], dict[str, CFunction]]:
    ast = _load_ast(source_path, include_dirs, extra_args, clang)
    return function_irs_from_ast(ast, source_path), function_decl_map_from_ast(ast)


def parse_translation_unit_with_decls_and_types(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> tuple[dict[str, FunctionIR], dict[str, CFunction], CTypeCatalog]:
    ast = _load_ast(source_path, include_dirs, extra_args, clang)
    return (
        function_irs_from_ast(ast, source_path),
        function_decl_map_from_ast(ast),
        type_catalog_from_ast(ast),
    )


def _load_ast(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
) -> dict[str, Any]:
    include_dirs = include_dirs or []
    extra_args = extra_args or []
    cmd = [
        clang,
        "-Xclang",
        "-ast-dump=json",
        "-fsyntax-only",
        *(f"-I{d}" for d in include_dirs),
        *extra_args,
        str(source_path),
    ]
    raw = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    return json.loads(raw)


def function_irs_from_ast(ast: dict[str, Any], source_path: str | Path) -> dict[str, FunctionIR]:
    functions: dict[str, FunctionIR] = {}
    source_name = str(source_path)
    for node in _walk(ast):
        if node.get("kind") == "FunctionDecl" and node.get("name"):
            statements: list[Stmt] = []
            for child in node.get("inner", []) or []:
                _collect_statements(child, statements, source_name)
            functions[node["name"]] = FunctionIR(node["name"], statements)
    return functions


def function_decl_map_from_ast(ast: dict[str, Any]) -> dict[str, CFunction]:
    functions: dict[str, CFunction] = {}
    for node in _walk(ast):
        if node.get("kind") != "FunctionDecl" or not node.get("name"):
            continue
        decl = _function_decl(node)
        if decl is not None:
            functions[decl.name] = decl
    return functions


def header_function_decls_from_ast(ast: dict[str, Any], header_path: str | Path) -> list[CFunction]:
    target = str(Path(header_path).resolve())
    functions: list[CFunction] = []
    seen: set[str] = set()
    for node in _walk(ast):
        if node.get("kind") != "FunctionDecl" or not node.get("name"):
            continue
        if node.get("storageClass") == "static":
            continue
        if not _node_is_from_target(node, target):
            continue
        decl = _function_decl(node)
        if decl is None or decl.name in seen:
            continue
        if decl.name.upper() == decl.name or decl.name.startswith("_"):
            continue
        seen.add(decl.name)
        functions.append(decl)
    return functions


def type_catalog_from_ast(ast: dict[str, Any]) -> CTypeCatalog:
    catalog = CTypeCatalog()
    typedef_aliases: dict[str, str] = {}

    for node in _walk(ast):
        if node.get("kind") != "TypedefDecl" or node.get("isImplicit"):
            continue
        alias = node.get("name")
        if not isinstance(alias, str) or not alias:
            continue
        qual_type = _qual_type(node)
        if _is_function_pointer_type(qual_type):
            catalog.function_pointers[alias] = _function_pointer_typedef(alias, qual_type)
            continue
        target = _record_name_from_type(qual_type)
        if target:
            typedef_aliases[alias] = target

    for node in _walk(ast):
        if node.get("kind") != "RecordDecl" or node.get("isImplicit"):
            continue
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        aliases = {name, *(alias for alias, target in typedef_aliases.items() if target == name)}
        if node.get("completeDefinition"):
            catalog.complete_structs.update(aliases)
            fields = _record_fields(node)
            for type_name in aliases:
                catalog.struct_fields[type_name] = fields
        else:
            catalog.opaque_structs.update(aliases)

    catalog.opaque_structs.difference_update(catalog.complete_structs)
    return catalog


def _record_fields(node: dict[str, Any]) -> dict[str, CParam]:
    fields: dict[str, CParam] = {}
    for index, child in enumerate(node.get("inner", []) or []):
        if not isinstance(child, dict) or child.get("kind") != "FieldDecl":
            continue
        param = _field_decl(child, index)
        if param is not None:
            fields[param.name] = param
    return fields


def _field_decl(node: dict[str, Any], index: int) -> CParam | None:
    raw_type = _qual_type(node)
    if not raw_type:
        return None
    name = node.get("name") or f"field{index}"
    return _param_from_type(name, raw_type)


def _function_pointer_typedef(name: str, qual_type: str | None) -> CFunctionPointerTypedef:
    return_type, param_types = _function_pointer_parts(qual_type)
    params = [
        _param_from_type(f"arg{index}", param_type)
        for index, param_type in enumerate(param_types)
        if param_type != "void"
    ]
    return CFunctionPointerTypedef(name, return_type, params)


def _is_function_pointer_type(qual_type: str | None) -> bool:
    return bool(qual_type and "(*)" in qual_type)


def _function_pointer_parts(qual_type: str | None) -> tuple[str, list[str]]:
    if not qual_type or "(*)" not in qual_type:
        return "void", []
    return_type, rest = qual_type.split("(*)", 1)
    params_raw = rest.strip()
    if params_raw.startswith("(") and params_raw.endswith(")"):
        params_raw = params_raw[1:-1]
    params = _split_type_list(params_raw)
    return return_type.strip() or "void", params


def _split_type_list(text: str) -> list[str]:
    if not text.strip():
        return []
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in text:
        if ch == "," and depth == 0:
            item = "".join(cur).strip()
            if item:
                out.append(item)
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


def _record_name_from_type(qual_type: str | None) -> str | None:
    if not qual_type:
        return None
    match = re.search(r"\bstruct\s+([A-Za-z_]\w*)\b", qual_type)
    return match.group(1) if match else None


def _function_decl(node: dict[str, Any]) -> CFunction | None:
    name = node.get("name")
    qual_type = _qual_type(node)
    if not isinstance(name, str) or not name:
        return None
    return_type = _function_return_type(qual_type)
    if return_type is None:
        return None
    params: list[CParam] = []
    for index, child in enumerate(node.get("inner", []) or []):
        if isinstance(child, dict) and child.get("kind") == "ParmVarDecl":
            param = _param_decl(child, index)
            if param is not None:
                params.append(param)
    return CFunction(
        name=name,
        return_type=return_type,
        return_base=_base_type(return_type),
        return_is_pointer="*" in return_type,
        params=params,
    )


def _function_return_type(qual_type: str | None) -> str | None:
    if not qual_type:
        return None
    if "(" not in qual_type:
        return qual_type.strip()
    return qual_type.split("(", 1)[0].strip()


def _param_decl(node: dict[str, Any], index: int) -> CParam | None:
    raw_type = _qual_type(node)
    if not raw_type:
        return None
    name = node.get("name") or f"arg{index}"
    return _param_from_type(name, raw_type)


def _param_from_type(name: str, raw_type: str) -> CParam:
    return CParam(
        name=name,
        raw_type=f"{raw_type} {name}".strip(),
        base_type=_base_type(raw_type),
        is_pointer="*" in raw_type,
        is_const=bool(re.search(r"\bconst\b", raw_type)),
        is_array=bool(re.search(r"\[[^\]]*\]", raw_type)),
        array_size=_array_size(raw_type),
        pointer_depth=raw_type.count("*"),
    )


def _base_type(raw_type: str) -> str:
    clean = re.sub(r"\bconst\b|\bvolatile\b|\brestrict\b", " ", raw_type)
    clean = re.sub(r"\[[^\]]*\]", " ", clean)
    clean = clean.replace("*", " ")
    tokens = [token for token in clean.split() if token not in {"unsigned", "signed", "long", "short", "struct"}]
    if not tokens:
        return "int"
    return tokens[-1]


def _array_size(raw_type: str) -> int:
    match = re.search(r"\[(\d+)\]", raw_type)
    return int(match.group(1)) if match else 0


def _walk(node: Any):
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("inner", []) or []:
        yield from _walk(child)


def _collect_statements(node: dict[str, Any], out: list[Stmt], source_path: str | None = None) -> None:
    kind = node.get("kind")
    if kind == "IfStmt":
        children = node.get("inner", []) or []
        if children:
            out.append(IfStmt(_expr(children[0]), _if_body(node, source_path), _source_location(node, source_path)))
    elif kind == "DeclStmt":
        loc = _source_location(node, source_path)
        for child in node.get("inner", []) or []:
            if isinstance(child, dict) and child.get("kind") == "VarDecl":
                out.append(_declaration_stmt(child, loc or _source_location(child, source_path)))
        return
    elif kind == "VarDecl":
        out.append(_declaration_stmt(node, _source_location(node, source_path)))
    elif kind == "CallExpr":
        out.append(ExprStmt(_expr(node), _source_location(node, source_path)))
    elif kind == "BinaryOperator" and node.get("opcode") == "=":
        children = node.get("inner", []) or []
        if len(children) >= 2:
            out.append(AssignmentStmt(_expr(children[0]), _expr(children[1]), _source_location(node, source_path)))
    elif kind == "ReturnStmt":
        children = node.get("inner", []) or []
        out.append(ReturnStmt(_expr(children[0]) if children else None, _source_location(node, source_path)))
    elif kind in {"ForStmt", "WhileStmt", "DoStmt"}:
        out.append(LoopStmt(_loop_kind(kind), _loop_condition(node), _loop_body(node, source_path), _source_location(node, source_path)))
    elif kind == "SwitchStmt":
        children = node.get("inner", []) or []
        selector = _expr(children[0]) if children else UnknownExpr("missing-switch-selector")
        cases, default_body = _switch_case_bodies(node, source_path)
        out.append(SwitchStmt(
            selector,
            cases,
            _has_default_case(node),
            _switch_body(node, source_path),
            _source_location(node, source_path),
            default_body,
        ))
        return

    for child in node.get("inner", []) or []:
        _collect_statements(child, out, source_path)


def _switch_cases(node: dict[str, Any]) -> list[SwitchCase]:
    cases: list[SwitchCase] = []
    for child in _walk(node):
        if child.get("kind") != "CaseStmt":
            continue
        values = child.get("inner", []) or []
        if not values:
            continue
        cases.append(SwitchCase(_literal_value(values[0])))
    return cases


def _switch_case_bodies(node: dict[str, Any], source_path: str | None) -> tuple[list[SwitchCase], list[Stmt]]:
    cases: list[SwitchCase] = []
    default_body: list[Stmt] = []
    for child in _walk(node):
        kind = child.get("kind")
        if kind == "CaseStmt":
            values = child.get("inner", []) or []
            if not values:
                continue
            body: list[Stmt] = []
            for body_child in values[1:]:
                _collect_statements(body_child, body, source_path)
            cases.append(SwitchCase(_literal_value(values[0]), body))
        elif kind == "DefaultStmt":
            body = []
            for body_child in child.get("inner", []) or []:
                _collect_statements(body_child, body, source_path)
            default_body.extend(body)
    return cases, default_body


def _has_default_case(node: dict[str, Any]) -> bool:
    return any(child.get("kind") == "DefaultStmt" for child in _walk(node))


def _switch_body(node: dict[str, Any], source_path: str | None) -> list[Stmt]:
    body: list[Stmt] = []
    children = node.get("inner", []) or []
    for child in children[1:]:
        _collect_statements(child, body, source_path)
    return body


def _source_location(node: dict[str, Any], source_path: str | None) -> SourceLocation | None:
    raw = node.get("loc")
    if not isinstance(raw, dict) or "line" not in raw:
        raw_range = node.get("range")
        if isinstance(raw_range, dict):
            raw = raw_range.get("begin")
    if isinstance(raw, dict) and "line" not in raw:
        raw_range = node.get("range")
        if isinstance(raw_range, dict):
            end = raw_range.get("end")
            if isinstance(end, dict) and "line" in end:
                raw = end
    if not isinstance(raw, dict) or "line" not in raw:
        return None
    return SourceLocation(
        file=raw.get("file") or source_path,
        line=raw.get("line"),
        col=raw.get("col"),
    )


def _node_is_from_target(node: dict[str, Any], target: str) -> bool:
    saw_location = False
    for key in ("loc",):
        raw = node.get(key)
        if isinstance(raw, dict):
            saw_location = True
            if isinstance(raw.get("includedFrom"), dict):
                return False
            raw_file = raw.get("file")
            if isinstance(raw_file, str):
                return str(Path(raw_file).resolve()) == target
    raw_range = node.get("range")
    if isinstance(raw_range, dict):
        for key in ("begin", "end"):
            raw = raw_range.get(key)
            if isinstance(raw, dict):
                saw_location = True
                if isinstance(raw.get("includedFrom"), dict):
                    return False
                raw_file = raw.get("file")
                if isinstance(raw_file, str):
                    return str(Path(raw_file).resolve()) == target
    return saw_location


def _qual_type(node: dict[str, Any]) -> str | None:
    raw_type = node.get("type")
    if isinstance(raw_type, dict):
        qual = raw_type.get("qualType")
        if isinstance(qual, str):
            return qual
    return None


def _declaration_stmt(node: dict[str, Any], loc: SourceLocation | None) -> DeclarationStmt:
    return DeclarationStmt(
        node.get("name", ""),
        _qual_type(node),
        _var_decl_init(node),
        loc,
    )


def _var_decl_init(node: dict[str, Any]) -> Expr | None:
    for child in node.get("inner", []) or []:
        if not isinstance(child, dict):
            continue
        if child.get("kind") in {"BuiltinType", "PointerType", "RecordType", "ElaboratedType"}:
            continue
        expr = _expr(child)
        if not isinstance(expr, UnknownExpr):
            return expr
    return None


def _loop_kind(kind: str) -> str:
    return {
        "ForStmt": "for",
        "WhileStmt": "while",
        "DoStmt": "do",
    }.get(kind, kind)


def _loop_condition(node: dict[str, Any]) -> Expr | None:
    for child in node.get("inner", []) or []:
        if not isinstance(child, dict):
            continue
        expr = _expr(child)
        if not isinstance(expr, UnknownExpr):
            return expr
    return None


def _loop_body(node: dict[str, Any], source_path: str | None) -> list[Stmt]:
    return _compound_body(node, source_path)


def _if_body(node: dict[str, Any], source_path: str | None) -> list[Stmt]:
    children = [child for child in node.get("inner", []) or [] if isinstance(child, dict)]
    if len(children) < 2:
        return []
    body_node = children[1]
    if body_node.get("kind") == "CompoundStmt":
        return _compound_body(node, source_path)
    body: list[Stmt] = []
    _collect_statements(body_node, body, source_path)
    return body


def _compound_body(node: dict[str, Any], source_path: str | None) -> list[Stmt]:
    body: list[Stmt] = []
    for child in node.get("inner", []) or []:
        if isinstance(child, dict) and child.get("kind") == "CompoundStmt":
            for nested in child.get("inner", []) or []:
                _collect_statements(nested, body, source_path)
            break
    return body


def _expr(node: dict[str, Any]) -> Expr:
    kind = node.get("kind", "Unknown")
    children = [c for c in node.get("inner", []) or [] if isinstance(c, dict)]
    c_type = _qual_type(node)

    if kind in {"ImplicitCastExpr", "ParenExpr", "ConstantExpr"} and children:
        return _expr(children[0])
    if kind in {"CStyleCastExpr", "CXXFunctionalCastExpr", "CXXStaticCastExpr", "CXXReinterpretCastExpr"}:
        return CastExpr(c_type, _expr(children[0]) if children else UnknownExpr("missing-cast-expr"), node.get("castKind"), c_type)
    if kind == "DeclRefExpr":
        return VarRef(_decl_ref_name(node), c_type)
    if kind == "IntegerLiteral":
        return IntLiteral(int(node.get("value", "0"), 0), c_type)
    if kind == "UnaryOperator":
        operand = _expr(children[0]) if children else UnknownExpr("missing-unary-operand")
        op = node.get("opcode", "?")
        if op == "&":
            return AddressOf(operand, c_type)
        if op == "*":
            return Dereference(operand, c_type)
        return UnaryOp(op, operand, c_type)
    if kind == "BinaryOperator":
        left = _expr(children[0]) if children else UnknownExpr("missing-left")
        right = _expr(children[1]) if len(children) > 1 else UnknownExpr("missing-right")
        return BinaryOp(node.get("opcode", "?"), left, right, c_type)
    if kind == "MemberExpr":
        base = _expr(children[0]) if children else UnknownExpr("missing-member-base")
        return FieldAccess(base, node.get("name", ""), c_type)
    if kind == "ArraySubscriptExpr":
        base = _expr(children[0]) if children else UnknownExpr("missing-array-base")
        index = _expr(children[1]) if len(children) > 1 else UnknownExpr("missing-array-index")
        return ArraySubscript(base, index, c_type)
    if kind == "CallExpr":
        callee = _call_name(children[0]) if children else ""
        args = [_expr(c) for c in children[1:]]
        return CallExpr(callee, args, c_type)

    return UnknownExpr(kind, c_type)


def _decl_ref_name(node: dict[str, Any]) -> str:
    ref = node.get("referencedDecl")
    if isinstance(ref, dict) and ref.get("name"):
        return ref["name"]
    return node.get("name", "")


def _call_name(node: dict[str, Any]) -> str:
    expr = _expr(node)
    if isinstance(expr, VarRef):
        return expr.name
    text = _expr_text(expr)
    if text:
        return text
    return ""


def _expr_text(expr: Expr) -> str | None:
    if isinstance(expr, VarRef):
        return expr.name
    if isinstance(expr, AddressOf):
        inner = _expr_text(expr.operand)
        if inner:
            return f"&{inner}"
    if isinstance(expr, Dereference):
        inner = _expr_text(expr.operand)
        if inner:
            return f"*{inner}"
    if isinstance(expr, CastExpr):
        inner = _expr_text(expr.expr)
        if inner:
            target = expr.target_type or "void"
            return f"(({target}){inner})"
    if isinstance(expr, FieldAccess):
        base = _expr_text(expr.base)
        if base:
            return f"{base}->{expr.field}"
    if isinstance(expr, ArraySubscript):
        base = _expr_text(expr.base)
        index = _expr_text(expr.index)
        if base and index:
            return f"{base}[{index}]"
    if isinstance(expr, IntLiteral):
        return str(expr.value)
    return None


def _literal_value(node: dict[str, Any]) -> int | str:
    expr = _expr(node)
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, VarRef):
        return expr.name
    text = _expr_text(expr)
    if text:
        return text
    return node.get("kind", "unknown")
