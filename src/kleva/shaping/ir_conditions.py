from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ir.aliases import record_alias, resolve_aliases
from ..ir.model import (
    ArraySubscript,
    BinaryOp,
    BreakStmt,
    CallExpr,
    CastExpr,
    ContinueStmt,
    DeclarationStmt,
    Expr,
    FieldAccess,
    FunctionIR,
    IfStmt,
    IntLiteral,
    LoopStmt,
    ReturnStmt,
    Stmt,
    SwitchStmt,
    UnaryOp,
    VarRef,
)
from ..ir.relations import flipped_relation, negated_relation, relation_name
from ..ir.render import assignable_expr, is_pointer_expr, value_expr
from .candidates import BranchCandidate, BranchFact, ObjectPathFact, display_source_location, object_path_facts_from_expr
from .ir_byte_order import DecodedFieldAlias
from .ir_poststate import post_state_facts_from_direct_assignments


@dataclass(frozen=True)
class IrConditionOps:
    safe_c_name:       Callable[[str], str]
    nonmatching_value: Callable[[str], str]
    decoded_aliases:   dict[str, DecodedFieldAlias] | None = None
    encode_fn:         Callable[[str], str] | None = None
    pointer_like_types: frozenset[str] = frozenset()


def condition_candidates_from_ir(func: FunctionIR, ops: IrConditionOps) -> list[BranchCandidate]:
    """
    Generate generic branch candidates from typed IR conditions.

    This is intentionally domain-neutral. It knows expression shapes such as
    comparisons, boolean OR, boolean AND, and unary NOT; it does not know names
    like packet, socket, TCP, or simulator.
    """
    candidates: list[BranchCandidate] = []
    seen: set[str] = set()

    for index, (stmt, condition, local_names, continuation_facts, path_conditions) in enumerate(_if_conditions_with_aliases(func.statements, {}, set(), [])):
        true_post_state_facts = post_state_facts_from_direct_assignments(stmt.body)
        true_branch_exits = _body_exits(stmt.body)
        path_preconditions = _path_precondition_alternatives(path_conditions, ops)
        for suffix, setup, branch_text, facts, branch_facts in condition_setup_alternatives(condition, ops):
            if not setup:
                continue
            for path_setup, path_branch_text, path_facts, path_branch_facts in path_preconditions:
                combined_setup = _dedup_setup([*path_setup, *setup])
                if not combined_setup:
                    continue
                combined_branch_text = (
                    f"{path_branch_text}; {branch_text}"
                    if path_branch_text
                    else branch_text
                )
                combined_facts = [*path_facts, *facts]
                combined_branch_facts = [*path_branch_facts, *branch_facts]
                reachable_continuation_facts = (
                    continuation_facts
                    if suffix.startswith("false_") or not true_branch_exits
                    else []
                )
                if _setup_references_local_root(combined_setup, local_names):
                    continue
                name = _clean_name(ops.safe_c_name(f"ir_if_{index}_{suffix}"))
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    combined_setup,
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:if[{index}]"),
                    target_branch=f"if {combined_branch_text}",
                    origin="ir",
                    object_paths=_dedup_object_paths([*combined_facts, *reachable_continuation_facts]),
                    branch_facts=combined_branch_facts,
                    post_state_facts=[] if suffix.startswith("false_") else true_post_state_facts,
                ))

    return candidates


def _if_conditions_with_aliases(
    statements:   list[Stmt],
    aliases:      dict[str, Expr],
    local_names:  set[str],
    path_conditions: list[tuple[Expr, str]],
) -> list[tuple[IfStmt, Expr, set[str], list[ObjectPathFact], list[tuple[Expr, str]]]]:
    found: list[tuple[IfStmt, Expr, set[str], list[ObjectPathFact], list[tuple[Expr, str]]]] = []
    current_aliases = dict(aliases)
    current_locals  = set(local_names)
    current_path_conditions = list(path_conditions)
    for index, stmt in enumerate(statements):
        if isinstance(stmt, DeclarationStmt):
            current_locals.add(stmt.name)
        record_alias(stmt, current_aliases)
        if isinstance(stmt, IfStmt):
            resolved_condition = resolve_aliases(stmt.condition, current_aliases)
            continuation_facts = _object_path_facts_from_statements(statements[index + 1:])
            found.append((
                stmt,
                resolved_condition,
                set(current_locals),
                continuation_facts,
                list(current_path_conditions),
            ))
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
            if _body_exits(stmt.body):
                current_path_conditions.append((resolved_condition, "false"))
        elif isinstance(stmt, LoopStmt):
            loop_condition = resolve_aliases(stmt.condition, current_aliases)
            found.extend(_if_conditions_with_aliases(
                stmt.body,
                dict(current_aliases),
                set(current_locals),
                [*current_path_conditions, (loop_condition, "true")],
            ))
        elif isinstance(stmt, SwitchStmt):
            found.extend(_if_conditions_with_aliases(stmt.body, dict(current_aliases), set(current_locals), list(current_path_conditions)))
    return found


def _body_exits(body: list[Stmt]) -> bool:
    return any(isinstance(stmt, (BreakStmt, ContinueStmt, ReturnStmt)) for stmt in body)


def _path_precondition_alternatives(
    conditions: list[tuple[Expr, str]],
    ops:        IrConditionOps,
) -> list[tuple[list[str], str, list, list[BranchFact]]]:
    alternatives: list[tuple[list[str], str, list, list[BranchFact]]] = [([], "", [], [])]
    for condition, polarity in conditions:
        condition_alternatives = (
            _true_setup_alternatives(condition, ops)
            if polarity == "true"
            else _false_setup_alternatives(condition, ops)
        )
        if not condition_alternatives:
            continue
        next_alternatives: list[tuple[list[str], str, list, list[BranchFact]]] = []
        for base_setup, base_text, base_facts, base_branch_facts in alternatives:
            for _suffix, setup, branch_text, facts, branch_facts in condition_alternatives:
                next_alternatives.append((
                    _dedup_setup([*base_setup, *setup]),
                    f"{base_text}; {branch_text}" if base_text else branch_text,
                    [*base_facts, *facts],
                    [*base_branch_facts, *branch_facts],
                ))
        alternatives = next_alternatives
    return alternatives


def path_precondition_alternatives(
    conditions: list[Expr],
    ops:        IrConditionOps,
) -> list[tuple[list[str], str, list, list[BranchFact]]]:
    """
    Return setup alternatives for selected-path trigger conditions.

    A selected path can only reach a later statement when earlier terminating
    guards are false. This helper exposes that trigger computation to other IR
    shapers without making them parse or reason from source text.
    """
    return _path_precondition_alternatives([(condition, "false") for condition in conditions], ops)


def _dedup_setup(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _setup_references_local_root(setup: list[str], local_names: set[str]) -> bool:
    if not local_names:
        return False
    return any(
        _setup_line_mentions_local_root(line, local_names)
        for line in setup
    )


def _setup_line_mentions_local_root(line: str, local_names: set[str]) -> bool:
    root = _setup_line_root(line)
    if root in local_names:
        return True
    for name in local_names:
        escaped = re.escape(name)
        if re.search(rf"(?<![A-Za-z0-9_>.])\(?{escaped}\)?\s*(?:->|\.|\[)", line):
            return True
    return False


def _setup_line_root(line: str) -> str | None:
    guard = re.match(r"^\s*if\s*\(\s*!\s*([A-Za-z_]\w*)\s*\)\s*return\b", line)
    if guard:
        return guard.group(1)
    guard_marker = re.match(r"^\s*__GUARD__\(\s*([A-Za-z_]\w*)\s*\)\s*$", line)
    if guard_marker:
        return guard_marker.group(1)
    return _assignment_root(line)


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


def condition_setup_alternatives(expr: Expr, ops: IrConditionOps) -> list[tuple[str, list[str], str, list, list[BranchFact]]]:
    """Return typed setup alternatives that make an IR condition true or false."""
    if isinstance(expr, BinaryOp) and expr.op == "||":
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for label, side in (("left", expr.left), ("right", expr.right)):
            for suffix, setup, branch_text, facts, branch_facts in _true_setup_alternatives(side, ops):
                out.append((f"{label}_{suffix}", setup, branch_text, facts, branch_facts))
        false_left = _false_setup_alternatives(expr.left, ops)
        false_right = _false_setup_alternatives(expr.right, ops)
        for left_suffix, left_setup, left_branch, left_facts, left_branch_facts in false_left:
            for right_suffix, right_setup, right_branch, right_facts, right_branch_facts in false_right:
                out.append((
                    f"false_{left_suffix}_and_{right_suffix}",
                    [*left_setup, *right_setup],
                    f"({left_branch}) && ({right_branch})",
                    [*left_facts, *right_facts],
                    [*left_branch_facts, *right_branch_facts],
                ))
        return out

    if isinstance(expr, BinaryOp) and expr.op == "&&":
        left = _true_setup_alternatives(expr.left, ops)
        right = _true_setup_alternatives(expr.right, ops)
        if not left or not right:
            return []
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for left_suffix, left_setup, left_branch, left_facts, left_branch_facts in left:
            for right_suffix, right_setup, right_branch, right_facts, right_branch_facts in right:
                out.append((
                    f"{left_suffix}_and_{right_suffix}",
                    [*left_setup, *right_setup],
                    f"({left_branch}) && ({right_branch})",
                    [*left_facts, *right_facts],
                    [*left_branch_facts, *right_branch_facts],
                ))
        for label, side in (("left", expr.left), ("right", expr.right)):
            for suffix, setup, branch_text, facts, branch_facts in _false_setup_alternatives(side, ops):
                out.append((f"false_{label}_{suffix}", setup, branch_text, facts, branch_facts))
        return out

    bitwise = _bitwise_flag(expr)
    if bitwise:
        target, mask, facts = bitwise
        return [
            (f"{target}_has_{mask}", [f"{target} |= {mask};"], f"{target} & {mask}", facts, [BranchFact(target, "&", mask)]),
            (f"false_{target}_has_{mask}", [f"{target} = 0;"], f"!({target} & {mask})", facts, [BranchFact(target, "!&", mask)]),
        ]

    if isinstance(expr, BinaryOp) and expr.op in {"==", "!=", ">", ">=", "<", "<="}:
        decoded = _decoded_comparison(expr, ops)
        if decoded:
            return decoded
        comparison = _comparison_parts(expr)
        if comparison is None:
            return []
        target, op, rhs, target_expr = comparison
        normalized = BinaryOp(op, target_expr, IntLiteral(0) if _is_null_value(rhs) else expr.right, expr.c_type)
        if _is_pointer_like_expr(target_expr, ops):
            return [*_pointer_setup_alternatives(normalized, target, rhs, ops), *_false_setup_alternatives(expr, ops)]
        value = _value_for_relation(op, rhs, ops)
        setup = _coordinated_field_setup(target, op, rhs, expr.right, ops)
        if not setup:
            setup = [f"{target} = {value};"]
        suffix = f"{target}_{relation_name(op)}_{rhs}"
        true_candidate = (suffix, setup, f"{target} {op} {rhs}", object_path_facts_from_expr(target_expr), [BranchFact(target, op, rhs)])
        return [true_candidate, *_false_setup_alternatives(expr, ops)]

    if isinstance(expr, UnaryOp) and expr.op == "!":
        bitwise = _bitwise_flag(expr.operand)
        if bitwise:
            target, mask, facts = bitwise
            return [
                (f"not_{target}_has_{mask}", [f"{target} = 0;"], f"!({target} & {mask})", facts, [BranchFact(target, "!&", mask)]),
                (f"false_not_{target}_has_{mask}", [f"{target} |= {mask};"], f"{target} & {mask}", facts, [BranchFact(target, "&", mask)]),
            ]
        target = assignable_expr(expr.operand)
        if target is None:
            return []
        return [
            (f"not_{target}", [f"{target} = 0;"], f"!{target}", object_path_facts_from_expr(expr.operand), [BranchFact(target, "==", "0")]),
            (f"false_not_{target}", _non_null_setup(expr.operand, target, ops), f"!!{target}", object_path_facts_from_expr(expr.operand), [BranchFact(target, "!=", "0")]),
        ]

    target = assignable_expr(expr)
    if target is not None:
        return [
            (f"truthy_{target}", _non_null_setup(expr, target, ops), target, object_path_facts_from_expr(expr), [BranchFact(target, "!=", "0")]),
            (f"false_truthy_{target}", [f"{target} = 0;"], f"!{target}", object_path_facts_from_expr(expr), [BranchFact(target, "==", "0")]),
        ]

    return []


def true_condition_setup_alternatives(expr: Expr, ops: IrConditionOps) -> list[tuple[str, list[str], str, list, list[BranchFact]]]:
    """Return typed setup alternatives that make an IR condition true."""
    return _true_setup_alternatives(expr, ops)


def _true_setup_alternatives(expr: Expr, ops: IrConditionOps) -> list[tuple[str, list[str], str, list, list[BranchFact]]]:
    if isinstance(expr, BinaryOp) and expr.op == "||":
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for label, side in (("left", expr.left), ("right", expr.right)):
            for suffix, setup, branch_text, facts, branch_facts in _true_setup_alternatives(side, ops):
                out.append((f"{label}_{suffix}", setup, branch_text, facts, branch_facts))
        return out

    if isinstance(expr, BinaryOp) and expr.op == "&&":
        left = _true_setup_alternatives(expr.left, ops)
        right = _true_setup_alternatives(expr.right, ops)
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for left_suffix, left_setup, left_branch, left_facts, left_branch_facts in left:
            for right_suffix, right_setup, right_branch, right_facts, right_branch_facts in right:
                out.append((
                    f"{left_suffix}_and_{right_suffix}",
                    [*left_setup, *right_setup],
                    f"({left_branch}) && ({right_branch})",
                    [*left_facts, *right_facts],
                    [*left_branch_facts, *right_branch_facts],
                ))
        return out

    bitwise = _bitwise_flag(expr)
    if bitwise:
        target, mask, facts = bitwise
        return [(f"{target}_has_{mask}", [f"{target} |= {mask};"], f"{target} & {mask}", facts, [BranchFact(target, "&", mask)])]

    if isinstance(expr, BinaryOp) and expr.op in {"==", "!=", ">", ">=", "<", "<="}:
        decoded = _decoded_comparison(expr, ops, false_only=False)
        if decoded:
            return decoded[:1]
        comparison = _comparison_parts(expr)
        if comparison is None:
            return []
        target, op, rhs, target_expr = comparison
        normalized = BinaryOp(op, target_expr, IntLiteral(0) if _is_null_value(rhs) else expr.right, expr.c_type)
        if _is_pointer_like_expr(target_expr, ops):
            return _pointer_setup_alternatives(normalized, target, rhs, ops)
        value = _value_for_relation(op, rhs, ops)
        setup = _coordinated_field_setup(target, op, rhs, expr.right, ops)
        if not setup:
            setup = [f"{target} = {value};"]
        suffix = f"{target}_{relation_name(op)}_{rhs}"
        return [(suffix, setup, f"{target} {op} {rhs}", object_path_facts_from_expr(target_expr), [BranchFact(target, op, rhs)])]

    if isinstance(expr, UnaryOp) and expr.op == "!":
        bitwise = _bitwise_flag(expr.operand)
        if bitwise:
            target, mask, facts = bitwise
            return [(f"not_{target}_has_{mask}", [f"{target} = 0;"], f"!({target} & {mask})", facts, [BranchFact(target, "!&", mask)])]
        target = assignable_expr(expr.operand)
        if target is None:
            return []
        return [(f"not_{target}", [f"{target} = 0;"], f"!{target}", object_path_facts_from_expr(expr.operand), [BranchFact(target, "==", "0")])]

    target = assignable_expr(expr)
    if target is not None:
        return [(f"truthy_{target}", _non_null_setup(expr, target, ops), target, object_path_facts_from_expr(expr), [BranchFact(target, "!=", "0")])]

    return []


def _false_setup_alternatives(expr: Expr, ops: IrConditionOps) -> list[tuple[str, list[str], str, list, list[BranchFact]]]:
    if isinstance(expr, BinaryOp) and expr.op == "||":
        left = _false_setup_alternatives(expr.left, ops)
        right = _false_setup_alternatives(expr.right, ops)
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for left_suffix, left_setup, left_branch, left_facts, left_branch_facts in left:
            for right_suffix, right_setup, right_branch, right_facts, right_branch_facts in right:
                out.append((
                    f"{left_suffix}_and_{right_suffix}",
                    _dedup_setup([*left_setup, *right_setup]),
                    f"({left_branch}) && ({right_branch})",
                    [*left_facts, *right_facts],
                    [*left_branch_facts, *right_branch_facts],
                ))
        return out

    if isinstance(expr, BinaryOp) and expr.op == "&&":
        out: list[tuple[str, list[str], str, list, list[BranchFact]]] = []
        for label, side in (("left", expr.left), ("right", expr.right)):
            for suffix, setup, branch_text, facts, branch_facts in _false_setup_alternatives(side, ops):
                out.append((f"{label}_{suffix}", setup, branch_text, facts, branch_facts))
        return out

    bitwise = _bitwise_flag(expr)
    if bitwise:
        target, mask, facts = bitwise
        return [(f"false_{target}_has_{mask}", [f"{target} = 0;"], f"!({target} & {mask})", facts, [BranchFact(target, "!&", mask)])]

    if isinstance(expr, BinaryOp) and expr.op in {"==", "!=", ">", ">=", "<", "<="}:
        decoded = _decoded_comparison(expr, ops, true_only=False)
        if decoded:
            return decoded[-1:]
        comparison = _comparison_parts(expr)
        if comparison is None:
            return []
        target, op, rhs, target_expr = comparison
        false_op = negated_relation(op)
        if _is_pointer_like_expr(target_expr, ops):
            return _pointer_setup_alternatives(
                BinaryOp(false_op, target_expr, IntLiteral(0) if _is_null_value(rhs) else expr.right, expr.c_type),
                target,
                rhs,
                ops,
                prefix="false_",
            )
        value = _value_for_relation(false_op, rhs, ops)
        setup = _coordinated_field_setup(target, false_op, rhs, expr.right, ops)
        if not setup:
            setup = [f"{target} = {value};"]
        suffix = f"false_{target}_{relation_name(false_op)}_{rhs}"
        return [(suffix, setup, f"{target} {false_op} {rhs}", object_path_facts_from_expr(target_expr), [BranchFact(target, false_op, rhs)])]
    if isinstance(expr, UnaryOp) and expr.op == "!":
        bitwise = _bitwise_flag(expr.operand)
        if bitwise:
            target, mask, facts = bitwise
            return [(f"false_not_{target}_has_{mask}", [f"{target} |= {mask};"], f"{target} & {mask}", facts, [BranchFact(target, "&", mask)])]
        target = assignable_expr(expr.operand)
        if target is None:
            return []
        return [(f"false_not_{target}", _non_null_setup(expr.operand, target, ops), f"!!{target}", object_path_facts_from_expr(expr.operand), [BranchFact(target, "!=", "0")])]
    target = assignable_expr(expr)
    if target is not None:
        return [(f"false_truthy_{target}", [f"{target} = 0;"], f"!{target}", object_path_facts_from_expr(expr), [BranchFact(target, "==", "0")])]
    return []


def _value_for_relation(op: str, rhs: str, ops: IrConditionOps) -> str:
    if op == "==":
        return rhs
    if op == "!=":
        return ops.nonmatching_value(rhs)
    if op == ">":
        return f"(({rhs}) + 1)"
    if op == ">=":
        return rhs
    if op == "<":
        return f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
    return rhs


def _coordinated_field_setup(
    target:   str,
    op:       str,
    rhs:      str,
    rhs_expr: Expr,
    ops:      IrConditionOps,
) -> list[str]:
    rhs_target = assignable_expr(rhs_expr)
    if rhs_target is None or rhs_target == target or rhs_target != rhs:
        return []
    if "->" not in rhs_target and "." not in rhs_target:
        return []

    rhs_value = _small_rhs_value_for_relation(op)
    if "->" not in target and "." not in target:
        return [f"{rhs_target} = {rhs_value};"]

    target_value = _value_for_relation(op, rhs_target, ops)
    return [
        f"{rhs_target} = {rhs_value};",
        f"{target} = {target_value};",
    ]


def _small_rhs_value_for_relation(op: str) -> str:
    if op in {"<", ">"}:
        return "2" if op == "<" else "1"
    if op == "!=":
        return "0"
    return "1"


def _decoded_comparison(
    expr: BinaryOp,
    ops: IrConditionOps,
    true_only: bool = True,
    false_only: bool = True,
) -> list[tuple[str, list[str], str, list, list[BranchFact]]]:
    aliases = ops.decoded_aliases or {}
    encode_fn_for = ops.encode_fn
    if not aliases or encode_fn_for is None:
        return []

    target_name = assignable_expr(expr.left)
    rhs = value_expr(expr.right)
    op = expr.op
    if target_name not in aliases or rhs is None:
        target_name = assignable_expr(expr.right)
        rhs = value_expr(expr.left)
        op = flipped_relation(expr.op)
    if target_name not in aliases or rhs is None:
        return []

    alias = aliases[target_name]
    encode_fn = encode_fn_for(alias.decode_fn)
    if not encode_fn:
        return []

    true_value = _value_for_relation(op, rhs, ops)
    false_op = negated_relation(op)
    false_value = _value_for_relation(false_op, rhs, ops)
    true_candidate = (
        f"{target_name}_{relation_name(op)}_{rhs}",
        [f"{alias.target} = {encode_fn}({true_value});"],
        f"{target_name} {op} {rhs}",
        [],
        [BranchFact(alias.target, op, rhs)],
    )
    false_candidate = (
        f"false_{target_name}_{relation_name(false_op)}_{rhs}",
        [f"{alias.target} = {encode_fn}({false_value});"],
        f"{target_name} {false_op} {rhs}",
        [],
        [BranchFact(alias.target, false_op, rhs)],
    )
    if true_only and not false_only:
        return [true_candidate]
    if false_only and not true_only:
        return [false_candidate]
    return [true_candidate, false_candidate]


def _comparison_parts(expr: BinaryOp) -> tuple[str, str, str, Expr] | None:
    target = assignable_expr(expr.left)
    rhs = value_expr(expr.right)
    if target is not None and rhs is not None:
        return target, expr.op, rhs, expr.left

    target = assignable_expr(expr.right)
    rhs = value_expr(expr.left)
    if target is not None and rhs is not None:
        return target, flipped_relation(expr.op), rhs, expr.right

    return None


def _bitwise_flag(expr: Expr) -> tuple[str, str, list] | None:
    if not isinstance(expr, BinaryOp) or expr.op != "&":
        return None

    target = assignable_expr(expr.left)
    mask = value_expr(expr.right)
    facts_expr = expr.left
    if target is None or mask is None:
        target = assignable_expr(expr.right)
        mask = value_expr(expr.left)
        facts_expr = expr.right

    if target is None or mask is None:
        return None
    return target, mask, object_path_facts_from_expr(facts_expr)


def _clean_name(name: str) -> str:
    return re.sub(r"_+", "_", name).strip("_")


def _pointer_setup_alternatives(
    expr: BinaryOp,
    target: str,
    rhs: str,
    ops: IrConditionOps,
    prefix: str = "",
) -> list[tuple[str, list[str], str, list]]:
    if not _is_null_value(rhs):
        return []
    if expr.op == "==":
        setup = [f"{target} = {_null_assignment_value(expr.left, ops)};"]
    elif expr.op == "!=":
        setup = _non_null_setup(expr.left, target, ops)
    else:
        return []
    suffix = f"{prefix}{target}_{relation_name(expr.op)}_{rhs}"
    return [(suffix, setup, f"{target} {expr.op} {rhs}", object_path_facts_from_expr(expr.left), [BranchFact(target, expr.op, rhs)])]


def _is_null_value(value: str) -> bool:
    return value in {"0", "NULL", "\\null"}


def _is_pointer_like_expr(expr: Expr, ops: IrConditionOps) -> bool:
    if is_pointer_expr(expr):
        return True
    c_type = getattr(expr, "c_type", None)
    return isinstance(c_type, str) and c_type.strip() in ops.pointer_like_types


def _null_assignment_value(expr: Expr, ops: IrConditionOps) -> str:
    if _is_pointer_like_expr(expr, ops):
        return "NULL"
    return "0"


def _non_null_assignment_value(expr: Expr, ops: IrConditionOps) -> str:
    c_type = getattr(expr, "c_type", None)
    if isinstance(c_type, str) and "*" in c_type:
        return f"(({c_type})1)"
    if isinstance(c_type, str) and c_type.strip() in ops.pointer_like_types:
        return f"(({c_type})1)"
    return "1"


def _non_null_setup(expr: Expr, target: str, ops: IrConditionOps) -> list[str]:
    if target.strip().startswith("&"):
        return []
    if isinstance(expr, VarRef) and _is_pointer_like_expr(expr, ops):
        return [f"__GUARD__({target})"]
    if _is_pointer_like_expr(expr, ops):
        return [f"/* kleva: non-null pointer path {target} backed by fixture */"]
    return [f"{target} = {_non_null_assignment_value(expr, ops)};"]
