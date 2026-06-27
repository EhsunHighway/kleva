from __future__ import annotations

from ..ir.model import BinaryOp, FunctionIR, IfStmt, IntLiteral, ReturnStmt, UnaryOp, VarRef
from ..ir.walk import walk_if_statements


def accepts_null_param_from_ir(func: FunctionIR, param_name: str) -> bool:
    """
    Return true when typed IR shows a parameter-specific NULL guard.

    This is used for no-contract functions. A pointer parameter alone is not
    enough evidence; the function must have a recognizable guard whose body
    returns after seeing that parameter as NULL.
    """
    for stmt in walk_if_statements(func):
        if not _body_returns(stmt):
            continue
        if _condition_accepts_null(stmt.condition, param_name):
            return True
    return False


def _body_returns(stmt: IfStmt) -> bool:
    return any(isinstance(child, ReturnStmt) for child in stmt.body)


def _condition_accepts_null(expr, param_name: str) -> bool:
    if isinstance(expr, UnaryOp) and expr.op == "!":
        return isinstance(expr.operand, VarRef) and expr.operand.name == param_name

    if isinstance(expr, BinaryOp) and expr.op in {"||", "&&"}:
        return (
            _condition_accepts_null(expr.left, param_name) or
            _condition_accepts_null(expr.right, param_name)
        )

    if isinstance(expr, BinaryOp) and expr.op == "==":
        return (
            _is_param(expr.left, param_name) and _is_zero(expr.right)
        ) or (
            _is_zero(expr.left) and _is_param(expr.right, param_name)
        )

    return False


def _is_param(expr, param_name: str) -> bool:
    return isinstance(expr, VarRef) and expr.name == param_name


def _is_zero(expr) -> bool:
    return isinstance(expr, IntLiteral) and expr.value == 0
