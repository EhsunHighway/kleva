from __future__ import annotations

from typing import Dict

from .model import (
    AddressOf,
    ArraySubscript,
    AssignmentStmt,
    BinaryOp,
    CallExpr,
    CastExpr,
    DeclarationStmt,
    Dereference,
    Expr,
    FieldAccess,
    IntLiteral,
    Stmt,
    UnaryOp,
    VarRef,
)


AliasMap = Dict[str, Expr]


def record_alias(stmt: Stmt, aliases: AliasMap) -> None:
    if isinstance(stmt, DeclarationStmt) and stmt.init is not None:
        if is_aliasable_expr(stmt.init):
            aliases[stmt.name] = stmt.init
        return
    if isinstance(stmt, AssignmentStmt) and isinstance(stmt.target, VarRef):
        if is_aliasable_expr(stmt.value):
            aliases[stmt.target.name] = stmt.value


def resolve_aliases(expr: Expr, aliases: AliasMap, seen: set[str] | None = None) -> Expr:
    seen = seen or set()
    if isinstance(expr, VarRef) and expr.name in aliases and expr.name not in seen:
        return resolve_aliases(aliases[expr.name], aliases, {*seen, expr.name})
    if isinstance(expr, UnaryOp):
        return UnaryOp(expr.op, resolve_aliases(expr.operand, aliases, seen), expr.c_type)
    if isinstance(expr, AddressOf):
        return AddressOf(resolve_aliases(expr.operand, aliases, seen), expr.c_type)
    if isinstance(expr, Dereference):
        return Dereference(resolve_aliases(expr.operand, aliases, seen), expr.c_type)
    if isinstance(expr, BinaryOp):
        return BinaryOp(expr.op, resolve_aliases(expr.left, aliases, seen), resolve_aliases(expr.right, aliases, seen), expr.c_type)
    if isinstance(expr, FieldAccess):
        return FieldAccess(resolve_aliases(expr.base, aliases, seen), expr.field, expr.c_type)
    if isinstance(expr, ArraySubscript):
        return ArraySubscript(resolve_aliases(expr.base, aliases, seen), resolve_aliases(expr.index, aliases, seen), expr.c_type)
    if isinstance(expr, CallExpr):
        return CallExpr(expr.callee, [resolve_aliases(arg, aliases, seen) for arg in expr.args], expr.c_type)
    if isinstance(expr, CastExpr):
        return CastExpr(expr.target_type, resolve_aliases(expr.expr, aliases, seen), expr.kind, expr.c_type)
    return expr


def is_aliasable_expr(expr: Expr) -> bool:
    if isinstance(expr, (VarRef, FieldAccess, ArraySubscript, AddressOf, Dereference)):
        return True
    if isinstance(expr, CastExpr):
        return is_aliasable_expr(expr.expr)
    if isinstance(expr, UnaryOp):
        return is_aliasable_expr(expr.operand)
    if isinstance(expr, BinaryOp):
        return is_aliasable_expr(expr.left) or is_aliasable_expr(expr.right)
    if isinstance(expr, CallExpr):
        return False
    if isinstance(expr, IntLiteral):
        return False
    return False
