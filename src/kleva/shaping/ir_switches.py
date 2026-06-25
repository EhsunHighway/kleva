from __future__ import annotations

from ..ir.model import FieldAccess, FunctionIR, SwitchStmt, VarRef
from .candidates import BranchCandidate


def state_switch_candidates_from_ir(func: FunctionIR) -> list[BranchCandidate]:
    """
    Generate candidates from typed IR switch facts.

    This recognizes a generic state-machine shape without naming the domain:
    switch over a direct field access, e.g. `switch (obj->state)`.
    """
    candidates: list[BranchCandidate] = []
    seen: set[str] = set()
    for stmt in func.statements:
        if not isinstance(stmt, SwitchStmt):
            continue
        selector = stmt.selector
        if not isinstance(selector, FieldAccess) or not isinstance(selector.base, VarRef):
            continue
        for case in stmt.cases:
            name = f"ir_case_{selector.field}_{case.value}"
            if name in seen:
                continue
            seen.add(name)
            candidates.append(BranchCandidate(
                name,
                [f"{selector.base.name}->{selector.field} = {case.value};"],
            ))
    return candidates
