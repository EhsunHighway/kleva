from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir.aliases import AliasMap, record_alias, resolve_aliases
from ..ir.model import AddressOf, ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, DeclarationStmt, Expr, FieldAccess, FunctionIR, IfStmt, IntLiteral, LoopStmt, ReturnStmt, Stmt, SwitchStmt, UnaryOp, VarRef
from ..ir.naming import safe_name
from ..ir.render import assignable_expr
from .candidates import BranchCandidate, BranchFact, display_source_location
from .ir_conditions import IrConditionOps, true_condition_setup_alternatives


@dataclass(frozen=True)
class LookupHitAlternative:
    setup:        list[str]
    branch_facts: list[BranchFact]


def fallback_lookup_candidates_from_ir(
    func: FunctionIR,
    condition_ops: IrConditionOps,
    helper_irs: dict[str, FunctionIR] | None = None,
    helper_params: dict[str, tuple[str, ...]] | None = None,
) -> list[BranchCandidate]:
    """
    Generate candidates for generic fallback lookup flow:

        exact = exact_lookup(...);
        if (!exact && guard) {
            fallback = fallback_lookup(...);
        }
        if (fallback) { ... }

    The helper-specific knowledge is inferred from helper IR, not names: a
    helper hit is a path that returns an address of an array element.
    """
    helper_irs = helper_irs or {}
    helper_params = helper_params or {}
    candidates: list[BranchCandidate] = []
    seen: set[str] = set()
    aliases: AliasMap = {}
    declarations: dict[str, CallExpr] = {}

    for index, stmt in enumerate(func.statements):
        record_alias(stmt, aliases)
        if isinstance(stmt, DeclarationStmt) and isinstance(stmt.init, CallExpr):
            declarations[stmt.name] = stmt.init
            continue
        if not isinstance(stmt, IfStmt):
            continue
        shape = _fallback_assignment(stmt)
        if shape is None:
            continue
        exact_var, guard_expr, fallback_var, fallback_call = shape
        exact_call = declarations.get(exact_var)
        if exact_call is None:
            continue
        if not _has_truthy_if_after(func.statements[index + 1:], fallback_var):
            continue

        guard_alternatives = true_condition_setup_alternatives(resolve_aliases(guard_expr, aliases), condition_ops)
        guard_setups = [setup for _suffix, setup, _text, _paths, _facts in guard_alternatives if setup]
        if not guard_setups:
            guard_setups = [[]]

        fallback_hits = _lookup_hit_alternatives(fallback_call, helper_irs, helper_params, condition_ops, aliases)
        if not fallback_hits:
            continue
        exact_miss = _lookup_miss_setup(exact_call, fallback_call, helper_irs, helper_params, condition_ops, aliases)

        for alt_index, hit in enumerate(fallback_hits, 1):
            for guard_index, guard_setup in enumerate(guard_setups, 1):
                name = safe_name(f"ir_fallback_lookup_{fallback_var}_{alt_index}_{guard_index}", "lookup")
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(BranchCandidate(
                    name,
                    [*guard_setup, *exact_miss, *hit.setup],
                    source_location=display_source_location(stmt.loc, f"ir:{func.name}:fallback_lookup[{index}]"),
                    target_branch=f"fallback lookup {fallback_var} hit",
                    origin="ir",
                    branch_facts=hit.branch_facts,
                ))
    return candidates


def _fallback_assignment(stmt: IfStmt) -> tuple[str, Expr, str, CallExpr] | None:
    if not isinstance(stmt.condition, BinaryOp) or stmt.condition.op != "&&":
        return None
    exact_var = _negated_var(stmt.condition.left)
    guard_expr = stmt.condition.right
    if exact_var is None:
        exact_var = _negated_var(stmt.condition.right)
        guard_expr = stmt.condition.left
    if exact_var is None:
        return None
    for nested in stmt.body:
        if isinstance(nested, AssignmentStmt) and isinstance(nested.target, VarRef) and isinstance(nested.value, CallExpr):
            return exact_var, guard_expr, nested.target.name, nested.value
    return None


def _negated_var(expr: Expr) -> str | None:
    if isinstance(expr, UnaryOp) and expr.op == "!" and isinstance(expr.operand, VarRef):
        return expr.operand.name
    return None


def _has_truthy_if_after(statements: list[Stmt], name: str) -> bool:
    for stmt in statements:
        if isinstance(stmt, IfStmt) and isinstance(stmt.condition, VarRef) and stmt.condition.name == name:
            return True
    return False


def _lookup_hit_alternatives(
    call: CallExpr,
    helper_irs: dict[str, FunctionIR],
    helper_params: dict[str, tuple[str, ...]],
    condition_ops: IrConditionOps,
    caller_aliases: AliasMap,
) -> list[LookupHitAlternative]:
    helper_ir = helper_irs.get(call.callee)
    params = helper_params.get(call.callee, ())
    if helper_ir is None or len(params) != len(call.args):
        return []
    arg_by_param = {name: resolve_aliases(arg, caller_aliases) for name, arg in zip(params, call.args)}
    out: list[LookupHitAlternative] = []
    for condition, aliases in _return_slot_conditions(helper_ir.statements, {}):
        mapped_aliases = {
            name: _replace_param_refs(expr, arg_by_param)
            for name, expr in aliases.items()
        }
        mapped_condition = _replace_param_refs(resolve_aliases(condition, mapped_aliases), arg_by_param)
        for _suffix, setup, _branch_text, _paths, facts in true_condition_setup_alternatives(mapped_condition, condition_ops):
            if not setup:
                continue
            setup = _normalize_array_field_lines(setup)
            facts = _normalize_array_field_facts(facts)
            setup, facts = _materialize_decoded_rhs(setup, facts, condition_ops)
            out.append(LookupHitAlternative(setup, facts))
    return _dedup_alternatives(out)


def _lookup_miss_setup(
    exact_call: CallExpr,
    fallback_call: CallExpr,
    helper_irs: dict[str, FunctionIR],
    helper_params: dict[str, tuple[str, ...]],
    condition_ops: IrConditionOps,
    caller_aliases: AliasMap,
) -> list[str]:
    hits = _lookup_hit_alternatives(exact_call, helper_irs, helper_params, condition_ops, caller_aliases)
    fallback_hits = _lookup_hit_alternatives(fallback_call, helper_irs, helper_params, condition_ops, caller_aliases)
    fallback_targets = {fact.target for hit in fallback_hits for fact in hit.branch_facts}
    for hit in hits:
        for fact in hit.branch_facts:
            if fact.target in fallback_targets:
                continue
            miss = _miss_line_for_fact(fact)
            if miss:
                return [miss]
    return []


def _miss_line_for_fact(fact: BranchFact) -> str | None:
    if fact.relation == "==":
        return f"{fact.target} = {_nonmatching_value(fact.value)};"
    if fact.relation == "!=":
        return f"{fact.target} = {fact.value};"
    return None


def _normalize_array_field_lines(lines: list[str]) -> list[str]:
    return [_normalize_array_field_text(line) for line in lines]


def _normalize_array_field_facts(facts: list[BranchFact]) -> list[BranchFact]:
    return [
        BranchFact(
            _normalize_array_field_text(fact.target),
            fact.relation,
            _normalize_array_field_text(fact.value),
        )
        for fact in facts
    ]


def _normalize_array_field_text(text: str) -> str:
    return re.sub(r"(\[[^\]]+\])->", r"\1.", text)


def _materialize_decoded_rhs(
    setup: list[str],
    facts: list[BranchFact],
    ops: IrConditionOps,
) -> tuple[list[str], list[BranchFact]]:
    decoded_aliases = ops.decoded_aliases or {}
    encode_fn_for = ops.encode_fn
    if not decoded_aliases or encode_fn_for is None:
        return setup, facts

    out_setup: list[str] = []
    extra_facts: list[BranchFact] = []
    replacements: dict[str, str] = {}
    for line in setup:
        match = re.fullmatch(r"(.+?)\s*=\s*([A-Za-z_]\w*)\s*;", line.strip())
        if not match or match.group(2) not in decoded_aliases:
            out_setup.append(line)
            continue
        lhs, rhs = match.groups()
        alias = decoded_aliases[rhs]
        encode_fn = encode_fn_for(alias.decode_fn)
        if not encode_fn:
            out_setup.append(line)
            continue
        value = "1"
        replacements[rhs] = value
        out_setup.append(f"{lhs} = {value};")
        out_setup.append(f"{alias.target} = {encode_fn}({value});")
        extra_facts.append(BranchFact(alias.target, "==", value))

    out_facts = [
        BranchFact(fact.target, fact.relation, replacements.get(fact.value, fact.value))
        for fact in facts
    ]
    return out_setup, [*out_facts, *extra_facts]


def _return_slot_conditions(statements: list[Stmt], aliases: AliasMap) -> list[tuple[Expr, AliasMap]]:
    found: list[tuple[Expr, AliasMap]] = []
    current_aliases = dict(aliases)
    for stmt in statements:
        _record_slot_alias(stmt, current_aliases)
        if isinstance(stmt, IfStmt):
            if _body_returns_slot(stmt.body, current_aliases):
                found.append((stmt.condition, dict(current_aliases)))
            found.extend(_return_slot_conditions(stmt.body, dict(current_aliases)))
        elif isinstance(stmt, LoopStmt):
            found.extend(_return_slot_conditions(stmt.body, dict(current_aliases)))
        elif isinstance(stmt, SwitchStmt):
            found.extend(_return_slot_conditions(stmt.body, dict(current_aliases)))
            for case in stmt.cases:
                found.extend(_return_slot_conditions(case.body, dict(current_aliases)))
            found.extend(_return_slot_conditions(stmt.default_body, dict(current_aliases)))
    return found


def _record_slot_alias(stmt: Stmt, aliases: AliasMap) -> None:
    if isinstance(stmt, DeclarationStmt) and isinstance(stmt.init, AddressOf) and isinstance(stmt.init.operand, ArraySubscript):
        aliases[stmt.name] = ArraySubscript(stmt.init.operand.base, IntLiteral(0), stmt.init.operand.c_type)
        return
    record_alias(stmt, aliases)


def _body_returns_slot(statements: list[Stmt], aliases: AliasMap) -> bool:
    for stmt in statements:
        if isinstance(stmt, ReturnStmt) and stmt.value is not None:
            value = resolve_aliases(stmt.value, aliases)
            return isinstance(value, ArraySubscript) or (
                isinstance(value, AddressOf) and isinstance(value.operand, ArraySubscript)
            )
    return False


def _replace_param_refs(expr: Expr, arg_by_param: dict[str, Expr]) -> Expr:
    if isinstance(expr, VarRef) and expr.name in arg_by_param:
        return arg_by_param[expr.name]
    if isinstance(expr, FieldAccess):
        return FieldAccess(_replace_param_refs(expr.base, arg_by_param), expr.field, expr.c_type)
    if isinstance(expr, ArraySubscript):
        return ArraySubscript(_replace_param_refs(expr.base, arg_by_param), _replace_param_refs(expr.index, arg_by_param), expr.c_type)
    if isinstance(expr, AddressOf):
        return AddressOf(_replace_param_refs(expr.operand, arg_by_param), expr.c_type)
    if isinstance(expr, UnaryOp):
        return UnaryOp(expr.op, _replace_param_refs(expr.operand, arg_by_param), expr.c_type)
    if isinstance(expr, BinaryOp):
        return BinaryOp(expr.op, _replace_param_refs(expr.left, arg_by_param), _replace_param_refs(expr.right, arg_by_param), expr.c_type)
    return expr


def _dedup_alternatives(alts: list[LookupHitAlternative]) -> list[LookupHitAlternative]:
    out: list[LookupHitAlternative] = []
    seen: set[tuple[tuple[str, ...], tuple[BranchFact, ...]]] = set()
    for alt in alts:
        key = (tuple(alt.setup), tuple(alt.branch_facts))
        if key in seen:
            continue
        seen.add(key)
        out.append(alt)
    return out


def _nonmatching_value(value: str) -> str:
    if re.fullmatch(r"0|0x0+", value):
        return "1"
    return "0"
