from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .model import (
    BinaryOp,
    CallExpr,
    Expr,
    FieldAccess,
    FunctionIR,
    IfStmt,
    IntLiteral,
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
    ast = json.loads(raw)
    functions: dict[str, FunctionIR] = {}
    for node in _walk(ast):
        if node.get("kind") == "FunctionDecl" and node.get("name"):
            statements: list[Stmt] = []
            for child in node.get("inner", []) or []:
                _collect_statements(child, statements)
            functions[node["name"]] = FunctionIR(node["name"], statements)
    return functions


def _walk(node: Any):
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("inner", []) or []:
        yield from _walk(child)


def _collect_statements(node: dict[str, Any], out: list[Stmt]) -> None:
    kind = node.get("kind")
    if kind == "IfStmt":
        children = node.get("inner", []) or []
        if children:
            out.append(IfStmt(_expr(children[0])))
    elif kind == "SwitchStmt":
        children = node.get("inner", []) or []
        selector = _expr(children[0]) if children else UnknownExpr("missing-switch-selector")
        out.append(SwitchStmt(selector, _switch_cases(node)))

    for child in node.get("inner", []) or []:
        _collect_statements(child, out)


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


def _expr(node: dict[str, Any]) -> Expr:
    kind = node.get("kind", "Unknown")
    children = [c for c in node.get("inner", []) or [] if isinstance(c, dict)]

    if kind in {"ImplicitCastExpr", "ParenExpr", "ConstantExpr"} and children:
        return _expr(children[0])
    if kind == "DeclRefExpr":
        return VarRef(_decl_ref_name(node))
    if kind == "IntegerLiteral":
        return IntLiteral(int(node.get("value", "0"), 0))
    if kind == "UnaryOperator":
        return UnaryOp(node.get("opcode", "?"), _expr(children[0]) if children else UnknownExpr("missing-unary-operand"))
    if kind == "BinaryOperator":
        left = _expr(children[0]) if children else UnknownExpr("missing-left")
        right = _expr(children[1]) if len(children) > 1 else UnknownExpr("missing-right")
        return BinaryOp(node.get("opcode", "?"), left, right)
    if kind == "MemberExpr":
        base = _expr(children[0]) if children else UnknownExpr("missing-member-base")
        return FieldAccess(base, node.get("name", ""))
    if kind == "CallExpr":
        callee = _call_name(children[0]) if children else ""
        args = [_expr(c) for c in children[1:]]
        return CallExpr(callee, args)

    return UnknownExpr(kind)


def _decl_ref_name(node: dict[str, Any]) -> str:
    ref = node.get("referencedDecl")
    if isinstance(ref, dict) and ref.get("name"):
        return ref["name"]
    return node.get("name", "")


def _call_name(node: dict[str, Any]) -> str:
    expr = _expr(node)
    if isinstance(expr, VarRef):
        return expr.name
    return ""


def _literal_value(node: dict[str, Any]) -> int | str:
    expr = _expr(node)
    if isinstance(expr, IntLiteral):
        return expr.value
    return repr(expr)
