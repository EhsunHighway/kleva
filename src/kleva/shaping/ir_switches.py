from __future__ import annotations

import re

from ..ir.aliases import record_alias, resolve_aliases
from ..ir.model import (
    AssignmentStmt,
    DeclarationStmt,
    Expr,
    FieldAccess,
    FunctionIR,
    IfStmt,
    IntLiteral,
    LoopStmt,
    CallExpr,
    AddressOf,
    ArraySubscript,
    Stmt,
    SwitchStmt,
    ReturnStmt,
    VarRef,
)
from ..ir.naming import safe_name
from ..ir.render import assignable_expr
from .candidates import BranchCandidate, BranchFact, ObjectPathFact, display_source_location, object_path_facts_from_expr
from .ir_conditions import IrConditionOps, condition_setup_alternatives


def state_switch_candidates_from_ir(
    func: FunctionIR,
    condition_ops: IrConditionOps | None = None,
    helper_irs: dict[str, FunctionIR] | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    """
    Generate candidates from typed IR switch facts.

    This recognizes a generic state-machine shape without naming the domain:
    switch over a direct field access, e.g. `switch (obj->state)`.
    """
    candidates: list[BranchCandidate] = []
    seen: set[str] = set()
    aliases: dict[str, Expr] = {}
    lookup_aliases: dict[str, str] = {}
    for index, stmt in enumerate(func.statements):
        record_alias(stmt, aliases)
        _record_lookup_result_alias(stmt, aliases, lookup_aliases, helper_irs or {}, helper_params or {})
        if not isinstance(stmt, SwitchStmt):
            continue
        selector = resolve_aliases(stmt.selector, aliases)
        selector_text = assignable_expr(selector)
        selector_text = _rewrite_lookup_text(selector_text, lookup_aliases)
        selector_leaf = _selector_leaf(selector)
        if selector_text is None or selector_leaf is None:
            continue
        object_paths = object_path_facts_from_expr(selector)
        for case in stmt.cases:
            case_name = safe_name(str(case.value), "switch")
            name = f"ir_case_{safe_name(selector_leaf, 'switch')}_{case_name}"
            case_setup = [f"{selector_text} = {case.value};"]
            case_fact = BranchFact(selector_text, "case", str(case.value))
            if name in seen:
                continue
            seen.add(name)
            candidates.append(BranchCandidate(
                name,
                case_setup,
                source_location=display_source_location(stmt.loc, f"ir:{func.name}:switch[{index}]"),
                target_branch=f"switch {selector_text} case {case.value}",
                origin="ir",
                object_paths=object_paths,
                branch_facts=[case_fact],
            ))
            if condition_ops is not None:
                guard_candidates = _case_guard_candidates(
                    func,
                    stmt,
                    index,
                    selector_text,
                    selector_leaf,
                    case.value,
                    case.body,
                    case_setup,
                    case_fact,
                    object_paths,
                    aliases,
                    lookup_aliases,
                    condition_ops,
                )
                for guard_candidate in guard_candidates:
                    if guard_candidate.name in seen:
                        continue
                    seen.add(guard_candidate.name)
                    candidates.append(guard_candidate)
            for value in _assigned_values_to_selector(stmt, selector, aliases):
                if value == case.value:
                    continue
                transition_name = f"ir_transition_{safe_name(selector_leaf, 'switch')}_{case_name}_to_{safe_name(str(value), 'switch')}"
                if transition_name in seen:
                    continue
                seen.add(transition_name)
                candidates.append(BranchCandidate(
                    transition_name,
                    [f"{selector_text} = {case.value};"],
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:switch[{index}]"),
                    target_branch=f"transition {selector_text} {case.value} -> {value}",
                    origin="ir",
                    object_paths=object_paths,
                    branch_facts=[BranchFact(selector_text, "case", str(case.value))],
                    witness_outputs=True,
                ))
        if stmt.has_default and stmt.cases:
            value = _default_value([case.value for case in stmt.cases])
            name = f"ir_default_{safe_name(selector_leaf, 'switch')}"
            if name not in seen:
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    [f"{selector_text} = {value};"],
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:switch[{index}]"),
                    target_branch=f"switch {selector_text} default",
                    origin="ir",
                    object_paths=object_paths,
                    branch_facts=[BranchFact(selector_text, "default", str(value))],
                ))
    return candidates


def _record_lookup_result_alias(
    stmt:           Stmt,
    aliases:        dict[str, Expr],
    lookup_aliases: dict[str, str],
    helper_irs:     dict[str, FunctionIR],
    helper_params:  dict[str, tuple[str, ...]],
) -> None:
    if not isinstance(stmt, DeclarationStmt):
        return
    if not isinstance(stmt.init, CallExpr):
        return
    helper_ir = helper_irs.get(stmt.init.callee)
    param_names = helper_params.get(stmt.init.callee, ())
    if helper_ir is None or len(param_names) != len(stmt.init.args):
        return
    returned_slot = _returned_array_slot_from_helper(helper_ir)
    if returned_slot is None:
        return
    arg_by_param = {
        name: resolve_aliases(arg, aliases)
        for name, arg in zip(param_names, stmt.init.args)
    }
    mapped_slot = _replace_param_refs(returned_slot, arg_by_param)
    slot_text = assignable_expr(mapped_slot)
    if slot_text is not None:
        lookup_aliases[stmt.name] = slot_text


def _returned_array_slot_from_helper(helper_ir: FunctionIR) -> Expr | None:
    return _returned_array_slot_from_statements(helper_ir.statements, {})


def _returned_array_slot_from_statements(statements: list[Stmt], aliases: dict[str, Expr]) -> Expr | None:
    current_aliases = dict(aliases)
    for stmt in statements:
        record_alias(stmt, current_aliases)
        if isinstance(stmt, ReturnStmt) and stmt.value is not None:
            value = resolve_aliases(stmt.value, current_aliases)
            if isinstance(value, AddressOf) and isinstance(value.operand, ArraySubscript):
                return ArraySubscript(value.operand.base, IntLiteral(0), value.operand.c_type)
        if isinstance(stmt, IfStmt):
            found = _returned_array_slot_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
        elif isinstance(stmt, LoopStmt):
            found = _returned_array_slot_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
        elif isinstance(stmt, SwitchStmt):
            found = _returned_array_slot_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
            for case in stmt.cases:
                found = _returned_array_slot_from_statements(case.body, dict(current_aliases))
                if found is not None:
                    return found
            found = _returned_array_slot_from_statements(stmt.default_body, dict(current_aliases))
            if found is not None:
                return found
    return None


def _replace_param_refs(expr: Expr, arg_by_param: dict[str, Expr]) -> Expr:
    if isinstance(expr, VarRef) and expr.name in arg_by_param:
        return arg_by_param[expr.name]
    if isinstance(expr, FieldAccess):
        return FieldAccess(_replace_param_refs(expr.base, arg_by_param), expr.field, expr.c_type)
    if isinstance(expr, ArraySubscript):
        return ArraySubscript(_replace_param_refs(expr.base, arg_by_param), _replace_param_refs(expr.index, arg_by_param), expr.c_type)
    if isinstance(expr, AddressOf):
        return AddressOf(_replace_param_refs(expr.operand, arg_by_param), expr.c_type)
    return expr


def _rewrite_lookup_text(text: str | None, lookup_aliases: dict[str, str]) -> str | None:
    if text is None:
        return None
    for name, slot_text in lookup_aliases.items():
        text = re.sub(rf"\b{re.escape(name)}->", f"{slot_text}.", text)
        if text == name:
            text = slot_text
    return text


def _rewrite_lookup_lines(lines: list[str], lookup_aliases: dict[str, str]) -> list[str]:
    return [
        rewritten
        for line in lines
        if (rewritten := _rewrite_lookup_text(line, lookup_aliases)) is not None
    ]


def _rewrite_lookup_branch_facts(facts: list[BranchFact], lookup_aliases: dict[str, str]) -> list[BranchFact]:
    out: list[BranchFact] = []
    for fact in facts:
        target = _rewrite_lookup_text(fact.target, lookup_aliases) or fact.target
        value = _rewrite_lookup_text(fact.value, lookup_aliases) or fact.value
        out.append(BranchFact(target, fact.relation, value))
    return out


def _case_guard_candidates(
    func:          FunctionIR,
    switch_stmt:   SwitchStmt,
    switch_index:  int,
    selector_text: str,
    selector_leaf: str,
    case_value:    int | str,
    case_body:     list[Stmt],
    case_setup:    list[str],
    case_fact:     BranchFact,
    object_paths:  list[ObjectPathFact],
    aliases:       dict[str, Expr],
    lookup_aliases: dict[str, str],
    condition_ops: IrConditionOps,
) -> list[BranchCandidate]:
    candidates: list[BranchCandidate] = []
    case_name = safe_name(str(case_value), "switch")
    guard_index = 0
    for if_stmt, condition, local_names in _if_conditions_with_aliases(case_body, aliases, set()):
        guard_index += 1
        for suffix, guard_setup, branch_text, guard_paths, guard_facts in condition_setup_alternatives(condition, condition_ops):
            guard_setup = _rewrite_lookup_lines(guard_setup, lookup_aliases)
            branch_text = _rewrite_lookup_text(branch_text, lookup_aliases) or branch_text
            guard_facts = _rewrite_lookup_branch_facts(guard_facts, lookup_aliases)
            if not guard_setup:
                continue
            if _setup_references_local_root(guard_setup, local_names):
                continue
            name = _clean_name(f"ir_case_guard_{safe_name(selector_leaf, 'switch')}_{case_name}_{guard_index}_{safe_name(suffix, 'guard')}")
            candidates.append(BranchCandidate(
                name,
                [*case_setup, *guard_setup],
                source_location=display_source_location(
                    if_stmt.loc or switch_stmt.loc,
                    f"ir:{func.name}:switch[{switch_index}]:case[{case_name}]:if[{guard_index}]",
                ),
                target_branch=f"switch {selector_text} case {case_value}; if {branch_text}",
                origin="ir",
                object_paths=_dedup_object_paths([*object_paths, *guard_paths]),
                branch_facts=[case_fact, *guard_facts],
            ))
    return candidates


def _if_conditions_with_aliases(
    statements:   list[Stmt],
    aliases:      dict[str, Expr],
    local_names:  set[str],
) -> list[tuple[IfStmt, Expr, set[str]]]:
    found: list[tuple[IfStmt, Expr, set[str]]] = []
    current_aliases = dict(aliases)
    current_locals  = set(local_names)
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt):
            current_locals.add(stmt.name)
        record_alias(stmt, current_aliases)
        if isinstance(stmt, IfStmt):
            found.append((stmt, resolve_aliases(stmt.condition, current_aliases), set(current_locals)))
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals)))
        elif isinstance(stmt, LoopStmt):
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals)))
        elif isinstance(stmt, SwitchStmt):
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals)))
            for case in stmt.cases:
                found.extend(_if_conditions_with_aliases(case.body, dict(current_aliases), set(current_locals)))
            found.extend(_if_conditions_with_aliases(stmt.default_body, dict(current_aliases), set(current_locals)))
    return found


def _setup_references_local_root(setup: list[str], local_names: set[str]) -> bool:
    return any(
        (root := _assignment_root(line)) is not None and root in local_names
        for line in setup
    )


def _assignment_root(line: str) -> str | None:
    lhs = re.split(r"\s*(?:\|=|&=|\+=|-=|=)\s*", line, 1)[0].strip()
    lhs = lhs.lstrip("*& ")
    while True:
        casted = re.match(r"^\(\([^)]*\)\)\s*(.*)$", lhs)
        if not casted:
            break
        lhs = casted.group(1).lstrip("*& ")
    match = re.match(r"([A-Za-z_]\w*)", lhs)
    return match.group(1) if match else None


def _dedup_object_paths(facts: list[ObjectPathFact]) -> list[ObjectPathFact]:
    out: list[ObjectPathFact] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for fact in facts:
        key = (fact.root, fact.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def _clean_name(name: str) -> str:
    return re.sub(r"_+", "_", name).strip("_")


def _default_value(values: list[int | str]) -> int:
    int_values = {value for value in values if isinstance(value, int)}
    candidate = 0
    while candidate in int_values:
        candidate += 1
    return candidate


def _selector_leaf(expr: Expr) -> str | None:
    if isinstance(expr, FieldAccess):
        if isinstance(expr.base, VarRef):
            return expr.field
        base = _selector_leaf(expr.base)
        if base:
            return f"{base}_{expr.field}"
        return expr.field
    if isinstance(expr, VarRef):
        return expr.name
    return None


def _assigned_values_to_selector(stmt: SwitchStmt, selector: Expr, aliases: dict[str, Expr]) -> list[int | str]:
    selector_text = assignable_expr(selector)
    if selector_text is None:
        return []
    values: list[int | str] = []
    for nested in stmt.body:
        if not isinstance(nested, AssignmentStmt):
            continue
        target_text = assignable_expr(resolve_aliases(nested.target, aliases))
        if target_text != selector_text:
            continue
        value = _literal_value(nested.value)
        if value is not None and value not in values:
            values.append(value)
    return values


def _literal_value(expr: Expr) -> int | str | None:
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, VarRef):
        return expr.name
    return None
