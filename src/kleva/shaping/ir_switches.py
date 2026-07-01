from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir.aliases import record_alias, resolve_aliases
from ..ir.model import (
    AssignmentStmt,
    BreakStmt,
    DeclarationStmt,
    Expr,
    FieldAccess,
    FunctionIR,
    IfStmt,
    IntLiteral,
    LoopStmt,
    ExprStmt,
    CallExpr,
    AddressOf,
    ArraySubscript,
    Stmt,
    SwitchStmt,
    ContinueStmt,
    ReturnStmt,
    VarRef,
)
from ..ir.naming import safe_name
from ..ir.render import assignable_expr
from .candidates import BranchCandidate, BranchFact, ObjectPathFact, StateTransitionFact, display_source_location, object_path_facts_from_expr
from .ir_conditions import IrConditionOps, condition_setup_alternatives, path_precondition_alternatives
from .ir_helper_effects import helper_effect_summary
from .ir_poststate import post_state_facts_from_direct_assignments


@dataclass(frozen=True)
class StateTransition:
    selector:    str
    source:      int | str
    target:      int | str
    setup:       tuple[str, ...] = ()
    guard:       str | None = None
    via:         str | None = None
    loc_display: str | None = None


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
            case_post_state_facts = post_state_facts_from_direct_assignments(case.body)
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
                post_state_facts=case_post_state_facts,
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
            for transition in _transitions_for_case(
                func,
                stmt,
                selector_text,
                selector,
                case.value,
                case.body,
                aliases,
                lookup_aliases,
                condition_ops,
                helper_irs or {},
                helper_params or {},
            ):
                if transition.target == case.value:
                    continue
                transition_name = (
                    f"ir_transition_{safe_name(selector_leaf, 'switch')}_"
                    f"{case_name}_to_{safe_name(str(transition.target), 'switch')}"
                )
                if transition.guard:
                    transition_name = f"{transition_name}_{safe_name(transition.guard, 'guard')}"
                if transition.via:
                    transition_name = f"{transition_name}_{safe_name(transition.via, 'via')}"
                if transition_name in seen:
                    continue
                seen.add(transition_name)
                candidates.append(BranchCandidate(
                    transition_name,
                    [*case_setup, *transition.setup],
                    source_location=transition.loc_display or display_source_location(stmt.loc, f"ir:{func.name}:switch[{index}]"),
                    target_branch=_transition_branch_text(transition),
                    origin="ir",
                    object_paths=object_paths,
                    branch_facts=[BranchFact(selector_text, "case", str(case.value))],
                    transition_facts=[StateTransitionFact(
                        selector_text,
                        str(case.value),
                        str(transition.target),
                        transition.guard,
                        transition.via,
                    )],
                    post_state_facts=case_post_state_facts,
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
    for if_stmt, condition, local_names, path_conditions in _if_conditions_with_aliases(case_body, aliases, set(), []):
        true_post_state_facts = post_state_facts_from_direct_assignments(if_stmt.body)
        guard_index += 1
        path_preconditions = path_precondition_alternatives(path_conditions, condition_ops)
        for suffix, guard_setup, branch_text, guard_paths, guard_facts in condition_setup_alternatives(condition, condition_ops):
            guard_setup = _rewrite_lookup_lines(guard_setup, lookup_aliases)
            branch_text = _rewrite_lookup_text(branch_text, lookup_aliases) or branch_text
            guard_facts = _rewrite_lookup_branch_facts(guard_facts, lookup_aliases)
            if not guard_setup:
                continue
            for path_setup, path_branch_text, path_paths, path_facts in path_preconditions:
                combined_setup = _dedup_setup([*case_setup, *path_setup, *guard_setup])
                if _setup_references_local_root(combined_setup, local_names):
                    continue
                combined_branch_text = (
                    f"{path_branch_text}; {branch_text}"
                    if path_branch_text
                    else branch_text
                )
                name = _clean_name(f"ir_case_guard_{safe_name(selector_leaf, 'switch')}_{case_name}_{guard_index}_{safe_name(suffix, 'guard')}")
                candidates.append(BranchCandidate(
                    name,
                    combined_setup,
                    source_location=display_source_location(
                        if_stmt.loc or switch_stmt.loc,
                        f"ir:{func.name}:switch[{switch_index}]:case[{case_name}]:if[{guard_index}]",
                    ),
                    target_branch=f"switch {selector_text} case {case_value}; if {combined_branch_text}",
                    origin="ir",
                    object_paths=_dedup_object_paths([*object_paths, *path_paths, *guard_paths]),
                    branch_facts=[case_fact, *path_facts, *guard_facts],
                    post_state_facts=[] if suffix.startswith("false_") else true_post_state_facts,
                ))
    return candidates


def _if_conditions_with_aliases(
    statements:   list[Stmt],
    aliases:      dict[str, Expr],
    local_names:  set[str],
    path_conditions: list[Expr],
) -> list[tuple[IfStmt, Expr, set[str], list[Expr]]]:
    found: list[tuple[IfStmt, Expr, set[str], list[Expr]]] = []
    current_aliases = dict(aliases)
    current_locals  = set(local_names)
    current_path_conditions = list(path_conditions)
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt):
            current_locals.add(stmt.name)
        record_alias(stmt, current_aliases)
        if isinstance(stmt, IfStmt):
            resolved_condition = resolve_aliases(stmt.condition, current_aliases)
            found.append((stmt, resolved_condition, set(current_locals), list(current_path_conditions)))
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
            if _body_exits(stmt.body):
                current_path_conditions.append(resolved_condition)
        elif isinstance(stmt, LoopStmt):
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
        elif isinstance(stmt, SwitchStmt):
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
            for case in stmt.cases:
                found.extend(_if_conditions_with_aliases(case.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
            found.extend(_if_conditions_with_aliases(stmt.default_body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
    return found


def _body_exits(body: list[Stmt]) -> bool:
    return any(isinstance(stmt, (BreakStmt, ContinueStmt, ReturnStmt)) for stmt in body)


def _dedup_setup(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _transitions_for_case(
    func:           FunctionIR,
    switch_stmt:    SwitchStmt,
    selector_text:  str,
    selector:       Expr,
    case_value:     int | str,
    case_body:      list[Stmt],
    aliases:        dict[str, Expr],
    lookup_aliases: dict[str, str],
    condition_ops:  IrConditionOps | None,
    helper_irs:     dict[str, FunctionIR],
    helper_params:  dict[str, tuple[str, ...]],
) -> list[StateTransition]:
    transitions: list[StateTransition] = []
    transitions.extend(_transitions_from_statements(
        func,
        switch_stmt.body,
        selector_text,
        selector,
        case_value,
        aliases,
        lookup_aliases,
        condition_ops,
        helper_irs,
        helper_params,
        guard_setup=(),
        guard_text=None,
    ))
    transitions.extend(_transitions_from_statements(
        func,
        case_body,
        selector_text,
        selector,
        case_value,
        aliases,
        lookup_aliases,
        condition_ops,
        helper_irs,
        helper_params,
        guard_setup=(),
        guard_text=None,
    ))
    return _dedup_transitions(transitions)


def _transitions_from_statements(
    func:           FunctionIR,
    statements:     list[Stmt],
    selector_text:  str,
    selector:       Expr,
    source_value:   int | str,
    aliases:        dict[str, Expr],
    lookup_aliases: dict[str, str],
    condition_ops:  IrConditionOps | None,
    helper_irs:     dict[str, FunctionIR],
    helper_params:  dict[str, tuple[str, ...]],
    guard_setup:    tuple[str, ...],
    guard_text:     str | None,
) -> list[StateTransition]:
    transitions: list[StateTransition] = []
    current_aliases = dict(aliases)
    for stmt in statements:
        record_alias(stmt, current_aliases)
        direct = _transition_from_assignment(
            stmt,
            selector_text,
            source_value,
            current_aliases,
            lookup_aliases,
            guard_setup,
            guard_text,
        )
        if direct is not None:
            transitions.append(direct)
            continue

        helper_transition = _transition_from_helper_call(
            stmt,
            selector_text,
            source_value,
            current_aliases,
            lookup_aliases,
            helper_irs,
            helper_params,
            guard_setup,
            guard_text,
        )
        if helper_transition is not None:
            transitions.append(helper_transition)
            continue

        if isinstance(stmt, IfStmt):
            if condition_ops is not None:
                condition = resolve_aliases(stmt.condition, current_aliases)
                for suffix, setup, branch_text, _guard_paths, _guard_facts in condition_setup_alternatives(condition, condition_ops):
                    if suffix.startswith("false_"):
                        continue
                    setup = tuple(_rewrite_lookup_lines(setup, lookup_aliases))
                    if not setup:
                        continue
                    transitions.extend(_transitions_from_statements(
                        func,
                        stmt.body,
                        selector_text,
                        selector,
                        source_value,
                        current_aliases,
                        lookup_aliases,
                        condition_ops,
                        helper_irs,
                        helper_params,
                        guard_setup=(*guard_setup, *setup),
                        guard_text=_rewrite_lookup_text(branch_text, lookup_aliases) or branch_text,
                    ))
            else:
                transitions.extend(_transitions_from_statements(
                    func,
                    stmt.body,
                    selector_text,
                    selector,
                    source_value,
                    current_aliases,
                    lookup_aliases,
                    condition_ops,
                    helper_irs,
                    helper_params,
                    guard_setup=guard_setup,
                    guard_text=guard_text,
                ))
        elif isinstance(stmt, LoopStmt):
            transitions.extend(_transitions_from_statements(
                func,
                stmt.body,
                selector_text,
                selector,
                source_value,
                current_aliases,
                lookup_aliases,
                condition_ops,
                helper_irs,
                helper_params,
                guard_setup=guard_setup,
                guard_text=guard_text,
            ))
        elif isinstance(stmt, SwitchStmt):
            transitions.extend(_transitions_from_statements(
                func,
                stmt.body,
                selector_text,
                selector,
                source_value,
                current_aliases,
                lookup_aliases,
                condition_ops,
                helper_irs,
                helper_params,
                guard_setup=guard_setup,
                guard_text=guard_text,
            ))
            for case in stmt.cases:
                transitions.extend(_transitions_from_statements(
                    func,
                    case.body,
                    selector_text,
                    selector,
                    source_value,
                    current_aliases,
                    lookup_aliases,
                    condition_ops,
                    helper_irs,
                    helper_params,
                    guard_setup=guard_setup,
                    guard_text=guard_text,
                ))
            transitions.extend(_transitions_from_statements(
                func,
                stmt.default_body,
                selector_text,
                selector,
                source_value,
                current_aliases,
                lookup_aliases,
                condition_ops,
                helper_irs,
                helper_params,
                guard_setup=guard_setup,
                guard_text=guard_text,
            ))
    return transitions


def _transition_from_assignment(
    stmt:           Stmt,
    selector_text:  str,
    source_value:   int | str,
    aliases:        dict[str, Expr],
    lookup_aliases: dict[str, str],
    guard_setup:    tuple[str, ...],
    guard_text:     str | None,
) -> StateTransition | None:
    if not isinstance(stmt, AssignmentStmt):
        return None
    target_text = assignable_expr(resolve_aliases(stmt.target, aliases))
    target_text = _rewrite_lookup_text(target_text, lookup_aliases)
    if target_text != selector_text:
        return None
    value = _literal_value(resolve_aliases(stmt.value, aliases))
    if value is None:
        return None
    return StateTransition(
        selector_text,
        source_value,
        value,
        setup=guard_setup,
        guard=guard_text,
        via=None,
    )


def _transition_from_helper_call(
    stmt:           Stmt,
    selector_text:  str,
    source_value:   int | str,
    aliases:        dict[str, Expr],
    lookup_aliases: dict[str, str],
    helper_irs:     dict[str, FunctionIR],
    helper_params:  dict[str, tuple[str, ...]],
    guard_setup:    tuple[str, ...],
    guard_text:     str | None,
) -> StateTransition | None:
    call = _call_expr_from_stmt(stmt)
    if call is None:
        return None
    helper_ir = helper_irs.get(call.callee)
    params = helper_params.get(call.callee, ())
    if helper_ir is None or len(params) != len(call.args):
        return None
    args = [
        assignable_expr(resolve_aliases(arg, aliases)) or ""
        for arg in call.args
    ]
    if any(not arg for arg in args):
        return None
    summary = helper_effect_summary(helper_ir, params, args, "equals_-1", call.callee)
    for fact in summary.post_state:
        target = _rewrite_lookup_text(fact.target, lookup_aliases)
        if target != selector_text or fact.relation != "==":
            continue
        return StateTransition(
            selector_text,
            source_value,
            fact.value,
            setup=guard_setup,
            guard=guard_text,
            via=f"helper:{call.callee}",
        )
    return None


def _call_expr_from_stmt(stmt: Stmt) -> CallExpr | None:
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr):
        return stmt.expr
    if isinstance(stmt, AssignmentStmt) and isinstance(stmt.value, CallExpr):
        return stmt.value
    if isinstance(stmt, DeclarationStmt) and isinstance(stmt.init, CallExpr):
        return stmt.init
    return None


def _transition_branch_text(transition: StateTransition) -> str:
    text = f"transition {transition.selector} {transition.source} -> {transition.target}"
    if transition.guard:
        text = f"{text} when {transition.guard}"
    if transition.via:
        text = f"{text} via {transition.via}"
    return text


def _dedup_transitions(transitions: list[StateTransition]) -> list[StateTransition]:
    out: list[StateTransition] = []
    seen: set[StateTransition] = set()
    for transition in transitions:
        if transition in seen:
            continue
        seen.add(transition)
        out.append(transition)
    return out


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
