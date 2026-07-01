from __future__ import annotations

from dataclasses import dataclass

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, DeclarationStmt, Expr, ExprStmt, FunctionIR, IfStmt, LoopStmt, ReturnStmt, Stmt, SwitchStmt, UnaryOp, VarRef
from ..ir.naming import safe_name
from ..ir.relations import flipped_relation, int_value
from ..ir.render import assignable_expr, is_pointer_expr, value_expr
from .candidates import OwnershipPathFact, PostStateFact
from .ir_ownership import ownership_facts_from_ir


@dataclass(frozen=True)
class HelperSideEffect:
    kind:     str
    target:   str
    value:    str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class HelperEffectSummary:
    helper:          str
    failure_when:    str
    success_setup:   tuple[str, ...]
    failure_setup:   tuple[str, ...]
    post_state:      tuple[PostStateFact, ...]
    ownership:       tuple[OwnershipPathFact, ...]
    side_effects:    tuple[HelperSideEffect, ...]


def helper_effect_summary(
    helper_ir: FunctionIR,
    helper_params: tuple[str, ...],
    call_args: list[str],
    failure_when: str,
    helper_name: str | None = None,
) -> HelperEffectSummary:
    if len(helper_params) != len(call_args):
        return HelperEffectSummary(helper_name or helper_ir.name, failure_when, (), (), (), (), ())

    arg_by_param = dict(zip(helper_params, call_args))
    success_setup = _setup_for_helper_outcome(helper_ir, arg_by_param, failure_when, success=True)
    failure_setup = _setup_for_helper_outcome(helper_ir, arg_by_param, failure_when, success=False)
    post_state = _dedup_post_state_facts(
        _post_state_facts_from_statements(helper_ir.statements, arg_by_param, {}, failure_when)
    )
    ownership = _ownership_facts_from_helper_ir(
        helper_name or helper_ir.name,
        helper_ir,
        helper_params,
        arg_by_param,
    )
    side_effects = _side_effects_from_helper_ir(helper_ir, arg_by_param)
    return HelperEffectSummary(
        helper_name or helper_ir.name,
        failure_when,
        tuple(success_setup),
        tuple(failure_setup),
        tuple(post_state),
        tuple(ownership),
        tuple(side_effects),
    )


def _setup_for_helper_outcome(
    helper_ir: FunctionIR,
    arg_by_param: dict[str, str],
    failure_when: str,
    success: bool,
) -> list[str]:
    setup: list[str] = []
    seen: set[str] = set()
    for stmt in helper_ir.statements:
        if not isinstance(stmt, IfStmt) or not _body_returns_failure(stmt.body, failure_when):
            continue
        lines = (
            _setup_for_false_condition(stmt.condition, arg_by_param)
            if success
            else _setup_for_true_condition(stmt.condition, arg_by_param)
        )
        for line in lines:
            if line not in seen:
                seen.add(line)
                setup.append(line)
    if success:
        for line in _returned_array_slot_setup(helper_ir, arg_by_param):
            if line not in seen:
                seen.add(line)
                setup.append(line)
    return setup


def _returned_array_slot_setup(helper_ir: FunctionIR, arg_by_param: dict[str, str]) -> list[str]:
    aliases: dict[str, ArraySubscript] = {}
    for stmt in helper_ir.statements:
        if isinstance(stmt, DeclarationStmt) and isinstance(stmt.init, ArraySubscript):
            aliases[stmt.name] = stmt.init
            continue
        if isinstance(stmt, ReturnStmt) and isinstance(stmt.value, VarRef):
            slot = aliases.get(stmt.value.name)
            if slot is None:
                continue
            return _array_slot_fixture_lines(helper_ir.name, stmt.value.name, slot, arg_by_param)
    return []


def _array_slot_fixture_lines(
    helper_name: str,
    result_name: str,
    slot: ArraySubscript,
    arg_by_param: dict[str, str],
) -> list[str]:
    base = value_expr(slot.base)
    index = int_value(slot.index)
    object_type = _pointed_object_type(slot.c_type)
    array_element_type = _pointed_object_type(getattr(slot.base, "c_type", None))
    if base is None or index is None or object_type is None:
        return []
    mapped_base = _map_param_target(base, arg_by_param)
    if mapped_base is None:
        return []
    obj_name = f"kleva_{safe_name(helper_name)}_{safe_name(result_name)}_{index}"
    lines: list[str] = []
    if array_element_type is not None:
        array_name = f"{obj_name}_array"
        lines.extend([
            f"{array_element_type} {array_name}[{index + 1}];",
            f"memset({array_name}, 0, sizeof({array_name}));",
            f"{mapped_base} = {array_name};",
        ])
    lines.extend([
        f"{object_type} *{obj_name} = malloc(sizeof(*{obj_name}));",
        f"assert({obj_name} != NULL);",
        f"memset({obj_name}, 0, sizeof(*{obj_name}));",
        f"{mapped_base}[{index}] = {obj_name};",
    ])
    return lines


def _pointed_object_type(c_type: str | None) -> str | None:
    if not c_type:
        return None
    text = c_type.strip()
    if not text.endswith("*"):
        return None
    text = text[:-1].strip()
    for qualifier in ("const ", "volatile "):
        if text.startswith(qualifier):
            text = text[len(qualifier):].strip()
    return text or None


def _setup_for_true_condition(expr: Expr, arg_by_param: dict[str, str]) -> list[str]:
    if isinstance(expr, UnaryOp) and expr.op == "!":
        target = _mapped_assignable(expr.operand, arg_by_param)
        return [f"{target} = 0;"] if target else []

    if isinstance(expr, BinaryOp):
        target = _mapped_assignable(expr.left, arg_by_param)
        rhs_value = int_value(expr.right)
        if target and rhs_value is not None:
            return _setup_for_true_relation(target, expr.op, rhs_value)
        target = _mapped_assignable(expr.right, arg_by_param)
        lhs_value = int_value(expr.left)
        if target and lhs_value is not None:
            return _setup_for_true_relation(target, flipped_relation(expr.op), lhs_value)

    target = _mapped_assignable(expr, arg_by_param)
    if target and is_pointer_expr(expr):
        return [f"/* kleva: non-null pointer path {target} backed by fixture */"]
    return [f"{target} = 1;"] if target else []


def _setup_for_false_condition(expr: Expr, arg_by_param: dict[str, str]) -> list[str]:
    if isinstance(expr, UnaryOp) and expr.op == "!":
        target = _mapped_assignable(expr.operand, arg_by_param)
        if target and is_pointer_expr(expr.operand):
            return [f"/* kleva: non-null pointer path {target} backed by fixture */"]
        return [f"{target} = 1;"] if target else []

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
    return [f"{target} = 0;"] if target else []


def _setup_for_true_relation(target: str, op: str, value: int) -> list[str]:
    if op == "==":
        return [f"{target} = {value};"]
    if op == "!=":
        return [f"{target} = {value + 1};"]
    if op == "<":
        return [f"{target} = {value - 1 if value > 0 else 0};"]
    if op == "<=":
        return [f"{target} = {value};"]
    if op == ">":
        return [f"{target} = {value + 1};"]
    if op == ">=":
        return [f"{target} = {value};"]
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


def _post_state_facts_from_statements(
    statements:    list[Stmt],
    arg_by_param:  dict[str, str],
    aliases:       AliasMap,
    failure_when:  str,
) -> list[PostStateFact]:
    terminals = _post_state_paths(
        statements,
        arg_by_param,
        failure_when,
        [([], dict(aliases))],
    )
    return _intersect_post_state_paths(terminals)


def _post_state_paths(
    statements:   list[Stmt],
    arg_by_param: dict[str, str],
    failure_when: str,
    active:       list[tuple[list[PostStateFact], AliasMap]],
) -> list[list[PostStateFact]]:
    terminals: list[list[PostStateFact]] = []
    states = list(active)

    for stmt in statements:
        next_states: list[tuple[list[PostStateFact], AliasMap]] = []
        for facts, aliases in states:
            if isinstance(stmt, DeclarationStmt):
                next_aliases = dict(aliases)
                record_alias(stmt, next_aliases)
                next_states.append((facts, next_aliases))
                continue
            if isinstance(stmt, AssignmentStmt):
                next_aliases = dict(aliases)
                next_facts = [*facts]
                fact = _post_state_fact_from_assignment(stmt, arg_by_param, next_aliases)
                if fact is not None:
                    next_facts.append(fact)
                record_alias(stmt, next_aliases)
                next_states.append((next_facts, next_aliases))
                continue
            if isinstance(stmt, ReturnStmt):
                if _return_is_success(stmt.value, failure_when):
                    terminals.append(facts)
                continue
            if isinstance(stmt, IfStmt):
                terminals.extend(_post_state_paths(stmt.body, arg_by_param, failure_when, [(facts, dict(aliases))]))
                next_states.append((facts, aliases))
                continue
            if isinstance(stmt, LoopStmt):
                next_states.append((facts, aliases))
                continue
            if isinstance(stmt, SwitchStmt):
                case_bodies = [case.body for case in stmt.cases if case.body]
                if stmt.default_body:
                    case_bodies.append(stmt.default_body)
                for body in case_bodies or [stmt.body]:
                    terminals.extend(_post_state_paths(body, arg_by_param, failure_when, [(facts, dict(aliases))]))
                if not stmt.has_default:
                    next_states.append((facts, aliases))
                continue
            next_states.append((facts, aliases))
        states = next_states
        if not states:
            break

    terminals.extend(facts for facts, _aliases in states)
    return terminals


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
    relation, value = _assignment_relation(stmt.value, arg_by_param, aliases)
    if mapped is None or relation is None or value is None:
        return None
    return PostStateFact(mapped, relation, value)


def _assignment_relation(
    value:        Expr,
    arg_by_param: dict[str, str],
    aliases:      AliasMap,
) -> tuple[str | None, str | None]:
    literal = int_value(value)
    if literal is not None:
        return "==", str(literal)
    value_text = value_expr(resolve_aliases(value, aliases))
    if value_text is None:
        return None, None
    mapped = _map_param_target(value_text, arg_by_param) or value_text
    return "==", mapped


def _ownership_facts_from_helper_ir(
    helper_name:   str,
    helper_ir:     FunctionIR,
    helper_params: tuple[str, ...],
    arg_by_param:  dict[str, str],
) -> tuple[OwnershipPathFact, ...]:
    facts: list[OwnershipPathFact] = []
    seen: set[OwnershipPathFact] = set()
    for fact in ownership_facts_from_ir(helper_ir, set(helper_params)):
        target = _map_param_target(fact.param, arg_by_param)
        if target is None:
            continue
        mapped = OwnershipPathFact(target, fact.action, f"{helper_name}:{fact.target}")
        if mapped in seen:
            continue
        seen.add(mapped)
        facts.append(mapped)
    return tuple(facts)


def _side_effects_from_helper_ir(
    helper_ir: FunctionIR,
    arg_by_param: dict[str, str],
) -> tuple[HelperSideEffect, ...]:
    effects: list[HelperSideEffect] = []
    seen: set[HelperSideEffect] = set()
    for stmt in _walk_shallow(helper_ir.statements):
        effect = _side_effect_from_stmt(stmt, arg_by_param)
        if effect is None or effect in seen:
            continue
        seen.add(effect)
        effects.append(effect)
    return tuple(effects)


def _side_effect_from_stmt(
    stmt: Stmt,
    arg_by_param: dict[str, str],
) -> HelperSideEffect | None:
    if isinstance(stmt, AssignmentStmt):
        target = value_expr(stmt.target)
        if target is None:
            return None
        mapped_target = _map_param_target(target, arg_by_param) or target
        mapped_value = _effect_value(stmt.value, arg_by_param)
        kind = "array-slot-filled" if "[" in mapped_target else "field-changed"
        return HelperSideEffect(kind, mapped_target, mapped_value, "assignment")
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr):
        args = ", ".join(_effect_value(arg, arg_by_param) or "?" for arg in stmt.expr.args)
        return HelperSideEffect("call", stmt.expr.callee, args, "call")
    return None


def _effect_value(expr: Expr, arg_by_param: dict[str, str]) -> str | None:
    literal = int_value(expr)
    if literal is not None:
        return str(literal)
    text = value_expr(expr)
    if text is None:
        return None
    return _map_param_target(text, arg_by_param) or text


def _walk_shallow(statements: list[Stmt]) -> list[Stmt]:
    out: list[Stmt] = []
    for stmt in statements:
        out.append(stmt)
        if isinstance(stmt, IfStmt):
            out.extend(_walk_shallow(stmt.body))
        elif isinstance(stmt, LoopStmt):
            out.extend(_walk_shallow(stmt.body))
        elif isinstance(stmt, SwitchStmt):
            out.extend(_walk_shallow(stmt.body))
            for case in stmt.cases:
                out.extend(_walk_shallow(case.body))
            out.extend(_walk_shallow(stmt.default_body))
    return out


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


def _mapped_assignable(expr: Expr, arg_by_param: dict[str, str]) -> str | None:
    target = assignable_expr(expr) or value_expr(expr)
    if target is None:
        return None
    return _map_param_target(target, arg_by_param)


def _map_param_target(target: str | None, arg_by_param: dict[str, str]) -> str | None:
    if target is None:
        return None
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


def _intersect_post_state_paths(paths: list[list[PostStateFact]]) -> list[PostStateFact]:
    if not paths:
        return []
    common = set(paths[0])
    for path in paths[1:]:
        common.intersection_update(path)
    return [fact for fact in _dedup_post_state_facts(paths[0]) if fact in common]


def _dedup_post_state_facts(facts: list[PostStateFact]) -> list[PostStateFact]:
    out: list[PostStateFact] = []
    seen: set[PostStateFact] = set()
    for fact in facts:
        if fact in seen:
            continue
        seen.add(fact)
        out.append(fact)
    return out
