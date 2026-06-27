from __future__ import annotations

from .model import (
    AddressOf,
    ArraySubscript,
    BinaryOp,
    CallExpr,
    CastExpr,
    Dereference,
    Expr,
    FieldAccess,
    IntLiteral,
    UnaryOp,
    VarRef,
)


def assignable_expr(expr: Expr) -> str | None:
    if isinstance(expr, VarRef):
        return expr.name
    if isinstance(expr, CastExpr):
        return None
    if isinstance(expr, Dereference):
        inner = assignable_expr(expr.operand) or value_expr(expr.operand)
        if inner:
            return f"*{inner}"
    if isinstance(expr, FieldAccess):
        base = assignable_expr(expr.base) or value_expr(expr.base)
        if base is None:
            return None
        return f"{base}{_field_operator(expr.base)}{expr.field}"
    if isinstance(expr, ArraySubscript):
        base = assignable_expr(expr.base) or value_expr(expr.base)
        index = value_expr(expr.index)
        if base is None or index is None:
            return None
        return f"{base}[{index}]"
    return None


def value_expr(expr: Expr) -> str | None:
    if isinstance(expr, IntLiteral):
        return str(expr.value)
    if isinstance(expr, VarRef):
        return expr.name
    if isinstance(expr, AddressOf):
        inner = assignable_expr(expr.operand)
        if inner:
            return f"&{inner}"
    if isinstance(expr, Dereference):
        inner = assignable_expr(expr.operand)
        if inner:
            return f"*{inner}"
    if isinstance(expr, CastExpr):
        inner = value_expr(expr.expr) or assignable_expr(expr.expr)
        if inner and expr.target_type:
            return f"(({expr.target_type}){inner})"
    if isinstance(expr, FieldAccess):
        return assignable_expr(expr)
    if isinstance(expr, ArraySubscript):
        return assignable_expr(expr)
    if isinstance(expr, UnaryOp):
        inner = value_expr(expr.operand)
        if inner is not None:
            return f"{expr.op}{inner}"
    if isinstance(expr, BinaryOp):
        left = value_expr(expr.left)
        right = value_expr(expr.right)
        if left is not None and right is not None:
            return f"({left} {expr.op} {right})"
    if isinstance(expr, CallExpr):
        args = [value_expr(arg) for arg in expr.args]
        if all(arg is not None for arg in args):
            return f"{expr.callee}({', '.join(args)})"
    return None


def is_pointer_expr(expr: Expr) -> bool:
    c_type = getattr(expr, "c_type", None)
    return isinstance(c_type, str) and "*" in c_type


def _field_operator(base: Expr) -> str:
    c_type = getattr(base, "c_type", None)
    if isinstance(c_type, str) and c_type:
        return "->" if "*" in c_type else "."
    if isinstance(base, CastExpr) and base.target_type:
        return "->" if "*" in base.target_type else "."
    return "->"
