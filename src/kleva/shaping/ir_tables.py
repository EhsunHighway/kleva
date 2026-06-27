from __future__ import annotations

from dataclasses import dataclass

from ..ir.model import ArraySubscript, BinaryOp, Expr, FieldAccess, FunctionIR, IfStmt, LoopStmt, SourceLocation
from ..ir.naming import safe_name
from ..ir.render import value_expr
from ..ir.walk import walk_statements
from .candidates import BranchCandidate, BranchFact, display_source_location


@dataclass(frozen=True)
class LookupLoop:
    array_expr: str
    field:      str
    key_expr:   str
    bound_expr: str | None = None
    loc:        SourceLocation | None = None


def lookup_loops_from_ir(func: FunctionIR) -> list[LookupLoop]:
    lookups: list[LookupLoop] = []
    seen: set[tuple[str, str, str]] = set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, LoopStmt):
            continue
        for nested in stmt.body:
            if not isinstance(nested, IfStmt):
                continue
            for condition in _flatten_and(nested.condition):
                lookup = _lookup_from_comparison(condition, _loop_bound(stmt), stmt.loc)
                if not lookup:
                    continue
                key = (lookup.array_expr, lookup.field, lookup.key_expr)
                if key in seen:
                    continue
                seen.add(key)
                lookups.append(lookup)
    return lookups


def table_candidates_from_ir(func: FunctionIR) -> list[BranchCandidate]:
    candidates: list[BranchCandidate] = []
    for lookup in lookup_loops_from_ir(func):
        source_location = display_source_location(lookup.loc, f"ir:{func.name}:table:{safe_name(lookup.array_expr)}_{lookup.field}")
        candidates.append(BranchCandidate(
            f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_hit",
            [f"{lookup.array_expr}[0].{lookup.field} = {lookup.key_expr};"],
            source_location=source_location,
            target_branch=f"table {lookup.array_expr}.{lookup.field} hit",
            origin="ir",
            branch_facts=[BranchFact(f"{lookup.array_expr}[0].{lookup.field}", "==", lookup.key_expr)],
        ))
        candidates.append(BranchCandidate(
            f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_miss",
            [f"{lookup.array_expr}[0].{lookup.field} = 0;"],
            source_location=source_location,
            target_branch=f"table {lookup.array_expr}.{lookup.field} miss",
            origin="ir",
            branch_facts=[BranchFact(f"{lookup.array_expr}[0].{lookup.field}", "!=", lookup.key_expr)],
        ))
        if lookup.bound_expr and _assignable_text(lookup.bound_expr):
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_full",
                [
                    f"{lookup.bound_expr} = 1;",
                    f"{lookup.array_expr}[0].{lookup.field} = {lookup.key_expr};",
                ],
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} full",
                origin="ir",
                branch_facts=[BranchFact(f"{lookup.array_expr}[0].{lookup.field}", "==", lookup.key_expr)],
            ))
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_first_free",
                [
                    f"{lookup.bound_expr} = 1;",
                    f"{lookup.array_expr}[0].{lookup.field} = 0;",
                ],
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} first_free",
                origin="ir",
                branch_facts=[BranchFact(f"{lookup.array_expr}[0].{lookup.field}", "!=", lookup.key_expr)],
            ))
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_duplicate",
                [
                    f"{lookup.bound_expr} = 2;",
                    f"{lookup.array_expr}[0].{lookup.field} = {lookup.key_expr};",
                    f"{lookup.array_expr}[1].{lookup.field} = {lookup.key_expr};",
                ],
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} duplicate",
                origin="ir",
                branch_facts=[
                    BranchFact(f"{lookup.array_expr}[0].{lookup.field}", "==", lookup.key_expr),
                    BranchFact(f"{lookup.array_expr}[1].{lookup.field}", "==", lookup.key_expr),
                ],
            ))
    return candidates


def _lookup_from_comparison(
    expr: Expr,
    bound_expr: str | None,
    loc: SourceLocation | None,
) -> LookupLoop | None:
    if not isinstance(expr, BinaryOp) or expr.op != "==":
        return None
    left = _array_field(expr.left)
    right = value_expr(expr.right)
    if left and right:
        array_expr, field = left
        return LookupLoop(array_expr, field, right, bound_expr, loc)

    right_field = _array_field(expr.right)
    left_text = value_expr(expr.left)
    if right_field and left_text:
        array_expr, field = right_field
        return LookupLoop(array_expr, field, left_text, bound_expr, loc)
    return None


def _loop_bound(loop: LoopStmt) -> str | None:
    condition = loop.condition
    if not isinstance(condition, BinaryOp) or condition.op not in {"<", "<="}:
        return None
    return value_expr(condition.right)


def _array_field(expr: Expr) -> tuple[str, str] | None:
    if not isinstance(expr, FieldAccess) or not isinstance(expr.base, ArraySubscript):
        return None
    array_expr = value_expr(expr.base.base)
    if not array_expr:
        return None
    return array_expr, expr.field


def _flatten_and(expr: Expr) -> list[Expr]:
    if isinstance(expr, BinaryOp) and expr.op == "&&":
        return [*_flatten_and(expr.left), *_flatten_and(expr.right)]
    return [expr]


def _assignable_text(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in "_->." for ch in value)
