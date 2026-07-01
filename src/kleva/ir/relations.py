from __future__ import annotations

from .model import CastExpr, Expr, IntLiteral, UnaryOp


def int_value(expr: Expr) -> int | None:
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, CastExpr):
        return int_value(expr.expr)
    if isinstance(expr, UnaryOp) and expr.op == "-":
        value = int_value(expr.operand)
        if value is not None:
            return -value
    return None


def flipped_relation(op: str) -> str:
    return {
        "<":  ">",
        "<=": ">=",
        ">":  "<",
        ">=": "<=",
    }.get(op, op)


def negated_relation(op: str) -> str:
    return {
        "==": "!=",
        "!=": "==",
        ">":  "<=",
        ">=": "<",
        "<":  ">=",
        "<=": ">",
    }[op]


def relation_name(op: str) -> str:
    return {
        "==": "eq",
        "!=": "ne",
        ">":  "gt",
        ">=": "ge",
        "<":  "lt",
        "<=": "le",
    }[op]
