from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir.model import ArraySubscript, BinaryOp, Expr, FieldAccess, FunctionIR, IfStmt, LoopStmt, SourceLocation
from ..ir.naming import safe_name
from ..ir.render import value_expr
from ..ir.walk import walk_statements
from .candidates import BranchCandidate, BranchFact, display_source_location


@dataclass(frozen=True)
class LookupLoop:
    array_expr:    str
    field:         str
    key_expr:      str
    slot_expr:     str
    element_type:  str | None = None
    bound_expr:    str | None = None
    loc:           SourceLocation | None = None


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
        setup_0 = _materialize_pointer_slots(lookup, [0])
        preamble = _pointer_slot_preamble(lookup)
        candidates.append(BranchCandidate(
            f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_hit",
            [
                *setup_0,
                *_bound_setup(lookup, 1),
                f"{lookup.slot_expr} = {lookup.key_expr};",
            ],
            preamble=preamble,
            source_location=source_location,
            target_branch=f"table {lookup.array_expr}.{lookup.field} hit",
            origin="ir",
            branch_facts=[BranchFact(lookup.slot_expr, "==", lookup.key_expr)],
        ))
        candidates.append(BranchCandidate(
            f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_miss",
            [
                *setup_0,
                *_bound_setup(lookup, 1),
                f"{lookup.slot_expr} = 0;",
            ],
            preamble=preamble,
            source_location=source_location,
            target_branch=f"table {lookup.array_expr}.{lookup.field} miss",
            origin="ir",
            branch_facts=[BranchFact(lookup.slot_expr, "!=", lookup.key_expr)],
        ))
        if lookup.bound_expr and _assignable_text(lookup.bound_expr):
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_full",
                [
                    *setup_0,
                    f"{lookup.bound_expr} = 1;",
                    f"{lookup.slot_expr} = {lookup.key_expr};",
                ],
                preamble=preamble,
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} full",
                origin="ir",
                branch_facts=[BranchFact(lookup.slot_expr, "==", lookup.key_expr)],
            ))
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_first_free",
                [
                    *setup_0,
                    f"{lookup.bound_expr} = 1;",
                    f"{lookup.slot_expr} = 0;",
                ],
                preamble=preamble,
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} first_free",
                origin="ir",
                branch_facts=[BranchFact(lookup.slot_expr, "!=", lookup.key_expr)],
            ))
            duplicate_slot = _indexed_slot_text(lookup, 1)
            setup_01 = _materialize_pointer_slots(lookup, [0, 1])
            candidates.append(BranchCandidate(
                f"ir_table_{safe_name(lookup.array_expr)}_{lookup.field}_duplicate",
                [
                    *setup_01,
                    f"{lookup.bound_expr} = 2;",
                    f"{lookup.slot_expr} = {lookup.key_expr};",
                    f"{duplicate_slot} = {lookup.key_expr};",
                ],
                preamble=preamble,
                source_location=source_location,
                target_branch=f"table {lookup.array_expr}.{lookup.field} duplicate",
                origin="ir",
                branch_facts=[
                    BranchFact(lookup.slot_expr, "==", lookup.key_expr),
                    BranchFact(duplicate_slot, "==", lookup.key_expr),
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
        array_expr, field, slot_expr, element_type = left
        return LookupLoop(array_expr, field, right, slot_expr, element_type, bound_expr, loc)

    right_field = _array_field(expr.right)
    left_text = value_expr(expr.left)
    if right_field and left_text:
        array_expr, field, slot_expr, element_type = right_field
        return LookupLoop(array_expr, field, left_text, slot_expr, element_type, bound_expr, loc)
    return None


def _loop_bound(loop: LoopStmt) -> str | None:
    condition = loop.condition
    if not isinstance(condition, BinaryOp) or condition.op not in {"<", "<="}:
        return None
    return value_expr(condition.right)


def _array_field(expr: Expr) -> tuple[str, str, str, str | None] | None:
    if not isinstance(expr, FieldAccess) or not isinstance(expr.base, ArraySubscript):
        return None
    array_expr = value_expr(expr.base.base)
    if not array_expr:
        return None
    element_type = _pointer_element_type(expr.base.c_type)
    operator = "->" if element_type else "."
    slot_expr = f"{array_expr}[0]{operator}{expr.field}"
    return array_expr, expr.field, slot_expr, element_type


def _indexed_slot_text(lookup: LookupLoop, index: int) -> str:
    if "[0]" in lookup.slot_expr:
        return lookup.slot_expr.replace("[0]", f"[{index}]", 1)
    return f"{lookup.array_expr}[{index}].{lookup.field}"


def _pointer_element_type(c_type: str | None) -> str | None:
    if not isinstance(c_type, str) or "*" not in c_type:
        return None
    return c_type.replace("*", "").strip() or None


def _materialize_pointer_slots(lookup: LookupLoop, indexes: list[int]) -> list[str]:
    if not lookup.element_type:
        return []
    lines: list[str] = []
    base_name = safe_name(f"{lookup.array_expr}_{lookup.field}", "slot")
    for index in indexes:
        name = f"{base_name}_{index}"
        lines.extend([
            f"{lookup.element_type} *{name} = malloc(sizeof(*{name}));",
            f"if (!{name}) return 0;",
            f"memset({name}, 0, sizeof(*{name}));",
            f"{lookup.array_expr}[{index}] = {name};",
        ])
    return lines


def _pointer_slot_preamble(lookup: LookupLoop) -> list[str]:
    if not lookup.element_type:
        return []
    return ["#include <stdlib.h>"]


def _bound_setup(lookup: LookupLoop, value: int) -> list[str]:
    if lookup.bound_expr and _assignable_text(lookup.bound_expr):
        return [f"{lookup.bound_expr} = {value};"]
    return []


def _flatten_and(expr: Expr) -> list[Expr]:
    if isinstance(expr, BinaryOp) and expr.op == "&&":
        return [*_flatten_and(expr.left), *_flatten_and(expr.right)]
    return [expr]


def _assignable_text(value: str) -> bool:
    if not value or not all(ch.isalnum() or ch in "_->." for ch in value):
        return False
    if value.isdigit():
        return False
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        return False
    return True
