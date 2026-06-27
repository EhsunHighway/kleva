from __future__ import annotations

from dataclasses import dataclass

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import AssignmentStmt, CallExpr, DeclarationStmt, FieldAccess, FunctionIR, Stmt, VarRef
from ..ir.render import assignable_expr
from ..ir.walk import child_statements


@dataclass(frozen=True)
class DecodedFieldAlias:
    decode_fn: str
    target:    str


def decoded_field_aliases_from_ir(func: FunctionIR) -> dict[str, DecodedFieldAlias]:
    aliases: dict[str, DecodedFieldAlias] = {}
    _record_decoded_aliases(func.statements, aliases, {})
    return aliases


def _record_decoded_aliases(
    statements: list[Stmt],
    decoded_aliases: dict[str, DecodedFieldAlias],
    expr_aliases: AliasMap,
) -> None:
    current_expr_aliases = dict(expr_aliases)
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt) and stmt.init is not None:
            _record_named_value(stmt.name, stmt.init, decoded_aliases, current_expr_aliases)
            record_alias(stmt, current_expr_aliases)
        elif isinstance(stmt, AssignmentStmt) and isinstance(stmt.target, VarRef):
            _record_named_value(stmt.target.name, stmt.value, decoded_aliases, current_expr_aliases)
            record_alias(stmt, current_expr_aliases)
        _record_decoded_aliases(child_statements(stmt), decoded_aliases, dict(current_expr_aliases))


def _record_named_value(
    name: str,
    value,
    decoded_aliases: dict[str, DecodedFieldAlias],
    expr_aliases: AliasMap,
) -> None:
    decoded = _decoded_call(resolve_aliases(value, expr_aliases))
    if decoded is not None:
        decoded_aliases[name] = decoded
        return
    if isinstance(value, VarRef) and value.name in decoded_aliases:
        decoded_aliases[name] = decoded_aliases[value.name]


def _decoded_call(value) -> DecodedFieldAlias | None:
    if not isinstance(value, CallExpr) or "ntoh" not in value.callee or len(value.args) != 1:
        return None
    target = assignable_expr(value.args[0])
    if target is None:
        return None
    return DecodedFieldAlias(value.callee, target)
