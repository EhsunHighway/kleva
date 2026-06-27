from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import AssignmentStmt, BinaryOp, CallExpr, DeclarationStmt, Expr, FunctionIR, IfStmt, LoopStmt, ReturnStmt, SourceLocation, Stmt, SwitchStmt, UnaryOp, VarRef
from ..ir.naming import safe_name
from ..ir.relations import flipped_relation, int_value
from ..ir.render import assignable_expr, value_expr
from ..ir.walk import body_has_return
from .candidates import BranchCandidate, CallOutcomeFact, PostStateFact, display_source_location


@dataclass(frozen=True)
class CalleeGuard:
    callee:       str
    args:         list[str]
    failure_when: str
    loc:          SourceLocation | None = None
    result:       str | None = None


@dataclass(frozen=True)
class CallResultAlias:
    callee: str
    args:   list[str]
    result: str | None = None


def callee_guards_from_ir(func: FunctionIR) -> list[CalleeGuard]:
    guards: list[CalleeGuard] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for guard in _callee_guards_from_statements(func.statements, {}):
        key = (guard.callee, tuple(guard.args), guard.failure_when)
        if key in seen:
            continue
        seen.add(key)
        guards.append(guard)
    return guards


def _callee_guards_from_statements(
    statements:     list[Stmt],
    result_aliases: dict[str, CallResultAlias],
) -> list[CalleeGuard]:
    guards: list[CalleeGuard] = []
    current_aliases = dict(result_aliases)
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt):
            alias = _call_result_alias(stmt.init)
            if alias is not None:
                current_aliases[stmt.name] = CallResultAlias(alias.callee, alias.args, stmt.name)
            continue
        if isinstance(stmt, AssignmentStmt) and isinstance(stmt.target, VarRef):
            alias = _call_result_alias(stmt.value)
            if alias is not None:
                current_aliases[stmt.target.name] = CallResultAlias(alias.callee, alias.args, stmt.target.name)
        if isinstance(stmt, IfStmt):
            if body_has_return(stmt.body):
                guard = _guard_from_condition(stmt.condition, current_aliases)
                if guard:
                    guards.append(CalleeGuard(guard.callee, guard.args, guard.failure_when, stmt.loc, guard.result))
            guards.extend(_callee_guards_from_statements(stmt.body, dict(current_aliases)))
        elif isinstance(stmt, LoopStmt):
            guards.extend(_callee_guards_from_statements(stmt.body, dict(current_aliases)))
        elif isinstance(stmt, SwitchStmt):
            guards.extend(_callee_guards_from_statements(stmt.body, dict(current_aliases)))
    return guards


def _call_result_alias(expr: Expr | None) -> CallResultAlias | None:
    if not isinstance(expr, CallExpr):
        return None
    args = _arg_texts(expr)
    if len(args) != len(expr.args):
        return None
    return CallResultAlias(expr.callee, args)


def callee_candidates_from_ir(
    func: FunctionIR,
    setup_for_call: Callable[[str, list[str]], tuple[list[str], list[str]]] | None = None,
    helper_irs: dict[str, FunctionIR] | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    candidates: list[BranchCandidate] = []
    for guard in callee_guards_from_ir(func):
        safe = safe_name(guard.callee)
        mode = safe_name(guard.failure_when)
        source_location = display_source_location(guard.loc, f"ir:{func.name}:callee:{safe}")
        candidates.append(BranchCandidate(
            f"ir_callee_{safe}_{mode}_failure",
            [],
            source_location=source_location,
            target_branch=f"callee {guard.callee} failure {guard.failure_when}",
            origin="ir",
            call_facts=[CallOutcomeFact(guard.callee, guard.failure_when, "failure")],
        ))
        setup: list[str] = []
        preamble: list[str] = []
        if setup_for_call:
            setup, preamble = setup_for_call(guard.callee, guard.args)
        setup.extend(_helper_success_setup_from_ir(guard, helper_irs or {}, helper_params or {}))
        success_name = f"ir_callee_{safe}_{mode}_success"
        post_state_facts = _dedup_post_state_facts([
            *_post_state_facts_from_setup(setup),
            *_post_state_facts_from_helper_ir(guard, helper_irs or {}, helper_params or {}, result_alias=guard.result),
        ])
        witness_setup, extra_outputs = _side_effect_witnesses(success_name, post_state_facts)
        candidates.append(BranchCandidate(
            success_name,
            setup,
            preamble,
            source_location=source_location,
            target_branch=f"callee {guard.callee} success {guard.failure_when}",
            witness_outputs=True,
            origin="ir",
            witness_setup=witness_setup,
            extra_outputs=extra_outputs,
            call_facts=[CallOutcomeFact(guard.callee, guard.failure_when, "success")],
            post_state_facts=post_state_facts,
        ))
    return candidates


def _guard_from_condition(
    condition,
    result_aliases: dict[str, CallResultAlias] | None = None,
) -> CalleeGuard | None:
    result_aliases = result_aliases or {}
    if isinstance(condition, UnaryOp) and condition.op == "!" and isinstance(condition.operand, CallExpr):
        return CalleeGuard(condition.operand.callee, _arg_texts(condition.operand), "zero")
    if isinstance(condition, UnaryOp) and condition.op == "!" and isinstance(condition.operand, VarRef):
        alias = result_aliases.get(condition.operand.name)
        if alias is not None:
            return CalleeGuard(alias.callee, alias.args, "zero", result=alias.result)
    if isinstance(condition, BinaryOp):
        right_value = int_value(condition.right)
        if isinstance(condition.left, CallExpr) and right_value is not None:
            failure = _failure_mode(condition.op, right_value)
            if failure:
                return CalleeGuard(condition.left.callee, _arg_texts(condition.left), failure)
        if isinstance(condition.left, VarRef) and right_value is not None:
            alias = result_aliases.get(condition.left.name)
            failure = _failure_mode(condition.op, right_value)
            if alias is not None and failure:
                return CalleeGuard(alias.callee, alias.args, failure, result=alias.result)
        left_value = int_value(condition.left)
        if isinstance(condition.right, CallExpr) and left_value is not None:
            failure = _failure_mode(flipped_relation(condition.op), left_value)
            if failure:
                return CalleeGuard(condition.right.callee, _arg_texts(condition.right), failure)
        if isinstance(condition.right, VarRef) and left_value is not None:
            alias = result_aliases.get(condition.right.name)
            failure = _failure_mode(flipped_relation(condition.op), left_value)
            if alias is not None and failure:
                return CalleeGuard(alias.callee, alias.args, failure, result=alias.result)
    return None


def _failure_mode(op: str, value: int) -> str | None:
    if op == "==":
        return f"equals_{value}"
    if op == "!=" and value == 0:
        return "nonzero"
    if op == "<" and value == 0:
        return "negative"
    if op == "<=" and value == 0:
        return "nonpositive"
    return None


def _arg_texts(call: CallExpr) -> list[str]:
    args: list[str] = []
    for arg in call.args:
        text = value_expr(arg)
        if text is None:
            return []
        args.append(text)
    return args


def _post_state_facts_from_setup(setup: list[str]) -> list[PostStateFact]:
    facts: list[PostStateFact] = []
    seen: set[PostStateFact] = set()
    for line in setup:
        fact = _assignment_post_state_fact(line)
        if fact is None or fact in seen:
            continue
        seen.add(fact)
        facts.append(fact)
    return facts


def _post_state_facts_from_helper_ir(
    guard: CalleeGuard,
    helper_irs: dict[str, FunctionIR],
    helper_params: dict[str, tuple[str, ...]],
    result_alias: str | None = None,
) -> list[PostStateFact]:
    helper_ir = helper_irs.get(guard.callee)
    param_names = helper_params.get(guard.callee, ())
    if helper_ir is None or len(param_names) != len(guard.args):
        return []

    arg_by_param = dict(zip(param_names, guard.args))
    facts = _post_state_facts_from_statements(helper_ir.statements, arg_by_param, {}, guard.failure_when)
    if result_alias:
        returned_target = _returned_target_from_helper_ir(helper_ir)
        if returned_target is not None:
            returned_arg = _map_param_target(returned_target, arg_by_param)
            if returned_arg is not None:
                facts.extend(_facts_for_return_alias(facts, returned_arg, result_alias))
    return _dedup_post_state_facts(facts)


def _helper_success_setup_from_ir(
    guard: CalleeGuard,
    helper_irs: dict[str, FunctionIR],
    helper_params: dict[str, tuple[str, ...]],
) -> list[str]:
    helper_ir = helper_irs.get(guard.callee)
    param_names = helper_params.get(guard.callee, ())
    if helper_ir is None or len(param_names) != len(guard.args):
        return []

    arg_by_param = dict(zip(param_names, guard.args))
    setup: list[str] = []
    seen: set[str] = set()
    for stmt in helper_ir.statements:
        if not isinstance(stmt, IfStmt) or not _body_returns_failure(stmt.body, guard.failure_when):
            continue
        for line in _setup_for_false_condition(stmt.condition, arg_by_param):
            if line not in seen:
                seen.add(line)
                setup.append(line)
    return setup


def _body_returns_failure(statements: list[Stmt], failure_when: str) -> bool:
    for stmt in statements:
        if isinstance(stmt, ReturnStmt) and not _return_is_success(stmt.value, failure_when):
            return True
        if isinstance(stmt, IfStmt) and _body_returns_failure(stmt.body, failure_when):
            return True
        if isinstance(stmt, LoopStmt) and _body_returns_failure(stmt.body, failure_when):
            return True
        if isinstance(stmt, SwitchStmt):
            if _body_returns_failure(stmt.body, failure_when):
                return True
            if _body_returns_failure(stmt.default_body, failure_when):
                return True
            if any(_body_returns_failure(case.body, failure_when) for case in stmt.cases):
                return True
    return False


def _setup_for_false_condition(expr: Expr, arg_by_param: dict[str, str]) -> list[str]:
    if isinstance(expr, UnaryOp) and expr.op == "!":
        target = _mapped_assignable(expr.operand, arg_by_param)
        if target:
            return [f"{target} = 1;"]
        return []

    if isinstance(expr, BinaryOp):
        target = _mapped_assignable(expr.left, arg_by_param)
        rhs_value = int_value(expr.right)
        if target and rhs_value is not None:
            return _setup_for_false_relation(target, expr.op, rhs_value)
        target = _mapped_assignable(expr.right, arg_by_param)
        lhs_value = int_value(expr.left)
        if target and lhs_value is not None:
            return _setup_for_false_relation(target, flipped_relation(expr.op), lhs_value)

    target = _mapped_assignable(expr, arg_by_param)
    if target:
        return [f"{target} = 0;"]
    return []


def _setup_for_false_relation(target: str, op: str, value: int) -> list[str]:
    if op == "==":
        return [f"{target} = {value + 1};"]
    if op == "!=":
        return [f"{target} = {value};"]
    if op == "<":
        return [f"{target} = {value};"]
    if op == "<=":
        return [f"{target} = {value + 1};"]
    if op == ">":
        return [f"{target} = {value};"]
    if op == ">=":
        return [f"{target} = {value - 1 if value > 0 else 0};"]
    return []


def _mapped_assignable(expr: Expr, arg_by_param: dict[str, str]) -> str | None:
    target = assignable_expr(expr) or value_expr(expr)
    if target is None:
        return None
    return _map_param_target(target, arg_by_param)


def _returned_target_from_helper_ir(helper_ir: FunctionIR) -> str | None:
    aliases: AliasMap = {}
    return _returned_target_from_statements(helper_ir.statements, aliases)


def _returned_target_from_statements(statements: list[Stmt], aliases: AliasMap) -> str | None:
    current_aliases = dict(aliases)
    for stmt in statements:
        if isinstance(stmt, DeclarationStmt):
            record_alias(stmt, current_aliases)
            continue
        if isinstance(stmt, AssignmentStmt):
            record_alias(stmt, current_aliases)
            continue
        if isinstance(stmt, ReturnStmt) and stmt.value is not None:
            target = value_expr(resolve_aliases(stmt.value, current_aliases))
            if target is not None:
                return target
        if isinstance(stmt, IfStmt):
            found = _returned_target_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
        elif isinstance(stmt, LoopStmt):
            found = _returned_target_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
        elif isinstance(stmt, SwitchStmt):
            found = _returned_target_from_statements(stmt.body, dict(current_aliases))
            if found is not None:
                return found
    return None


def _facts_for_return_alias(
    facts:        list[PostStateFact],
    returned_arg: str,
    result_alias: str,
) -> list[PostStateFact]:
    out: list[PostStateFact] = []
    for fact in facts:
        if fact.target == returned_arg:
            out.append(PostStateFact(result_alias, fact.relation, fact.value))
        elif fact.target.startswith(f"{returned_arg}->"):
            out.append(PostStateFact(f"{result_alias}->{fact.target[len(returned_arg) + 2:]}", fact.relation, fact.value))
        elif fact.target.startswith(f"{returned_arg}."):
            out.append(PostStateFact(f"{result_alias}.{fact.target[len(returned_arg) + 1:]}", fact.relation, fact.value))
    return out


def _post_state_facts_from_statements(
    statements:    list[Stmt],
    arg_by_param:  dict[str, str],
    aliases:       AliasMap,
    failure_when:  str,
) -> list[PostStateFact]:
    paths = _post_state_paths(
        statements,
        arg_by_param,
        failure_when,
        [_PathState([], dict(aliases))],
    )
    return _intersect_post_state_paths(paths)


@dataclass(frozen=True)
class _PathState:
    facts:   list[PostStateFact]
    aliases: AliasMap


def _post_state_paths(
    statements:   list[Stmt],
    arg_by_param: dict[str, str],
    failure_when: str,
    active:       list[_PathState],
) -> list[list[PostStateFact]]:
    terminals: list[list[PostStateFact]] = []
    states = list(active)

    for stmt in statements:
        next_states: list[_PathState] = []
        for state in states:
            if isinstance(stmt, DeclarationStmt):
                aliases = dict(state.aliases)
                record_alias(stmt, aliases)
                next_states.append(replace(state, aliases=aliases))
                continue
            if isinstance(stmt, AssignmentStmt):
                aliases = dict(state.aliases)
                facts = [*state.facts]
                fact = _post_state_fact_from_assignment(stmt, arg_by_param, aliases)
                if fact is not None:
                    facts.append(fact)
                record_alias(stmt, aliases)
                next_states.append(_PathState(facts, aliases))
                continue
            if isinstance(stmt, ReturnStmt):
                if _return_is_success(stmt.value, failure_when):
                    terminals.append(state.facts)
                continue
            if isinstance(stmt, IfStmt):
                terminals.extend(_post_state_paths(stmt.body, arg_by_param, failure_when, [state]))
                next_states.append(state)
                continue
            if isinstance(stmt, LoopStmt):
                next_states.append(state)
                continue
            if isinstance(stmt, SwitchStmt):
                case_bodies = [case.body for case in stmt.cases if case.body]
                if stmt.default_body:
                    case_bodies.append(stmt.default_body)
                if case_bodies:
                    for body in case_bodies:
                        terminals.extend(_post_state_paths(body, arg_by_param, failure_when, [state]))
                else:
                    terminals.extend(_post_state_paths(stmt.body, arg_by_param, failure_when, [state]))
                if not stmt.has_default:
                    next_states.append(state)
                continue
            next_states.append(state)
        states = next_states
        if not states:
            break

    terminals.extend(state.facts for state in states)
    return terminals


def _return_is_success(value: Expr | None, failure_when: str) -> bool:
    literal = int_value(value) if value is not None else None
    if failure_when == "nonzero":
        return literal == 0
    if failure_when == "zero":
        return literal != 0 if literal is not None else value is not None
    if failure_when == "negative":
        return literal is not None and literal >= 0
    if failure_when == "nonpositive":
        return literal is not None and literal > 0
    if failure_when.startswith("equals_"):
        forbidden = _failure_literal(failure_when)
        if forbidden is None or literal is None:
            return True
        return literal != forbidden
    return True


def _failure_literal(failure_when: str) -> int | None:
    text = failure_when[len("equals_"):] if failure_when.startswith("equals_") else failure_when
    try:
        return int(text)
    except ValueError:
        return None


def _post_state_fact_from_assignment(
    stmt:         AssignmentStmt,
    arg_by_param: dict[str, str],
    aliases:      AliasMap,
) -> PostStateFact | None:
    target_expr = resolve_aliases(stmt.target, aliases)
    target = value_expr(target_expr)
    if target is None:
        return None
    mapped = _map_param_target(target, arg_by_param)
    relation = _nonzero_relation(stmt.value)
    if mapped is None or relation is None:
        return None
    return PostStateFact(mapped, relation, "0")


def _intersect_post_state_paths(paths: list[list[PostStateFact]]) -> list[PostStateFact]:
    if not paths:
        return []
    common = set(paths[0])
    for path in paths[1:]:
        common.intersection_update(path)
    return [fact for fact in _dedup_post_state_facts(paths[0]) if fact in common]


def _map_param_target(target: str, arg_by_param: dict[str, str]) -> str | None:
    for param, arg in arg_by_param.items():
        if target == param:
            return arg
        if target == f"*{param}":
            return _map_deref_arg(arg)
        if target.startswith(f"{param}->"):
            return f"{arg}->{target[len(param) + 2:]}"
        if target.startswith(f"{param}."):
            return f"{arg}.{target[len(param) + 1:]}"
    return None


def _map_deref_arg(arg: str) -> str:
    if arg.startswith("&") and len(arg) > 1:
        return arg[1:]
    return f"*{arg}"


def _nonzero_relation(value: Expr) -> str | None:
    literal = int_value(value)
    if literal is None:
        return None
    return "!=" if literal != 0 else "=="


def _dedup_post_state_facts(facts: list[PostStateFact]) -> list[PostStateFact]:
    out: list[PostStateFact] = []
    seen: set[PostStateFact] = set()
    for fact in facts:
        if fact in seen:
            continue
        seen.add(fact)
        out.append(fact)
    return out


def _side_effect_witnesses(candidate_name: str, facts: list[PostStateFact]) -> tuple[list[str], list[str]]:
    witness_setup: list[str] = []
    outputs: list[str] = []
    seen: set[str] = set()
    for fact in facts:
        out_name = f"out_{safe_name(candidate_name)}_{_safe_witness_name(fact.target)}_nonzero"
        if out_name in seen:
            continue
        seen.add(out_name)
        witness_setup.append(f"int {out_name} = ({fact.target} != 0);")
        outputs.append(out_name)
    return witness_setup, outputs


def _assignment_post_state_fact(line: str) -> PostStateFact | None:
    text = line.strip()
    if not text.endswith(";") or "=" not in text or "==" in text:
        return None
    lhs = text.split("=", 1)[0].strip()
    if not lhs:
        return None
    return PostStateFact(lhs, "!=", "0")


def _safe_witness_name(value: str) -> str:
    return "_".join(part for part in safe_name(value).split("_") if part)
