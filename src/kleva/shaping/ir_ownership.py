from __future__ import annotations

from dataclasses import dataclass, field

from ..ir.model import AssignmentStmt, ArraySubscript, CallExpr, ExprStmt, FieldAccess, FunctionIR, ReturnStmt, VarRef
from ..ir.walk import walk_statements
from .ir_buffers import len_data_buffer_params_from_ir
from .ir_nullability import accepts_null_param_from_ir
from .ir_void_casts import void_param_cast_types_from_ir


BORROWED = "borrowed"
CONSUMED = "consumed"
TRANSFERRED = "transferred"


@dataclass(frozen=True)
class OwnershipSummary:
    param_behavior:       dict[str, str]
    returns_owned_pointer: bool
    nullable_params:       set[str] = field(default_factory=set)
    buffer_params:         set[str] = field(default_factory=set)
    void_cast_types:       dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OwnershipFact:
    param:  str
    action: str
    target: str


def consumed_params_from_ir(
    func: FunctionIR,
    param_names: set[str],
    consume_callees: set[str] | None = None,
) -> set[str]:
    """
    Return parameter names passed directly to known consuming callees.

    The default recognizes C's standard `free` and common destructor naming
    shapes. This does not encode a project domain; callers can provide an
    explicit callee set when they know additional ownership APIs.
    """
    consumed: set[str] = set()
    explicit_callees = consume_callees or set()

    for stmt in walk_statements(func):
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.expr, CallExpr):
            continue
        call = stmt.expr
        if not _is_consuming_callee(call.callee, explicit_callees):
            continue
        for arg in call.args:
            if isinstance(arg, VarRef) and arg.name in param_names:
                consumed.add(arg.name)

    return consumed


def ownership_facts_from_ir(
    func: FunctionIR,
    param_names: set[str],
    consume_callees: set[str] | None = None,
) -> list[OwnershipFact]:
    facts: list[OwnershipFact] = []
    seen: set[OwnershipFact] = set()
    explicit_callees = consume_callees or set()

    def add(fact: OwnershipFact) -> None:
        if fact in seen:
            return
        seen.add(fact)
        facts.append(fact)

    for stmt in walk_statements(func):
        if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr):
            call = stmt.expr
            if not _is_consuming_callee(call.callee, explicit_callees):
                continue
            for arg in call.args:
                if isinstance(arg, VarRef) and arg.name in param_names:
                    add(OwnershipFact(arg.name, CONSUMED, call.callee))
            continue

        if isinstance(stmt, AssignmentStmt):
            if not isinstance(stmt.target, (ArraySubscript, FieldAccess)):
                continue
            if isinstance(stmt.value, VarRef) and stmt.value.name in param_names:
                add(OwnershipFact(stmt.value.name, TRANSFERRED, _target_text(stmt.target)))

    return facts


def transferred_params_from_ir(func: FunctionIR, param_names: set[str]) -> set[str]:
    """
    Return parameters stored into an owner field.

    This recognizes a generic ownership-transfer shape:

    `owner->field = param;`

    The target object and field names are not interpreted. Storing a parameter
    into another object means generated tests should treat that parameter as no
    longer independently owned by the caller fixture.
    """
    transferred: set[str] = set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, AssignmentStmt):
            continue
        if not isinstance(stmt.target, (ArraySubscript, FieldAccess)):
            continue
        if isinstance(stmt.value, VarRef) and stmt.value.name in param_names:
            transferred.add(stmt.value.name)
    return transferred


def classify_ownership_from_ir(
    func: FunctionIR,
    param_names: set[str],
    consume_callees: set[str] | None = None,
    allocation_callees: set[str] | None = None,
    void_param_names: set[str] | None = None,
) -> OwnershipSummary:
    facts = ownership_facts_from_ir(func, param_names, consume_callees)
    consumed = {fact.param for fact in facts if fact.action == CONSUMED}
    transferred = {fact.param for fact in facts if fact.action == TRANSFERRED}
    behavior = {name: BORROWED for name in param_names}

    for name in transferred:
        behavior[name] = TRANSFERRED
    for name in consumed:
        behavior[name] = CONSUMED

    return OwnershipSummary(
        param_behavior=behavior,
        returns_owned_pointer=returns_owned_pointer_from_ir(func, allocation_callees),
        nullable_params={
            name for name in param_names
            if accepts_null_param_from_ir(func, name)
        },
        buffer_params=len_data_buffer_params_from_ir(func, param_names),
        void_cast_types=void_param_cast_types_from_ir(func, void_param_names or set()),
    )


def returns_owned_pointer_from_ir(
    func: FunctionIR,
    allocation_callees: set[str] | None = None,
) -> bool:
    explicit_callees = allocation_callees or set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, ReturnStmt) or not isinstance(stmt.value, CallExpr):
            continue
        if _is_allocating_callee(stmt.value.callee, explicit_callees):
            return True
    return False


def _is_consuming_callee(callee: str, explicit_callees: set[str]) -> bool:
    if callee in explicit_callees:
        return True
    return callee == "free" or callee.endswith(("_free", "_destroy", "_delete"))


def _is_allocating_callee(callee: str, explicit_callees: set[str]) -> bool:
    if callee in explicit_callees:
        return True
    return callee in {"malloc", "calloc", "realloc"} or callee.endswith(("_create", "_new", "_alloc"))


def _target_text(expr) -> str:
    if isinstance(expr, FieldAccess):
        return f"{_target_text(expr.base)}->{expr.field}"
    if isinstance(expr, ArraySubscript):
        return f"{_target_text(expr.base)}[]"
    if isinstance(expr, VarRef):
        return expr.name
    return "<unknown>"
