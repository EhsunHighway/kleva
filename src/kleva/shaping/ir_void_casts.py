from __future__ import annotations

import re

from ..ir.model import CastExpr, DeclarationStmt, FunctionIR, VarRef
from ..ir.walk import walk_statements


def void_param_cast_types_from_ir(
    func: FunctionIR,
    void_param_names: set[str],
) -> dict[str, str]:
    if not void_param_names:
        return {}

    casts: dict[str, str] = {}
    for stmt in walk_statements(func):
        if not isinstance(stmt, DeclarationStmt):
            continue
        init = stmt.init
        if not isinstance(init, CastExpr) or not isinstance(init.expr, VarRef):
            continue
        if init.expr.name not in void_param_names:
            continue
        cast_type = _base_type_from_pointer_cast(init.target_type)
        if cast_type:
            casts.setdefault(init.expr.name, cast_type)
    return casts


def _base_type_from_pointer_cast(target_type: str | None) -> str | None:
    if not target_type or "*" not in target_type:
        return None
    clean = re.sub(r"\bconst\b|\bvolatile\b|\brestrict\b", " ", target_type)
    clean = clean.replace("*", " ")
    tokens = [token for token in clean.split() if token != "struct"]
    if not tokens:
        return None
    return tokens[-1]
