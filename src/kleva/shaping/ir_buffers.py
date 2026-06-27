from __future__ import annotations

from ..ir.model import (
    AddressOf,
    ArraySubscript,
    BinaryOp,
    CallExpr,
    CastExpr,
    Dereference,
    Expr,
    ExprStmt,
    FieldAccess,
    FunctionIR,
    IfStmt,
    ReturnStmt,
    Stmt,
    SwitchStmt,
    UnaryOp,
    VarRef,
)
from ..ir.walk import walk_statements


def len_data_buffer_params_from_ir(func: FunctionIR, param_names: set[str]) -> set[str]:
    fields_by_param: dict[str, set[str]] = {name: set() for name in param_names}

    for stmt in walk_statements(func):
        _collect_len_data_fields_from_stmt(stmt, fields_by_param)

    return {
        name for name, fields in fields_by_param.items()
        if {"len", "data"}.issubset(fields)
    }


def param_uses_len_data_buffer_from_ir(func: FunctionIR, param_name: str) -> bool:
    return param_name in len_data_buffer_params_from_ir(func, {param_name})


def _collect_len_data_fields_from_stmt(
    stmt: Stmt,
    fields_by_param: dict[str, set[str]],
) -> None:
    for value in vars(stmt).values():
        _collect_len_data_fields_from_value(value, fields_by_param)


def _collect_len_data_fields_from_value(
    value,
    fields_by_param: dict[str, set[str]],
) -> None:
    if isinstance(value, FieldAccess):
        root = _root_name(value.base)
        if root in fields_by_param and value.field in {"len", "data"}:
            fields_by_param[root].add(value.field)
        _collect_len_data_fields_from_value(value.base, fields_by_param)
        return

    if isinstance(value, (AddressOf, Dereference, UnaryOp, CastExpr)):
        _collect_len_data_fields_from_value(value.operand if hasattr(value, "operand") else value.expr, fields_by_param)
        return

    if isinstance(value, BinaryOp):
        _collect_len_data_fields_from_value(value.left, fields_by_param)
        _collect_len_data_fields_from_value(value.right, fields_by_param)
        return

    if isinstance(value, ArraySubscript):
        _collect_len_data_fields_from_value(value.base, fields_by_param)
        _collect_len_data_fields_from_value(value.index, fields_by_param)
        return

    if isinstance(value, CallExpr):
        for arg in value.args:
            _collect_len_data_fields_from_value(arg, fields_by_param)
        return

    if isinstance(value, (IfStmt, ExprStmt, ReturnStmt, SwitchStmt)):
        _collect_len_data_fields_from_stmt(value, fields_by_param)
        return

    if isinstance(value, list):
        for item in value:
            _collect_len_data_fields_from_value(item, fields_by_param)
        return

    if isinstance(value, Expr):
        for child in vars(value).values():
            _collect_len_data_fields_from_value(child, fields_by_param)


def _root_name(expr: Expr) -> str | None:
    while isinstance(expr, (CastExpr, AddressOf, Dereference)):
        expr = expr.expr if isinstance(expr, CastExpr) else expr.operand
    if isinstance(expr, VarRef):
        return expr.name
    if isinstance(expr, FieldAccess):
        return _root_name(expr.base)
    if isinstance(expr, ArraySubscript):
        return _root_name(expr.base)
    return None
