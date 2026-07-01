from __future__ import annotations

from collections.abc import Iterator

from .model import BreakStmt, ContinueStmt, FunctionIR, IfStmt, LoopStmt, ReturnStmt, Stmt, SwitchStmt


def walk_statements(func: FunctionIR) -> Iterator[Stmt]:
    for stmt in func.statements:
        yield from walk_statement(stmt)


def walk_if_statements(func: FunctionIR) -> Iterator[IfStmt]:
    for stmt in walk_statements(func):
        if isinstance(stmt, IfStmt):
            yield stmt


def body_has_return(statements: list[Stmt]) -> bool:
    return any(isinstance(stmt, ReturnStmt) for stmt in statements)


def body_has_terminator(statements: list[Stmt]) -> bool:
    return any(isinstance(stmt, (BreakStmt, ContinueStmt, ReturnStmt)) for stmt in statements)


def walk_statement(stmt: Stmt) -> Iterator[Stmt]:
    yield stmt
    for child in child_statements(stmt):
        yield from walk_statement(child)


def child_statements(stmt: Stmt) -> list[Stmt]:
    if isinstance(stmt, IfStmt):
        return stmt.body
    if isinstance(stmt, LoopStmt):
        return stmt.body
    if isinstance(stmt, SwitchStmt):
        return [
            *stmt.body,
            *(child for case in stmt.cases for child in case.body),
            *stmt.default_body,
        ]
    return []
