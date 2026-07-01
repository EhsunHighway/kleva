from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import BinaryOp, CallExpr, Expr, FunctionIR, IfStmt, LoopStmt, ReturnStmt, Stmt, SwitchStmt
from ..ir.model import DeclarationStmt
from ..ir.relations import flipped_relation, int_value, relation_name
from ..ir.render import assignable_expr, value_expr
from ..ir.walk import body_has_return, walk_if_statements
from .candidates import BranchCandidate, BranchFact, CallOutcomeFact, ObjectPathFact, display_source_location, object_path_facts_from_expr


@dataclass(frozen=True)
class IrParserOps:
    safe_c_name:       Callable[[str], str]
    helper_call_rules: tuple["HelperCallRule", ...] = ()
    helper_irs:        dict[str, FunctionIR] | None = None
    helper_params:     dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class HelperCallRule:
    callee:        str
    success_setup: tuple[str, ...] = ()
    failure_setup: tuple[str, ...] = ()


@dataclass(frozen=True)
class NumericGuard:
    target: str
    op:     str
    value:  int


@dataclass(frozen=True)
class EqualityGuard:
    target: str
    op:     str
    value:  int


@dataclass(frozen=True)
class CallGuard:
    callee: str
    args:   list[str]
    op:     str
    value:  int


def parser_candidates_from_ir(func: FunctionIR, ops: IrParserOps) -> list[BranchCandidate]:
    """
    Generate generic boundary candidates for parser-like numeric guards.

    This shaper is deliberately name-neutral. It does not know protocol names or
    field names. It only recognizes early-return comparisons against integer
    constants and creates boundary values around the guard.
    """
    candidates: list[BranchCandidate] = []
    seen: set[str] = set()

    for index, (stmt, condition, local_names, continuation_facts) in enumerate(_parser_guard_statements(func.statements)):
        if not _returns_from_guard(stmt):
            continue
        for guard in _numeric_guards(condition):
            if _target_references_local_root(guard.target, local_names):
                continue
            for label, value in _boundary_values(guard):
                family = _numeric_family(guard)
                name = ops.safe_c_name(f"ir_{family}_{index}_{guard.target}_{relation_name(guard.op)}_{guard.value}_{label}")
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    [f"{guard.target} = {value};"],
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:if[{index}]"),
                    target_branch=f"{family} guard {guard.target} {guard.op} {guard.value} {label}",
                    origin="ir",
                    object_paths=continuation_facts,
                    branch_facts=[BranchFact(guard.target, "==", str(value))],
                ))
        for guard in _equality_guards(condition):
            if _target_references_local_root(guard.target, local_names):
                continue
            for label, value in _equality_values(guard):
                family = _equality_family(guard)
                name = ops.safe_c_name(f"ir_{family}_{index}_{guard.target}_{relation_name(guard.op)}_{guard.value}_{label}")
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    [f"{guard.target} = {value};"],
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:if[{index}]"),
                    target_branch=f"{family} guard {guard.target} {guard.op} {guard.value} {label}",
                    origin="ir",
                    object_paths=continuation_facts,
                    branch_facts=[BranchFact(guard.target, "==", str(value))],
                ))
        for guard in _call_guards(condition):
            safe_callee = ops.safe_c_name(guard.callee)
            for label in ("success", "failure"):
                name = ops.safe_c_name(f"ir_call_guard_{index}_{safe_callee}_{relation_name(guard.op)}_{guard.value}_{label}")
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    _helper_setup(guard, label, ops.helper_call_rules, ops),
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:if[{index}]"),
                    target_branch=f"call guard {guard.callee} {guard.op} {guard.value} {label}",
                    witness_outputs=True,
                    origin="ir",
                    object_paths=continuation_facts,
                    call_facts=[CallOutcomeFact(guard.callee, f"{relation_name(guard.op)}_{guard.value}", label)],
                ))

    return candidates


def _parser_guard_statements(
    statements: list[Stmt],
    aliases: AliasMap | None = None,
    local_names: set[str] | None = None,
) -> list[tuple[IfStmt, Expr, set[str], list[ObjectPathFact]]]:
    found: list[tuple[IfStmt, Expr, set[str], list[ObjectPathFact]]] = []
    current_aliases = dict(aliases or {})
    current_locals = set(local_names or set())
    for index, stmt in enumerate(statements):
        if isinstance(stmt, DeclarationStmt):
            current_locals.add(stmt.name)
        record_alias(stmt, current_aliases)
        if isinstance(stmt, IfStmt):
            found.append((
                stmt,
                resolve_aliases(stmt.condition, current_aliases),
                set(current_locals),
                _object_path_facts_from_statements(statements[index + 1:]),
            ))
            found.extend(_parser_guard_statements(stmt.body, dict(current_aliases), set(current_locals)))
        elif isinstance(stmt, LoopStmt):
            found.extend(_parser_guard_statements(stmt.body, dict(current_aliases), set(current_locals)))
        elif isinstance(stmt, SwitchStmt):
            found.extend(_parser_guard_statements(stmt.body, dict(current_aliases), set(current_locals)))
    return found


def _target_references_local_root(target: str, local_names: set[str]) -> bool:
    if not local_names:
        return False
    root = _assignment_root(target)
    if root in local_names:
        return True
    for name in local_names:
        escaped = re.escape(name)
        if re.search(rf"(?<![A-Za-z0-9_>.])\(?{escaped}\)?\s*(?:->|\.|\[)", target):
            return True
    return False


def _assignment_root(target: str) -> str | None:
    lhs = target.strip().lstrip("*& ")
    while True:
        casted = re.match(r"^\(\([^)]*\)\)\s*(.*)$", lhs)
        if not casted:
            break
        lhs = casted.group(1).lstrip("*& ")
    match = re.match(r"([A-Za-z_]\w*)", lhs)
    return match.group(1) if match else None


def _object_path_facts_from_statements(statements: list[Stmt]) -> list[ObjectPathFact]:
    facts: list[ObjectPathFact] = []

    def visit_value(value) -> None:
        if isinstance(value, Expr):
            facts.extend(object_path_facts_from_expr(value))
            for child in vars(value).values():
                visit_value(child)
        elif isinstance(value, Stmt):
            for child in vars(value).values():
                visit_value(child)
        elif isinstance(value, list):
            for item in value:
                visit_value(item)

    for stmt in statements:
        visit_value(stmt)
    return _dedup_object_paths(facts)


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


def _returns_from_guard(stmt: IfStmt) -> bool:
    return body_has_return(stmt.body)


def _numeric_guards(expr: Expr) -> list[NumericGuard]:
    if isinstance(expr, BinaryOp) and expr.op == "&&":
        return [*_numeric_guards(expr.left), *_numeric_guards(expr.right)]
    if isinstance(expr, BinaryOp) and expr.op == "||":
        return [*_numeric_guards(expr.left), *_numeric_guards(expr.right)]
    if not isinstance(expr, BinaryOp) or expr.op not in {"<", "<=", ">", ">="}:
        return []

    left_target = assignable_expr(expr.left)
    right_value = int_value(expr.right)
    if left_target is not None and right_value is not None:
        return [NumericGuard(left_target, expr.op, right_value)]

    right_target = assignable_expr(expr.right)
    left_value = int_value(expr.left)
    if right_target is not None and left_value is not None:
        return [NumericGuard(right_target, flipped_relation(expr.op), left_value)]

    return []


def _equality_guards(expr: Expr) -> list[EqualityGuard]:
    if isinstance(expr, BinaryOp) and expr.op == "&&":
        return [*_equality_guards(expr.left), *_equality_guards(expr.right)]
    if isinstance(expr, BinaryOp) and expr.op == "||":
        return [*_equality_guards(expr.left), *_equality_guards(expr.right)]
    if not isinstance(expr, BinaryOp) or expr.op not in {"==", "!="}:
        return []

    left_target = assignable_expr(expr.left)
    right_value = int_value(expr.right)
    if left_target is not None and right_value is not None:
        return [EqualityGuard(left_target, expr.op, right_value)]

    right_target = assignable_expr(expr.right)
    left_value = int_value(expr.left)
    if right_target is not None and left_value is not None:
        return [EqualityGuard(right_target, expr.op, left_value)]

    return []


def _call_guards(expr: Expr) -> list[CallGuard]:
    if isinstance(expr, BinaryOp) and expr.op == "&&":
        return [*_call_guards(expr.left), *_call_guards(expr.right)]
    if isinstance(expr, BinaryOp) and expr.op == "||":
        return [*_call_guards(expr.left), *_call_guards(expr.right)]
    if not isinstance(expr, BinaryOp) or expr.op not in {"==", "!="}:
        return []

    right_value = int_value(expr.right)
    if isinstance(expr.left, CallExpr) and right_value is not None:
        return [CallGuard(expr.left.callee, _arg_texts(expr.left), expr.op, right_value)]

    left_value = int_value(expr.left)
    if isinstance(expr.right, CallExpr) and left_value is not None:
        return [CallGuard(expr.right.callee, _arg_texts(expr.right), expr.op, left_value)]

    return []


def _boundary_values(guard: NumericGuard) -> list[tuple[str, int]]:
    value = guard.value
    below = max(value - 1, 0)
    above = value + 1
    if guard.op in {"<", "<="}:
        return [
            ("too_low", below),
            ("boundary", value),
            ("valid_high", above),
        ]
    return [
        ("valid_low", below),
        ("boundary", value),
        ("too_high", above),
    ]


def _equality_values(guard: EqualityGuard) -> list[tuple[str, int]]:
    other = guard.value + 1 if guard.value != 255 else guard.value - 1
    if guard.op == "!=":
        return [
            ("required", guard.value),
            ("other", other),
        ]
    return [
        ("forbidden", guard.value),
        ("allowed", other),
    ]


def _numeric_family(guard: NumericGuard) -> str:
    return "min_guard" if guard.op in {"<", "<="} else "max_guard"


def _equality_family(guard: EqualityGuard) -> str:
    return "required_value" if guard.op == "!=" else "forbidden_value"


def _helper_setup(guard: CallGuard, label: str, rules: tuple[HelperCallRule, ...], ops: IrParserOps | None = None) -> list[str]:
    for rule in rules:
        if rule.callee != guard.callee:
            continue
        templates = rule.success_setup if label == "success" else rule.failure_setup
        setup: list[str] = []
        for template in templates:
            rendered = _render_template(template, guard)
            if rendered:
                setup.append(rendered)
        return setup
    if ops is not None:
        return _helper_model_setup(guard, label, ops)
    return []


def _render_template(template: str, guard: CallGuard) -> str:
    out = template.replace("{callee}", guard.callee)
    for index, arg in enumerate(guard.args):
        out = out.replace(f"{{arg{index}}}", arg)
    if re.search(r"\{(?:arg\d+|callee)\}", out):
        return ""
    return out


def _helper_model_setup(guard: CallGuard, label: str, ops: IrParserOps) -> list[str]:
    helper_irs = ops.helper_irs or {}
    helper_params = ops.helper_params or {}
    helper_ir = helper_irs.get(guard.callee)
    param_names = helper_params.get(guard.callee, ())
    if helper_ir is None or len(param_names) != len(guard.args):
        return []

    desired = _desired_helper_return(guard, label)
    if desired not in {0, 1}:
        return []

    param_to_arg = dict(zip(param_names, guard.args))
    for stmt in helper_ir.statements:
        if isinstance(stmt, ReturnStmt) and stmt.value is not None:
            setup = _boolean_return_setup(stmt.value, desired, param_to_arg)
            if setup:
                return setup
    return []


def _desired_helper_return(guard: CallGuard, label: str) -> int | None:
    if guard.op == "!=":
        if label == "success":
            return guard.value
        return 1 if guard.value == 0 else 0
    if guard.op == "==":
        if label == "failure":
            return guard.value
        return 1 if guard.value == 0 else 0
    return None


def _boolean_return_setup(expr: Expr, desired: int, param_to_arg: dict[str, str]) -> list[str]:
    if not isinstance(expr, BinaryOp) or expr.op not in {"==", "!="}:
        return []
    left = assignable_expr(expr.left)
    right_value = int_value(expr.right)
    if left is not None and right_value is not None:
        return [_assignment_for_boolean_expr(left, expr.op, right_value, desired, param_to_arg)]

    right = assignable_expr(expr.right)
    left_value = int_value(expr.left)
    if right is not None and left_value is not None:
        return [_assignment_for_boolean_expr(right, expr.op, left_value, desired, param_to_arg)]
    return []


def _assignment_for_boolean_expr(
    target: str,
    op: str,
    value: int,
    desired: int,
    param_to_arg: dict[str, str],
) -> str:
    should_match = desired == 1 if op == "==" else desired == 0
    assigned = value if should_match else value + 1
    return f"{_rewrite_param_roots(target, param_to_arg)} = {assigned};"


def _rewrite_param_roots(expr: str, param_to_arg: dict[str, str]) -> str:
    out = expr
    for param, arg in sorted(param_to_arg.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(rf"\b{re.escape(param)}\b", arg, out)
    return out


def _arg_texts(call: CallExpr) -> list[str]:
    args: list[str] = []
    for arg in call.args:
        text = value_expr(arg)
        if text is None:
            return []
        args.append(text)
    return args
