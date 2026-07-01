from __future__ import annotations

import re

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import AssignmentStmt, DeclarationStmt, IfStmt, LoopStmt, ReturnStmt, Stmt, SwitchStmt
from ..ir.render import assignable_expr, value_expr
from .candidates import PostStateFact


def post_state_facts_from_direct_assignments(statements: list[Stmt]) -> list[PostStateFact]:
    """
    Extract conservative post-state facts from a known-reachable statement list.

    This only records straight-line assignments in that list. Nested branches,
    loops, and switches are skipped until the caller can provide a stronger
    path fact for those bodies.
    """
    facts: list[PostStateFact] = []
    seen: set[PostStateFact] = set()
    aliases: AliasMap = {}
    local_names: set[str] = set()
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt):
            local_names.add(stmt.name)
            record_alias(stmt, aliases)
            continue
        if isinstance(stmt, ReturnStmt):
            break
        if isinstance(stmt, (IfStmt, LoopStmt, SwitchStmt)):
            continue
        if not isinstance(stmt, AssignmentStmt):
            continue

        target = assignable_expr(resolve_aliases(stmt.target, aliases))
        value = value_expr(resolve_aliases(stmt.value, aliases))
        if target is None or value is None:
            continue
        if _expr_references_local_root(target, local_names) or _expr_references_local_root(value, local_names):
            continue
        fact = PostStateFact(target, "==", value)
        if fact in seen:
            continue
        seen.add(fact)
        facts.append(fact)
    return facts


def _expr_references_local_root(expr: str, local_names: set[str]) -> bool:
    if not local_names:
        return False
    root_match = re.match(r"\s*\(*\s*([A-Za-z_]\w*)\b", expr)
    if root_match and root_match.group(1) in local_names:
        return True
    for name in local_names:
        escaped = re.escape(name)
        if re.search(rf"(?<![A-Za-z0-9_]){escaped}\s*(?:->|\.|\[|\))", expr):
            return True
    return False
