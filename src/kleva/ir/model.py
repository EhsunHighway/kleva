from __future__ import annotations

from dataclasses import dataclass, field


class Expr:
    """Base class for typed expression facts."""


@dataclass(frozen=True)
class UnknownExpr(Expr):
    kind: str


@dataclass(frozen=True)
class VarRef(Expr):
    name: str


@dataclass(frozen=True)
class IntLiteral(Expr):
    value: int


@dataclass(frozen=True)
class UnaryOp(Expr):
    op:      str
    operand: Expr


@dataclass(frozen=True)
class BinaryOp(Expr):
    op:    str
    left:  Expr
    right: Expr


@dataclass(frozen=True)
class FieldAccess(Expr):
    base:  Expr
    field: str


@dataclass(frozen=True)
class CallExpr(Expr):
    callee: str
    args:   list[Expr] = field(default_factory=list)


class Stmt:
    """Base class for typed statement facts."""


@dataclass(frozen=True)
class IfStmt(Stmt):
    condition: Expr


@dataclass(frozen=True)
class SwitchCase:
    value: int | str


@dataclass(frozen=True)
class SwitchStmt(Stmt):
    selector: Expr
    cases:    list[SwitchCase] = field(default_factory=list)


@dataclass(frozen=True)
class FunctionIR:
    name:       str
    statements: list[Stmt] = field(default_factory=list)

