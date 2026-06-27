from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CFunction, CFunctionPointerTypedef, CTypeCatalog
from ..ir.model import BinaryOp, CallExpr, Expr, ExprStmt, FieldAccess, FunctionIR, IfStmt, SourceLocation, UnaryOp, VarRef
from ..ir.walk import walk_statements
from .candidates import BranchCandidate, BranchFact, ObjectPathFact, display_source_location


@dataclass(frozen=True)
class CallbackCall:
    target_expr: str
    loc:         SourceLocation | None = None


@dataclass(frozen=True)
class FieldCallback:
    root:        str
    fields:      tuple[str, ...]
    fp_decl:     CFunctionPointerTypedef
    root_type:   str | None = None
    value_type:  str | None = None

    @property
    def expr(self) -> str:
        return "->".join((self.root, *self.fields))

    @property
    def name_suffix(self) -> str:
        return "_".join((self.root, *self.fields))


def callback_calls_from_ir(func: FunctionIR) -> list[CallbackCall]:
    calls: list[CallbackCall] = []
    seen: set[str] = set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.expr, CallExpr):
            continue
        target = _callback_target(stmt.expr)
        if not target or target in seen:
            continue
        seen.add(target)
        calls.append(CallbackCall(target, stmt.loc))
    return calls


def _callback_target(call: CallExpr) -> str | None:
    if not isinstance(call.callee, str):
        return None
    if "->" in call.callee or "." in call.callee or "[" in call.callee:
        return call.callee
    return None


def callback_field_exprs_from_ir(func: FunctionIR) -> list[CallbackCall]:
    exprs: list[CallbackCall] = []
    seen: set[str] = set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.expr, CallExpr):
            continue
        callee = stmt.expr.callee
        if not callee or "->" not in callee:
            continue
        if callee in seen:
            continue
        seen.add(callee)
        exprs.append(CallbackCall(callee, stmt.loc))
    return exprs


def callback_guard_exprs_from_ir(func: FunctionIR) -> list[CallbackCall]:
    exprs: list[CallbackCall] = []
    seen: set[str] = set()
    for stmt in walk_statements(func):
        if not isinstance(stmt, IfStmt):
            continue
        for target in _guard_targets(stmt.condition):
            if target in seen:
                continue
            seen.add(target)
            exprs.append(CallbackCall(target, stmt.loc))
    return exprs


def callback_candidates_from_ir(
    func_ir: FunctionIR,
    func: CFunction,
    type_catalog: CTypeCatalog | None,
    function_pointer_stub_preamble: Callable[..., list[str]],
    function_pointer_stub_name: Callable[[str], str],
) -> list[BranchCandidate]:
    if not type_catalog:
        return []

    params = {p.name: p for p in func.params}
    candidates: list[BranchCandidate] = []
    for call in _direct_parameter_callback_calls(func_ir, params, type_catalog):
        param = params[call.target_expr]
        fp_decl = type_catalog.function_pointer(param.base_type)
        if not fp_decl:
            continue
        source_location = display_source_location(call.loc, f"ir:{func_ir.name}:callback:{param.name}")
        candidates.append(BranchCandidate(
            f"ir_callback_{param.name}_null",
            [],
            source_location=source_location,
            target_branch=f"callback {param.name} null",
            origin="ir",
            call_arg_overrides={param.name: "NULL"},
            branch_facts=[BranchFact(param.name, "==", "NULL")],
        ))
        candidates.append(BranchCandidate(
            f"ir_callback_{param.name}_present",
            [],
            _witness_stub_preamble(fp_decl),
            source_location=source_location,
            target_branch=f"callback {param.name} present",
            origin="ir",
            call_arg_overrides={param.name: function_pointer_stub_name(fp_decl.name)},
            witness_setup=[f"int out_{param.name}_called = kleva_stub_{fp_decl.name}_called;"],
            extra_outputs=[f"out_{param.name}_called"],
            branch_facts=[BranchFact(param.name, "!=", "NULL")],
        ))

    seen_field_targets: set[str] = set()
    for call in [*callback_field_exprs_from_ir(func_ir), *callback_guard_exprs_from_ir(func_ir)]:
        if call.target_expr in seen_field_targets:
            continue
        seen_field_targets.add(call.target_expr)
        _append_field_callback_candidates(
            candidates,
            call,
            func_ir,
            params,
            type_catalog,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )
    return candidates


def _append_field_callback_candidates(
    candidates: list[BranchCandidate],
    call: CallbackCall,
    func_ir: FunctionIR,
    params: dict[str, object],
    type_catalog: CTypeCatalog,
    function_pointer_stub_preamble: Callable[..., list[str]],
    function_pointer_stub_name: Callable[[str], str],
) -> None:
    callback = _field_callback(call.target_expr, params, type_catalog)
    if not callback:
        return

    object_paths = [
        ObjectPathFact(callback.root, callback.fields, callback.root_type, callback.value_type)
    ]
    source_location = display_source_location(call.loc, f"ir:{func_ir.name}:callback:{callback.name_suffix}")
    candidates.append(BranchCandidate(
        f"ir_callback_{callback.name_suffix}_null",
        [f"{callback.expr} = NULL;"],
        source_location=source_location,
        target_branch=f"callback {callback.expr} null",
        origin="ir",
        object_paths=object_paths,
        branch_facts=[BranchFact(callback.expr, "==", "NULL")],
    ))
    candidates.append(BranchCandidate(
        f"ir_callback_{callback.name_suffix}_present",
        [f"{callback.expr} = {function_pointer_stub_name(callback.fp_decl.name)};"],
        _witness_stub_preamble(callback.fp_decl),
        source_location=source_location,
        target_branch=f"callback {callback.expr} present",
        origin="ir",
        object_paths=object_paths,
        witness_setup=[f"int out_{callback.name_suffix}_called = kleva_stub_{callback.fp_decl.name}_called;"],
        extra_outputs=[f"out_{callback.name_suffix}_called"],
        branch_facts=[BranchFact(callback.expr, "!=", "NULL")],
    ))


def _witness_stub_preamble(decl: CFunctionPointerTypedef) -> list[str]:
    lines = [f"static int kleva_stub_{decl.name}_called;"]
    for line in _witness_function_pointer_stub_preamble(decl):
        lines.append(line)
    return lines


def _witness_function_pointer_stub_preamble(decl: CFunctionPointerTypedef) -> list[str]:
    params: list[str] = []
    for i, p in enumerate(decl.params):
        name = p.name or f"arg{i}"
        raw_type = p.raw_type.strip()
        if p.is_array:
            raw_type = raw_type.replace("[]", f"*{name}")
        elif not re.search(rf"\b{re.escape(name)}\b", raw_type):
            raw_type = f"{raw_type} {name}"
        params.append(raw_type)

    params_s = ", ".join(params) if params else "void"
    lines = [f"static {decl.return_type} kleva_stub_{decl.name}({params_s}) {{"]
    lines.append(f"    kleva_stub_{decl.name}_called++;")
    for p in decl.params:
        lines.append(f"    (void){p.name};")
    if decl.return_type.strip() != "void":
        lines.append("    return 0;")
    lines.append("}")
    return lines


def _direct_parameter_callback_calls(
    func_ir: FunctionIR,
    params: dict[str, object],
    type_catalog: CTypeCatalog,
) -> list[CallbackCall]:
    calls: list[CallbackCall] = []
    seen: set[str] = set()
    for stmt in walk_statements(func_ir):
        if not isinstance(stmt, ExprStmt) or not isinstance(stmt.expr, CallExpr):
            continue
        callee = stmt.expr.callee
        param = params.get(callee)
        if not param or callee in seen:
            continue
        if not type_catalog.function_pointer(param.base_type):
            continue
        seen.add(callee)
        calls.append(CallbackCall(callee, stmt.loc))
    return calls


def _field_callback(
    expr: str,
    params: dict[str, object],
    type_catalog: CTypeCatalog,
) -> FieldCallback | None:
    parts = expr.split("->")
    if len(parts) < 2 or any(not part for part in parts):
        return None
    root, fields = parts[0], tuple(parts[1:])
    if any("[" in field or "." in field for field in fields):
        return None
    param = params.get(root)
    if not param:
        return None

    current_type = param.base_type
    field_param = None
    for field in fields:
        field_param = type_catalog.field_type(current_type, field)
        if not field_param:
            return None
        current_type = field_param.base_type

    if not field_param:
        return None
    fp_decl = type_catalog.function_pointer(field_param.base_type)
    if not fp_decl:
        return None

    root_type = f"{param.base_type} *" if getattr(param, "is_pointer", False) else param.base_type
    return FieldCallback(root, fields, fp_decl, root_type, field_param.base_type)


def _guard_targets(expr: Expr) -> list[str]:
    if isinstance(expr, BinaryOp) and expr.op in {"&&", "||"}:
        return [*_guard_targets(expr.left), *_guard_targets(expr.right)]
    if isinstance(expr, UnaryOp) and expr.op == "!":
        return _guard_targets(expr.operand)
    if isinstance(expr, FieldAccess):
        target = _expr_text(expr)
        return [target] if target and "->" in target else []
    return []


def _expr_text(expr: Expr) -> str | None:
    if isinstance(expr, FieldAccess):
        base = _expr_text(expr.base)
        if base:
            return f"{base}->{expr.field}"
        return None
    if isinstance(expr, VarRef):
        return expr.name
    name = getattr(expr, "name", None)
    if isinstance(name, str) and name:
        return name
    return None
