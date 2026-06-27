from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceLocation:
    file: str | None = None
    line: int | None = None
    col:  int | None = None

    def display(self) -> str | None:
        if self.file and self.line is not None:
            if self.col is not None:
                return f"{self.file}:{self.line}:{self.col}"
            return f"{self.file}:{self.line}"
        if self.line is not None:
            if self.col is not None:
                return f"{self.line}:{self.col}"
            return str(self.line)
        return None


class Expr:
    """Base class for typed expression facts."""


@dataclass(frozen=True)
class UnknownExpr(Expr):
    kind: str
    c_type: str | None = None


@dataclass(frozen=True)
class VarRef(Expr):
    name: str
    c_type: str | None = None


@dataclass(frozen=True)
class IntLiteral(Expr):
    value: int
    c_type: str | None = None


@dataclass(frozen=True)
class UnaryOp(Expr):
    op:      str
    operand: Expr
    c_type:  str | None = None


@dataclass(frozen=True)
class AddressOf(Expr):
    operand: Expr
    c_type:  str | None = None


@dataclass(frozen=True)
class Dereference(Expr):
    operand: Expr
    c_type:  str | None = None


@dataclass(frozen=True)
class BinaryOp(Expr):
    op:    str
    left:  Expr
    right: Expr
    c_type: str | None = None


@dataclass(frozen=True)
class FieldAccess(Expr):
    base:  Expr
    field: str
    c_type: str | None = None


@dataclass(frozen=True)
class ArraySubscript(Expr):
    base:  Expr
    index: Expr
    c_type: str | None = None


@dataclass(frozen=True)
class CallExpr(Expr):
    callee: str
    args:   list[Expr] = field(default_factory=list)
    c_type: str | None = None


@dataclass(frozen=True)
class CastExpr(Expr):
    target_type: str | None
    expr:        Expr
    kind:        str | None = None
    c_type:      str | None = None


class Stmt:
    """Base class for typed statement facts."""


@dataclass(frozen=True)
class IfStmt(Stmt):
    condition: Expr
    body:      list[Stmt] = field(default_factory=list)
    loc:       SourceLocation | None = None


@dataclass(frozen=True)
class ExprStmt(Stmt):
    expr: Expr
    loc:  SourceLocation | None = None


@dataclass(frozen=True)
class AssignmentStmt(Stmt):
    target: Expr
    value:  Expr
    loc:    SourceLocation | None = None


@dataclass(frozen=True)
class DeclarationStmt(Stmt):
    name:    str
    c_type:  str | None = None
    init:    Expr | None = None
    loc:     SourceLocation | None = None


@dataclass(frozen=True)
class ReturnStmt(Stmt):
    value: Expr | None = None
    loc:   SourceLocation | None = None


@dataclass(frozen=True)
class LoopStmt(Stmt):
    kind:      str
    condition: Expr | None = None
    body:      list[Stmt] = field(default_factory=list)
    loc:       SourceLocation | None = None


@dataclass(frozen=True)
class SwitchCase:
    value: int | str
    body: list[Stmt] = field(default_factory=list)


@dataclass(frozen=True)
class SwitchStmt(Stmt):
    selector:    Expr
    cases:       list[SwitchCase] = field(default_factory=list)
    has_default: bool = False
    body:        list[Stmt] = field(default_factory=list)
    loc:         SourceLocation | None = None
    default_body: list[Stmt] = field(default_factory=list)


@dataclass(frozen=True)
class FunctionIR:
    name:       str
    statements: list[Stmt] = field(default_factory=list)
