from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, List, Tuple, TypedDict

from .acsl import ACSLBehavior
from .ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from .ir.aliases import AliasMap, record_alias, resolve_aliases
from .ir.model import AssignmentStmt, DeclarationStmt, FunctionIR, IfStmt, LoopStmt, SwitchStmt
from .ir.render import value_expr
from .fixtures.requirements import (
    FixtureRequirement,
    FixtureRequirementKind,
    fixture_failure_comments,
    requirements_from_assumptions,
    requirements_for_target,
    requirements_for_valid_params,
    usable_requirements,
)
from .shaping.candidates import BranchFact, ObjectPathFact, PostStateFact
from .shaping.ir_ownership import CONSUMED, TRANSFERRED, OwnershipSummary

LOCAL_BUFFER_SIZE = 256


class GeneratedInput(TypedDict, total=False):
    ktest_name: str
    c_type: str
    c_var: str
    bounds: tuple[int, int]


@dataclass
class BodyResult:
    body: list[str]
    outputs: list[str]
    cleanup: list[str]
    preamble: list[str]
    inputs: list[GeneratedInput]

    def __iter__(self) -> Iterator[list[str]]:
        yield self.body
        yield self.outputs
        yield self.cleanup
        yield self.preamble


def _param_consumed(ownership: OwnershipSummary | None, name: str) -> bool:
    return bool(ownership and ownership.param_behavior.get(name) == CONSUMED)


def _param_transferred(ownership: OwnershipSummary | None, name: str) -> bool:
    return bool(ownership and ownership.param_behavior.get(name) == TRANSFERRED)


def _param_frees(
    ownership: OwnershipSummary | None,
    name: str,
    source_text: str | None,
    func_name: str,
    ops,
) -> bool:
    if ownership is not None:
        return _param_consumed(ownership, name)
    return ops.function_frees_param(source_text, func_name, name)


def _param_takes_ownership(
    ownership: OwnershipSummary | None,
    name: str,
    source_text: str | None,
    func_name: str,
    ops,
) -> bool:
    if ownership is not None:
        return _param_transferred(ownership, name)
    return ops.function_takes_param_ownership(source_text, func_name, name)


def _param_accepts_null(
    ownership: OwnershipSummary | None,
    name: str,
    source_text: str | None,
    func_name: str,
    ops,
) -> bool:
    if ownership is not None:
        return name in ownership.nullable_params
    return ops.function_accepts_null_param(source_text, func_name, name)


def _returns_owned_pointer(
    ownership: OwnershipSummary | None,
    func: CFunction,
    ops,
) -> bool:
    if ownership is not None:
        return ownership.returns_owned_pointer
    return ops.function_returns_owned_pointer(func)


def _param_needs_len_data_shape(
    ownership: OwnershipSummary | None,
    func_name: str,
    param: CParam,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops,
) -> bool:
    if ownership is not None:
        return param.name in ownership.buffer_params
    return ops.needs_len_data_shape(func_name, param.name, source_text, type_catalog, param)


def _void_param_cast_types(
    ownership: OwnershipSummary | None,
    body: str,
    func: CFunction,
    ops,
) -> dict[str, str]:
    if ownership is not None:
        return ownership.void_cast_types
    return ops.void_param_cast_types(body, func)


@dataclass(frozen=True)
class BodyGenOps:
    scalar_bounds: dict[str, tuple[int, int]]
    default_shaping_features: frozenset[str]
    scalar_values_from_assumptions: Callable[[list[str]], dict[str, str]]
    extract_result_value: Callable[[list[str]], int | None]
    extract_non_null_params: Callable[[list[str]], list[str]]
    extract_nonzero_params: Callable[[list[str]], list[str]]
    extract_null_params: Callable[[list[str]], list[str]]
    extract_valid_params: Callable[[list[str]], list[str]]
    is_void_star: Callable[[CParam], bool]
    pointer_argument_setup: Callable[..., tuple[list[str], str, list[str]]]
    needs_len_data_shape: Callable[..., bool]
    append_len_data_shape: Callable[[list[str], str], None]
    param_ref_from_arg: Callable[[str], tuple[str, str] | None]
    function_frees_param: Callable[[str | None, str, str], bool]
    function_takes_param_ownership: Callable[[str | None, str, str], bool]
    function_accepts_null_param: Callable[[str | None, str, str], bool]
    function_returns_owned_pointer: Callable[[CFunction], bool]
    lookup_free_fn: Callable[[str, str | None, dict[str, CFunction] | None], str | None]
    assumption_setup_lines: Callable[..., list[str]]
    source_for_branch_shaping: Callable[[str | None, str], str]
    void_param_cast_types: Callable[[str, CFunction], dict[str, str]]
    unique_name: Callable[[str, set[str]], str]
    function_pointer_stub_preamble: Callable[..., list[str]]
    function_pointer_stub_name: Callable[[str], str]
    rewrite_setup_with_param_args: Callable[[list[str], dict[str, str]], list[str]]
    safe_c_name: Callable[[str], str]


def param_ref_from_arg(arg: str) -> tuple[str, str] | None:
    m = re.fullmatch(r"&([A-Za-z_]\w*)", arg)
    if m:
        return (m.group(1), ".")
    if arg != "NULL" and re.fullmatch(r"[A-Za-z_]\w*", arg):
        return (arg, "->")
    return None


def _function_pointer_param_setup(
    p: CParam,
    fp_decl: CFunctionPointerTypedef,
    lines: list[str],
    preamble: list[str],
    ops: BodyGenOps,
) -> str:
    for line in ops.function_pointer_stub_preamble(fp_decl):
        if line not in preamble:
            preamble.append(line)
    stub_name = ops.function_pointer_stub_name(fp_decl.name)
    lines.append(f"{p.base_type} {p.name} = {stub_name};")
    return p.name


def append_return_field_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    out_var: str,
    param_args: dict[str, str],
    type_catalog: CTypeCatalog | None,
    scalar_bounds: dict[str, tuple[int, int]],
) -> None:
    if not type_catalog or not type_catalog.is_complete_struct(func.return_base):
        return

    for field_name, field_param in type_catalog.struct_fields.get(func.return_base, {}).items():
        if field_name not in param_args:
            continue
        arg = param_args[field_name]
        out_name = f"out_{field_name}"
        if field_param.is_array:
            continue
        if field_param.is_pointer or type_catalog.function_pointer(field_param.base_type):
            lines.append(f"int {out_name}_same = ({out_var} != NULL && {out_var}->{field_name} == {arg});")
            outputs.append(f"{out_name}_same")
        elif field_param.base_type in scalar_bounds or field_param.base_type in ("EventType", "uint64_t", "size_t"):
            if field_param.base_type in scalar_bounds or field_param.base_type in ("uint64_t", "size_t"):
                lines.append(f"{field_param.base_type} {out_name} = {out_var} ? {out_var}->{field_name} : 0;")
                outputs.append(out_name)
            else:
                lines.append(f"int {out_name}_same = ({out_var} != NULL && {out_var}->{field_name} == {arg});")
                outputs.append(f"{out_name}_same")


def is_observable_scalar_type(
    type_name: str,
    type_catalog: CTypeCatalog | None,
    scalar_bounds: dict[str, tuple[int, int]],
) -> bool:
    if type_name in scalar_bounds or type_name in ("uint64_t", "size_t", "EventType"):
        return True
    if type_catalog and type_catalog.is_complete_struct(type_name):
        return False
    if type_catalog and type_catalog.function_pointer(type_name):
        return False
    return bool(re.fullmatch(r"[A-Za-z_]\w*", type_name))


def field_expr(base_expr: str, access: str, field_name: str) -> str:
    return f"{base_expr}{access}{field_name}"


def append_source_witness_outputs(
    lines: list[str],
    outputs: list[str],
    func: CFunction,
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    ops: BodyGenOps,
    ownership: OwnershipSummary | None = None,
) -> None:
    """
    Add generic post-call witnesses for mutable pointer parameters.

    These are intentionally structural rather than domain-specific. For a
    source-shaped candidate, fields on complete structs are often the clearest
    evidence that the intended side effect happened. KLEVA records shallow
    scalar/pointer fields, plus scalar/pointer fields one nested struct deep.
    """
    if not type_catalog:
        return

    seen: set[str] = set(outputs)
    for p in func.params:
        if not p.is_pointer or p.is_const or ops.is_void_star(p):
            continue
        if p.name not in active_params or p.name not in param_refs:
            continue
        if _param_frees(ownership, p.name, source_text, func.name, ops):
            continue
        if not type_catalog.is_complete_struct(p.base_type):
            continue

        base_expr, access = param_refs[p.name]
        for field_name, field_param in type_catalog.struct_fields.get(p.base_type, {}).items():
            out_name = ops.safe_c_name(f"out_{p.name}_{field_name}")
            expr = field_expr(base_expr, access, field_name)

            if field_param.is_array or "[" in field_param.raw_type:
                continue

            if field_param.is_pointer or type_catalog.function_pointer(field_param.base_type):
                if out_name not in seen:
                    lines.append(f"int {out_name}_nonnull = ({expr} != NULL);")
                    outputs.append(f"{out_name}_nonnull")
                    seen.add(out_name)
                continue

            if is_observable_scalar_type(field_param.base_type, type_catalog, ops.scalar_bounds):
                if out_name not in seen:
                    c_type = field_param.base_type if field_param.base_type in ops.scalar_bounds else "int"
                    lines.append(f"{c_type} {out_name} = {expr};")
                    outputs.append(out_name)
                    seen.add(out_name)
                continue

            if not type_catalog.is_complete_struct(field_param.base_type):
                continue

            nested_access = f"{access}{field_name}."
            for nested_name, nested_param in type_catalog.struct_fields.get(field_param.base_type, {}).items():
                nested_out = ops.safe_c_name(f"out_{p.name}_{field_name}_{nested_name}")
                nested_expr = field_expr(base_expr, nested_access, nested_name)
                if nested_param.is_array or "[" in nested_param.raw_type:
                    continue
                if nested_param.is_pointer or type_catalog.function_pointer(nested_param.base_type):
                    if nested_out not in seen:
                        lines.append(f"int {nested_out}_nonnull = ({nested_expr} != NULL);")
                        outputs.append(f"{nested_out}_nonnull")
                        seen.add(nested_out)
                    continue
                if is_observable_scalar_type(nested_param.base_type, type_catalog, ops.scalar_bounds) and nested_out not in seen:
                    c_type = nested_param.base_type if nested_param.base_type in ops.scalar_bounds else "int"
                    lines.append(f"{c_type} {nested_out} = {nested_expr};")
                    outputs.append(nested_out)
                    seen.add(nested_out)


@dataclass(frozen=True)
class PostconditionWitness:
    before_lines: list[str]
    after_lines: list[str]
    outputs: list[str]


def acsl_postcondition_witnesses(
    ensures: list[str],
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
    active_params: dict[str, CParam],
    owned_or_freed_params: set[str],
    null_roots: set[str],
    ops: BodyGenOps,
) -> PostconditionWitness:
    """
    Build generic post-state witnesses from ACSL old-state postconditions.

    KLEVA snapshots the old expression before the call and emits a compact
    boolean output after the call. EVA can then promote the side effect into a
    regression assertion.
    """
    before: list[str] = []
    after: list[str] = []
    outputs: list[str] = []
    seen_old: dict[str, str] = {}
    seen_out: set[str] = set()

    for expr in ensures:
        for part in re.split(r"\s*&&\s*", expr):
            parsed = _parse_old_postcondition(part)
            if parsed is None:
                continue
            lhs, old_expr, op, rhs = parsed
            lhs_root = _expr_root(lhs)
            old_root = _expr_root(old_expr)
            if lhs_root in owned_or_freed_params or old_root in owned_or_freed_params:
                continue
            if rhs and _expr_mentions_any_root(rhs, owned_or_freed_params):
                continue
            if lhs_root in null_roots or old_root in null_roots:
                continue
            if lhs_root not in active_params or old_root not in active_params:
                continue

            lhs_c = _rewrite_observable_expr(lhs, param_refs, param_args)
            old_c = _rewrite_observable_expr(old_expr, param_refs, param_args)
            rhs_c = _rewrite_observable_expr(rhs, param_refs, param_args) if rhs else None
            if lhs_c is None or old_c is None or (rhs and rhs_c is None):
                continue

            old_name = seen_old.get(old_c)
            if old_name is None:
                old_name = _witness_c_name(ops, f"kleva_old_{old_expr}")
                before.append(f"uintptr_t {old_name} = (uintptr_t)({old_c});")
                seen_old[old_c] = old_name

            out_name = _witness_c_name(ops, f"out_post_{lhs}")
            if op:
                out_name = _witness_c_name(ops, f"{out_name}_{op}_{rhs}")
                expected = f"({old_name} {op} (uintptr_t)({rhs_c}))"
            else:
                expected = old_name
            if out_name in seen_out:
                continue
            seen_out.add(out_name)
            after.append(f"int {out_name} = ((uintptr_t)({lhs_c}) == {expected});")
            outputs.append(out_name)

    return PostconditionWitness(before, after, outputs)


def ir_direct_assignment_witnesses(
    function_ir: FunctionIR | None,
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
    active_params: dict[str, CParam],
    owned_or_freed_params: set[str],
    null_roots: set[str],
    ops: BodyGenOps,
) -> PostconditionWitness:
    """
    Build conservative post-call witnesses from straight-line IR assignments.

    This intentionally ignores assignments inside branches, loops, and switches.
    Those require path-sensitive proof before they can become trusted oracles.
    """
    if function_ir is None:
        return PostconditionWitness([], [], [])

    before: list[str] = []
    after: list[str] = []
    outputs: list[str] = []
    seen: set[str] = set()
    seen_old: dict[str, str] = {}
    aliases: AliasMap = {}
    local_names: set[str] = set()

    for stmt in function_ir.statements:
        if isinstance(stmt, DeclarationStmt):
            local_names.add(stmt.name)
            record_alias(stmt, aliases)
            continue
        if isinstance(stmt, (IfStmt, LoopStmt, SwitchStmt)):
            continue
        if not isinstance(stmt, AssignmentStmt):
            continue
        record_alias(stmt, aliases)

        target = value_expr(resolve_aliases(stmt.target, aliases))
        value = value_expr(resolve_aliases(stmt.value, aliases))
        if target is None or value is None:
            continue
        if _expr_mentions_local_root(target, local_names) or _expr_mentions_local_root(value, local_names):
            continue
        root = _expr_root(target)
        if root not in active_params or root in owned_or_freed_params or root in null_roots:
            continue
        if _expr_mentions_any_root(value, owned_or_freed_params):
            continue

        target_c = _rewrite_observable_expr(target, param_refs, param_args)
        value_c = _rewrite_observable_expr(value, param_refs, param_args)
        if target_c is None or value_c is None:
            continue
        allowed_names = _observable_allowed_names(active_params, param_refs, param_args)
        if _expr_mentions_unknown_local(target_c, allowed_names) or _expr_mentions_unknown_local(value_c, allowed_names):
            continue

        out_name = _witness_c_name(ops, f"out_ir_post_{target}")
        if out_name in seen:
            continue
        seen.add(out_name)
        expected_c = value_c
        if target_c in value_c:
            old_name = seen_old.get(target_c)
            if old_name is None:
                old_name = _witness_c_name(ops, f"kleva_old_ir_{target}")
                before.append(f"uintptr_t {old_name} = (uintptr_t)({target_c});")
                seen_old[target_c] = old_name
            expected_c = value_c.replace(target_c, old_name)
        after.append(f"int {out_name} = ((uintptr_t)({target_c}) == (uintptr_t)({expected_c}));")
        outputs.append(out_name)

    return PostconditionWitness(before, after, outputs)


def _expr_mentions_local_root(expr: str, local_names: set[str]) -> bool:
    if not local_names:
        return False
    root_match = re.match(r"\s*\(*\s*([A-Za-z_]\w*)\b", expr)
    if root_match and root_match.group(1) in local_names:
        return True
    for name in local_names:
        escaped = re.escape(name)
        if re.search(rf"(?<![A-Za-z0-9_]){escaped}\s*(?:->|\.|\[|\))", expr):
            return True
    return False


def _observable_allowed_names(
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
) -> set[str]:
    names = set(active_params)
    names.update(param_refs)
    names.update(param_args)
    for base, _access in param_refs.values():
        root = _expr_root(base)
        if root:
            names.add(root)
    for arg in param_args.values():
        root = _expr_root(arg)
        if root and root != "NULL":
            names.add(root)
    return names


def _expr_mentions_unknown_local(expr: str, allowed_names: set[str]) -> bool:
    c_names = {
        "NULL",
        "false",
        "true",
        "sizeof",
        "uintptr_t",
        "intptr_t",
        "size_t",
        "ssize_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "char",
        "const",
        "int",
        "long",
        "short",
        "signed",
        "unsigned",
        "void",
    }
    for match in re.finditer(r"\b[A-Za-z_]\w*\b", expr):
        name = match.group(0)
        start = match.start()
        if name in allowed_names or name in c_names or name.isupper():
            continue
        if start >= 1 and expr[start - 1] == ".":
            continue
        if start >= 2 and expr[start - 2:start] == "->":
            continue
        return True
    return False


def post_state_fact_witnesses(
    facts: list[PostStateFact] | None,
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
    active_params: dict[str, CParam],
    owned_or_freed_params: set[str],
    null_roots: set[str],
    ops: BodyGenOps,
) -> PostconditionWitness:
    if not facts:
        return PostconditionWitness([], [], [])

    before: list[str] = []
    after: list[str] = []
    outputs: list[str] = []
    seen: set[str] = set()
    seen_old: dict[str, str] = {}
    for fact in facts:
        root = _expr_root(fact.target)
        if root not in active_params or root in owned_or_freed_params or root in null_roots:
            continue
        if _expr_mentions_any_root(fact.value, owned_or_freed_params):
            continue
        target_c = _rewrite_observable_expr(fact.target, param_refs, param_args)
        value_c = _rewrite_observable_expr(fact.value, param_refs, param_args)
        if target_c is None or value_c is None:
            continue
        allowed_names = _observable_allowed_names(active_params, param_refs, param_args)
        if _expr_mentions_unknown_local(target_c, allowed_names) or _expr_mentions_unknown_local(value_c, allowed_names):
            continue
        if fact.relation not in {"==", "!="}:
            continue
        out_name = _witness_c_name(ops, f"out_post_state_{fact.target}_{fact.relation}_{fact.value}")
        if out_name in seen:
            continue
        seen.add(out_name)
        expected_c = value_c
        if target_c in value_c:
            old_name = seen_old.get(target_c)
            if old_name is None:
                old_name = _witness_c_name(ops, f"kleva_old_post_state_{fact.target}")
                before.append(f"uintptr_t {old_name} = (uintptr_t)({target_c});")
                seen_old[target_c] = old_name
            expected_c = value_c.replace(target_c, old_name)
        after.append(f"int {out_name} = ((uintptr_t)({target_c}) {fact.relation} (uintptr_t)({expected_c}));")
        outputs.append(out_name)
    return PostconditionWitness(before, after, outputs)


def object_path_byte_buffer_setup_lines(
    requirements: list[FixtureRequirement] | None,
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
    ops: BodyGenOps,
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for req in requirements or []:
        if req.kind != FixtureRequirementKind.OBJECT_PATH_BYTE_BUFFER:
            continue
        target_c = _rewrite_observable_expr(req.target, param_refs, param_args)
        size_c = _rewrite_observable_expr(req.size, param_refs, param_args)
        if target_c is None or size_c is None:
            continue
        buffer_size = _safe_local_buffer_size(req.size)
        stem = _witness_c_name(ops, f"{req.target}_buffer")
        candidates = [
            f"uint8_t {stem}[{buffer_size}];",
            *_byte_buffer_content_lines(stem, f"sizeof({stem})", req.content),
            f"if ({target_c} == NULL) {target_c} = {stem};",
            *_byte_buffer_content_lines(target_c, size_c, req.content),
        ]
        for line in candidates:
            if line not in seen:
                lines.append(line)
                seen.add(line)
    return lines


def _byte_buffer_content_lines(buffer_expr: str, size_expr: str, content: str | None) -> list[str]:
    if content == "all-0xff":
        return [f"memset({buffer_expr}, 0xFF, {size_expr});"]
    if content == "first-byte-set":
        return [
            f"memset({buffer_expr}, 0, {size_expr});",
            f"if ({size_expr} > 0) {buffer_expr}[0] = 1;",
        ]
    return [f"memset({buffer_expr}, 0, {size_expr});"]


def object_path_value_setup_lines(
    requirements: list[FixtureRequirement] | None,
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for req in requirements or []:
        if req.kind != FixtureRequirementKind.OBJECT_PATH_VALUE:
            continue
        target_c = _rewrite_observable_expr(req.target, param_refs, param_args)
        value_c = _rewrite_observable_expr(req.value, param_refs, param_args)
        if target_c is None or value_c is None:
            line = f"/* fixture-failed: unsupported pointer relation: {req.target} {req.relation} {req.value} */"
        else:
            value = value_c if req.relation == "==" else _literal_for_fixture_relation(req.relation, value_c)
            line = f"{target_c} = {value};"
        if line not in seen:
            lines.append(line)
            seen.add(line)
    return lines


def object_path_facts_from_requirements(
    requirements: list[FixtureRequirement] | None,
    params_by_name: dict[str, CParam],
) -> list[ObjectPathFact]:
    facts: list[ObjectPathFact] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for req in requirements or []:
        if req.kind not in {
            FixtureRequirementKind.OBJECT_PATH_BYTE_BUFFER,
            FixtureRequirementKind.OBJECT_PATH_VALUE,
        }:
            continue
        parsed = _parse_object_path_target(req.target)
        if parsed is None:
            continue
        root, path = parsed
        param = params_by_name.get(root)
        if param is None:
            continue
        key = (root, path)
        if key in seen:
            continue
        seen.add(key)
        facts.append(ObjectPathFact(root, path, param.raw_type, ""))
    return facts


def _literal_for_fixture_relation(relation: str | None, value: str) -> str:
    if relation == ">":
        return f"(({value}) + 1)"
    if relation == "<":
        return f"(({value}) > 0 ? ({value}) - 1 : 0)"
    return value


def _parse_object_path_target(target: str | None) -> tuple[str, tuple[str, ...]] | None:
    if not target:
        return None
    parts = re.findall(r"(?:^|->|\.)([A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?", target)
    if len(parts) < 2:
        return None
    return parts[0], tuple(parts[1:])


def _assumes_without_typed_object_path_values(assumes: list[str]) -> list[str]:
    filtered: list[str] = []
    for expr in assumes:
        kept_parts = [
            part.strip()
            for part in re.split(r"\s*&&\s*", expr)
            if part.strip() and not _is_typed_object_path_value_part(part.strip())
        ]
        if kept_parts:
            filtered.append(" && ".join(kept_parts))
    return filtered


def _is_typed_object_path_value_part(part: str) -> bool:
    value_pattern = r"(?:[A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)"
    path_pattern = (
        r"[A-Za-z_]\w*"
        r"(?:->(?:[A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?"
        r"|\.(?:[A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?)+"
    )
    return bool(re.fullmatch(rf"{path_pattern}\s*(==|>=|>|<=|<)\s*{value_pattern}", part))


def _safe_local_buffer_size(size: str | None) -> str:
    if size and re.fullmatch(r"\d+|[A-Z][A-Z0-9_]*", size):
        return size
    return "64"


def _witness_c_name(ops: BodyGenOps, value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", ops.safe_c_name(value))
    return re.sub(r"_+", "_", name).strip("_")


def _append_missing_oracle(
    lines:   list[str],
    outputs: list[str],
    reason:  str,
) -> None:
    lines.append(f"/* oracle-missing: {reason} */")
    lines.append("int out_missing_oracle;")
    outputs.append("out_missing_oracle")


def _append_call_completed_witness(lines: list[str], outputs: list[str]) -> None:
    lines.append("int out_call_completed = 1;")
    outputs.append("out_call_completed")


def _has_pending_witness_outputs(
    *witnesses: PostconditionWitness,
    extra_outputs: list[str] | None = None,
) -> bool:
    return any(w.outputs for w in witnesses) or bool(extra_outputs)


def _parse_old_postcondition(expr: str) -> tuple[str, str, str | None, str | None] | None:
    part = expr.strip().rstrip(";")
    while part.startswith("(") and part.endswith(")"):
        inner = part[1:-1].strip()
        if not inner:
            break
        part = inner

    match = re.fullmatch(r"(.+?)\s*==\s*\\old\((.+?)\)\s*([+-])\s*(.+)", part)
    if match:
        lhs, old_expr, op, rhs = match.groups()
        return lhs.strip(), old_expr.strip(), op, rhs.strip()

    match = re.fullmatch(r"(.+?)\s*==\s*\\old\((.+?)\)", part)
    if match:
        lhs, old_expr = match.groups()
        return lhs.strip(), old_expr.strip(), None, None

    return None


def _expr_root(expr: str) -> str | None:
    match = re.match(r"\s*\(?\s*([A-Za-z_]\w*)", expr)
    return match.group(1) if match else None


def _expr_mentions_any_root(expr: str | None, roots: set[str]) -> bool:
    if not expr or not roots:
        return False
    for root in roots:
        escaped = re.escape(root)
        if re.search(rf"(?<![A-Za-z0-9_]){escaped}(?:\b|\s*(?:->|\.|\[))", expr):
            return True
    return False


def null_roots_from_branch_facts(facts: list[BranchFact] | None) -> set[str]:
    roots: set[str] = set()
    for fact in facts or []:
        if fact.relation not in {"==", "is"}:
            continue
        if fact.value not in {"0", "NULL", "\\null"}:
            continue
        root = _expr_root(fact.target)
        if root:
            roots.add(root)
    return roots


def _rewrite_observable_expr(
    expr: str | None,
    param_refs: dict[str, tuple[str, str]],
    param_args: dict[str, str],
) -> str | None:
    if expr is None:
        return None
    out = expr.strip()
    if not out:
        return None

    if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", out):
        return out
    if re.fullmatch(r"[A-Za-z_]\w*", out):
        return param_args.get(out, out)

    rewritten_roots: set[str] = set()
    for name in sorted(param_refs, key=len, reverse=True):
        base, access = param_refs[name]
        new_out = re.sub(
            rf"\b{re.escape(name)}->([A-Za-z_]\w*)",
            rf"{base}{access}\1",
            out,
        )
        if new_out != out:
            rewritten_roots.add(name)
            out = new_out
    for name, value in sorted(param_args.items(), key=lambda item: len(item[0]), reverse=True):
        if value == "NULL" or name in rewritten_roots:
            continue
        out = re.sub(rf"(?<!->)(?<!\.)\b{re.escape(name)}\b", value, out)

    if re.search(r"\\|==|!=|&&|\|\|", out):
        return None
    return out


def object_path_setup_lines(
    object_paths: list[ObjectPathFact],
    active_params: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]],
    type_catalog: CTypeCatalog | None,
    used_names: set[str],
    unique_name: Callable[[str, set[str]], str],
) -> list[str]:
    if not object_paths or not type_catalog:
        return []

    lines: list[str] = []
    seen_assignments: set[tuple[str, tuple[str, ...]]] = set()

    def append_heap_object(name: str, base_type: str) -> None:
        if "void *malloc(size_t size);" not in lines:
            lines.append("void *malloc(size_t size);")
        lines.append(f"{base_type} *{name} = malloc(sizeof(*{name}));")
        lines.append(f"if (!{name}) return 0;")
        lines.append(f"memset({name}, 0, sizeof(*{name}));")

    def append_pointer_array(field_ref: str, name: str, base_type: str) -> None:
        if "void *malloc(size_t size);" not in lines:
            lines.append("void *malloc(size_t size);")
        lines.append(f"{base_type} **{name} = malloc(sizeof(*{name}));")
        lines.append(f"if (!{name}) return 0;")
        lines.append(f"memset({name}, 0, sizeof(*{name}));")
        lines.append(f"{field_ref} = {name};")

    for fact in object_paths:
        if not fact.path or fact.root not in active_params or fact.root not in param_refs:
            continue

        current_type = active_params[fact.root].base_type
        current_expr, current_access = param_refs[fact.root]
        if len(fact.path) == 1:
            field_param = type_catalog.field_type(current_type, fact.path[0])
            if field_param and field_param.is_pointer and type_catalog.is_complete_struct(field_param.base_type):
                field_ref = f"{current_expr}{current_access}{fact.path[0]}"
                key = (fact.root, fact.path)
                if key not in seen_assignments:
                    seen_assignments.add(key)
                    obj_name = unique_name(f"{fact.root}_{fact.path[0]}_0", used_names)
                    if field_param.pointer_depth >= 2:
                        slots_name = unique_name(f"{fact.root}_{fact.path[0]}_slots", used_names)
                        append_pointer_array(field_ref, slots_name, field_param.base_type)
                    append_heap_object(obj_name, field_param.base_type)
                    if field_param.pointer_depth >= 2:
                        lines.append(f"{field_ref}[0] = {obj_name};")
                    else:
                        lines.append(f"{field_ref} = {obj_name};")
            continue

        walked: list[str] = []
        for field in fact.path[:-1]:
            field_param = type_catalog.field_type(current_type, field)
            if field_param is None:
                break

            walked.append(field)
            field_expr = f"{current_expr}{current_access}{field}"
            if field_param.is_pointer:
                if not type_catalog.is_complete_struct(field_param.base_type):
                    break
                key = (fact.root, tuple(walked))
                if key not in seen_assignments:
                    seen_assignments.add(key)
                    obj_name = unique_name(f"{fact.root}_{'_'.join(walked)}", used_names)
                    if field_param.pointer_depth >= 2:
                        slots_name = unique_name(f"{fact.root}_{'_'.join(walked)}_slots", used_names)
                        append_pointer_array(field_expr, slots_name, field_param.base_type)
                    append_heap_object(obj_name, field_param.base_type)
                    if field_param.pointer_depth >= 2:
                        lines.append(f"{field_expr}[0] = {obj_name};")
                    else:
                        lines.append(f"{field_expr} = {obj_name};")
                current_type = field_param.base_type
                current_expr = f"{field_expr}[0]" if field_param.pointer_depth >= 2 else field_expr
                current_access = "->"
                continue

            if not type_catalog.is_complete_struct(field_param.base_type):
                break
            current_type = field_param.base_type
            current_expr = field_expr
            current_access = "."
    return lines


def _object_path_backed(
    setup_lines: list[str],
    fact: ObjectPathFact,
    param_refs: dict[str, tuple[str, str]],
) -> bool:
    if not fact.path or fact.root not in param_refs:
        return False
    base_expr, access = param_refs[fact.root]
    prefix = f"{base_expr}{access}{fact.path[0]} ="
    if any(line.strip().startswith(prefix) for line in setup_lines):
        return True
    return False


def _pointer_var_initialized_by_call(setup_lines: list[str], var_name: str) -> bool:
    pattern = re.compile(
        rf"^\s*[A-Za-z_]\w*(?:\s+|\s*\*\s*)\*?\s*{re.escape(var_name)}\s*=\s*[A-Za-z_]\w*\s*\("
    )
    return any(pattern.match(line) for line in setup_lines)


def _paths_for_root(
    object_paths: list[ObjectPathFact] | None,
    root: str,
) -> list[tuple[str, ...]] | None:
    if not object_paths:
        return None
    paths = [fact.path for fact in object_paths if fact.root == root]
    return paths or None


def _candidate_mentions_param(lines: list[str] | None, param_name: str) -> bool:
    if not lines:
        return False
    return any(re.search(rf"\b{re.escape(param_name)}\b", line) for line in lines)


def _should_symbolize_scalar_param(func: CFunction, p: CParam, candidate_setup_lines: list[str]) -> bool:
    scalar_params = [
        param for param in func.params
        if not param.is_pointer and not param.is_array
    ]
    pointer_params = [
        param for param in func.params
        if param.is_pointer or param.is_array
    ]
    return (
        _candidate_mentions_param(candidate_setup_lines, p.name)
        or (len(scalar_params) == 1 and not pointer_params)
    )


def _local_scalar_decl(p: CParam, value: str) -> str:
    raw_type = p.raw_type.strip()
    if re.search(rf"\b{re.escape(p.name)}\b", raw_type):
        return f"{raw_type} = {value};"
    return f"{raw_type} {p.name} = {value};"


def _scalar_input_type(p: CParam) -> str:
    raw_type = p.raw_type.strip()
    without_name = re.sub(rf"\b{re.escape(p.name)}\b", "", raw_type).strip()
    without_name = re.sub(r"\s+", " ", without_name)
    return without_name or p.base_type


def _symbolic_scalar_input(p: CParam, scalar_bounds: dict[str, tuple[int, int]]) -> GeneratedInput:
    inp: GeneratedInput = {
        "ktest_name": p.name,
        "c_type": _scalar_input_type(p),
        "c_var": p.name,
    }
    if p.base_type in scalar_bounds:
        inp["bounds"] = scalar_bounds[p.base_type]
    return inp


def _append_symbolic_scalar_input(
    inputs: list[GeneratedInput],
    p: CParam,
    scalar_bounds: dict[str, tuple[int, int]],
) -> None:
    if any(inp["ktest_name"] == p.name for inp in inputs):
        return
    inputs.append(_symbolic_scalar_input(p, scalar_bounds))


def _local_pointer_decl(p: CParam, value: str) -> str:
    raw_type = p.raw_type.strip()
    if re.search(rf"\b{re.escape(p.name)}\b", raw_type):
        return f"{raw_type} = {value};"
    return f"{raw_type} {p.name} = {value};"


def _local_void_pointer_setup(p: CParam) -> list[str]:
    buf_name = f"{p.name}_buf"
    return [
        f"uint8_t {buf_name}[{LOCAL_BUFFER_SIZE}];",
        f"memset({buf_name}, 0, sizeof({buf_name}));",
        _local_pointer_decl(p, buf_name),
    ]


def gen_null_setup_body(
    func: CFunction,
    null_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    shaping_features: set[str] | None,
    ops: BodyGenOps,
    ownership: OwnershipSummary | None = None,
) -> BodyResult:
    """
    Generate (body_lines, output_vars, cleanup_lines) for a null-guard test.

    For each function parameter that isn't the null param under test,
    provide a concrete value (constructor for pointers, scalar for others).
    `void *` params are always passed as NULL.
    """
    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    inputs: list[GeneratedInput] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    owned_or_freed_params: set[str] = set()
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)

    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif ops.is_void_star(p):
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            frees_param = _param_frees(ownership, p.name, source_text, func.name, ops)
            takes_ownership = _param_takes_ownership(ownership, p.name, source_text, func.name, ops)
            owns_or_frees_param = frees_param or takes_ownership
            if owns_or_frees_param:
                owned_or_freed_params.add(p.name)
            suppress_constructor_guard = (
                frees_param and
                _param_accepts_null(ownership, p.name, source_text, func.name, ops)
            )
            prefer_raw_heap = (
                frees_param
            )
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p,
                source_text,
                type_catalog,
                function_decls,
                func.name,
                used_names,
                prefer_constructor=frees_param,
                suppress_constructor_guard=suppress_constructor_guard,
                prefer_raw_heap=prefer_raw_heap,
            )
            lines.extend(setup)
            if _param_needs_len_data_shape(ownership, func.name, p, source_text, type_catalog, ops):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not owns_or_frees_param:
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in enabled_features and type_catalog.function_pointer(p.base_type):
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name, str(lo))
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(ops.assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

    args_str = ", ".join(call_args)
    result_val = ops.extract_result_value(behavior.ensures)

    if func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        outputs.append(out_var)
        if result_val is not None:
            sentinel = "out_sentinel"
            lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
            outputs.append(sentinel)
    else:
        lines.append(f"{func.name}({args_str});")
        _append_call_completed_witness(lines, outputs)

    return BodyResult(lines, outputs, cleanup, preamble, inputs)


def gen_valid_setup_body(
    func: CFunction,
    valid_params: list[str],
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    extra_setup: list[str] | None,
    shaping_features: set[str] | None,
    source_shape_oracle: bool,
    source_shape_witnesses: bool,
    ops: BodyGenOps,
    ownership: OwnershipSummary | None = None,
    function_ir: FunctionIR | None = None,
    object_paths: list[ObjectPathFact] | None = None,
    call_arg_overrides: dict[str, str] | None = None,
    witness_setup: list[str] | None = None,
    extra_outputs: list[str] | None = None,
    post_state_facts: list[PostStateFact] | None = None,
    fixture_requirements: list[FixtureRequirement] | None = None,
    branch_facts: list[BranchFact] | None = None,
) -> BodyResult:
    """
    Generate (body_lines, output_vars, cleanup_lines) for a valid-path test.

    Creates proper objects for pointer parameters, uses symbolic scalars.
    """
    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    inputs: list[GeneratedInput] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    owned_or_freed_params: set[str] = set()
    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)
    void_cast_body = "" if ownership is not None else ops.source_for_branch_shaping(source_text, func.name)
    void_cast_types = _void_param_cast_types(ownership, void_cast_body, func, ops)
    non_null_params = set(ops.extract_non_null_params(behavior.assumes))
    nonzero_params = set(ops.extract_nonzero_params(behavior.assumes))
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)
    object_params = set(valid_params) | non_null_params
    null_roots = null_roots_from_branch_facts(branch_facts)
    fixture_requirements = [
        *(fixture_requirements or []),
        *requirements_from_assumptions(behavior.assumes),
        *requirements_for_valid_params(func.params, object_params),
    ]
    fixture_failures = fixture_failure_comments(fixture_requirements)
    fixture_requirements = usable_requirements(fixture_requirements)
    params_by_name = {p.name: p for p in func.params}
    requirement_object_paths = object_path_facts_from_requirements(
        fixture_requirements,
        params_by_name,
    )
    all_object_paths = [
        *(object_paths or []),
        *requirement_object_paths,
    ]
    result_val = ops.extract_result_value(behavior.ensures)
    candidate_setup_lines = [*(extra_setup or []), *(witness_setup or [])]

    for p in func.params:
        if p.is_array:
            zeros = ", ".join(["0"] * (p.array_size or 1))
            lines.append(f"uint8_t {p.name}[{p.array_size or 1}] = {{{zeros}}};")
            call_args.append(p.name)
            param_args[p.name] = p.name
        elif ops.is_void_star(p):
            cast_type = void_cast_types.get(p.name)
            if _candidate_mentions_param(candidate_setup_lines, p.name):
                lines.extend(_local_void_pointer_setup(p))
                call_args.append(p.name)
                param_args[p.name] = p.name
            elif p.name in object_params and cast_type and type_catalog and type_catalog.is_complete_struct(cast_type):
                var_name = ops.unique_name(f"{p.name}_{cast_type}", used_names)
                lines.append(f"{cast_type} {var_name};")
                lines.append(f"memset(&{var_name}, 0, sizeof({var_name}));")
                call_args.append(f"&{var_name}")
                param_args[p.name] = f"&{var_name}"
                param_refs[p.name] = (var_name, ".")
            elif p.name in object_params:
                lines.extend(_local_void_pointer_setup(p))
                call_args.append(p.name)
                param_args[p.name] = p.name
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.is_pointer and p.name in valid_params:
            shaped_paths = _paths_for_root(all_object_paths, p.name)
            frees_param = _param_frees(ownership, p.name, source_text, func.name, ops)
            takes_ownership = _param_takes_ownership(ownership, p.name, source_text, func.name, ops)
            owns_or_frees_param = frees_param or takes_ownership
            if owns_or_frees_param:
                owned_or_freed_params.add(p.name)
            suppress_constructor_guard = (
                frees_param and
                _param_accepts_null(ownership, p.name, source_text, func.name, ops)
            )
            prefer_raw_heap = (
                frees_param
            )
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p,
                source_text,
                type_catalog,
                function_decls,
                func.name,
                used_names,
                prefer_constructor=frees_param and result_val != -1,
                suppress_constructor_guard=suppress_constructor_guard,
                prefer_raw_heap=prefer_raw_heap,
                required_paths=shaped_paths,
                requirements=requirements_for_target(fixture_requirements, p.name),
            )
            lines.extend(setup)
            if _param_needs_len_data_shape(ownership, func.name, p, source_text, type_catalog, ops):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not owns_or_frees_param and not shaped_paths:
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif p.is_pointer:
            if _candidate_mentions_param(candidate_setup_lines, p.name):
                setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                    p,
                    source_text,
                    type_catalog,
                    function_decls,
                    func.name,
                    used_names,
                    required_paths=_paths_for_root(all_object_paths, p.name),
                    requirements=requirements_for_target(fixture_requirements, p.name),
                )
                lines.extend(setup)
                if arg == "NULL":
                    lines.append(_local_pointer_decl(p, "NULL"))
                    arg = p.name
                call_args.append(arg)
                param_args[p.name] = arg
                ref = ops.param_ref_from_arg(arg)
                if ref:
                    param_refs[p.name] = ref
                cleanup.extend(cleanup_for_param)
                if arg != "NULL":
                    active_params[p.name] = p
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif type_catalog and "function-pointers" in enabled_features and (fp_decl := type_catalog.function_pointer(p.base_type)):
            arg = _function_pointer_param_setup(p, fp_decl, lines, preamble, ops)
            call_args.append(arg)
            param_args[p.name] = arg
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            if p.name in scalar_values:
                call_args.append(value)
                param_args[p.name] = value
            elif _should_symbolize_scalar_param(func, p, candidate_setup_lines):
                _append_symbolic_scalar_input(inputs, p, ops.scalar_bounds)
                call_args.append(p.name)
                param_args[p.name] = p.name
            else:
                call_args.append(value)
                param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            if p.name in scalar_values:
                call_args.append(value)
                param_args[p.name] = value
            elif _should_symbolize_scalar_param(func, p, candidate_setup_lines):
                _append_symbolic_scalar_input(inputs, p, ops.scalar_bounds)
                call_args.append(p.name)
                param_args[p.name] = p.name
            else:
                call_args.append(value)
                param_args[p.name] = value

    loose_assumes = _assumes_without_typed_object_path_values(behavior.assumes)
    lines.extend(ops.assumption_setup_lines(loose_assumes, active_params, source_text, param_refs, param_args, shaping_features))
    if all_object_paths:
        missing_object_paths = [
            fact for fact in all_object_paths
            if not _object_path_backed(lines, fact, param_refs)
        ]
        lines.extend(object_path_setup_lines(
            missing_object_paths,
            active_params,
            param_refs,
            type_catalog,
            used_names,
            ops.unique_name,
        ))
    lines.extend(fixture_failures)
    lines.extend(object_path_value_setup_lines(
        fixture_requirements,
        param_refs,
        param_args,
    ))
    if extra_setup:
        lines.extend(ops.rewrite_setup_with_param_args(extra_setup, param_args))

    if call_arg_overrides:
        for index, p in enumerate(func.params):
            override = call_arg_overrides.get(p.name)
            if override is None:
                continue
            call_args[index] = override
            param_args[p.name] = override
            inputs = [inp for inp in inputs if inp["ktest_name"] != p.name]

    lines.extend(object_path_byte_buffer_setup_lines(
        fixture_requirements,
        param_refs,
        param_args,
        ops,
    ))

    args_str = ", ".join(call_args)
    postcondition_witness = acsl_postcondition_witnesses(
        behavior.ensures,
        param_refs,
        param_args,
        active_params,
        owned_or_freed_params,
        null_roots,
        ops,
    )
    if result_val == 0 or (result_val is None and not source_shape_oracle):
        ir_assignment_witness = ir_direct_assignment_witnesses(
            function_ir,
            param_refs,
            param_args,
            active_params,
            owned_or_freed_params,
            null_roots,
            ops,
        )
    else:
        ir_assignment_witness = PostconditionWitness([], [], [])
    post_state_witness = post_state_fact_witnesses(
        post_state_facts,
        param_refs,
        param_args,
        active_params,
        owned_or_freed_params,
        null_roots,
        ops,
    )
    lines.extend(postcondition_witness.before_lines)
    lines.extend(ir_assignment_witness.before_lines)
    lines.extend(post_state_witness.before_lines)

    if source_shape_oracle:
        out_var = "out_ret"
        if func.return_type.strip() not in ("void", ""):
            lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
            if func.return_is_pointer:
                nonnull_var = f"{out_var}_nonnull"
                lines.append(f"int {nonnull_var} = ({out_var} != NULL);")
                outputs.append(nonnull_var)
                append_return_field_outputs(lines, outputs, func, out_var, param_args, type_catalog, ops.scalar_bounds)
                if _returns_owned_pointer(ownership, func, ops):
                    free_fn = ops.lookup_free_fn(func.return_base, source_text, function_decls)
                    if free_fn:
                        cleanup.insert(0, f"if ({out_var}) {free_fn}({out_var});")
            else:
                outputs.append(out_var)
            if result_val is not None:
                sentinel = "out_sentinel"
                lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
                outputs.append(sentinel)
        else:
            lines.append(f"{func.name}({args_str});")
        if source_shape_witnesses:
            append_source_witness_outputs(
                lines,
                outputs,
                func,
                active_params,
                param_refs,
                source_text,
                type_catalog,
                ops,
                ownership,
            )
        if not outputs:
            if _has_pending_witness_outputs(
                postcondition_witness,
                ir_assignment_witness,
                post_state_witness,
                extra_outputs=extra_outputs,
            ):
                lines.append("/* oracle-deferred: post-call witness will be emitted below */")
            else:
                _append_call_completed_witness(lines, outputs)
    elif func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        if func.return_is_pointer:
            nonnull_var = f"{out_var}_nonnull"
            lines.append(f"int {nonnull_var} = ({out_var} != NULL);")
            outputs.append(nonnull_var)
            append_return_field_outputs(lines, outputs, func, out_var, param_args, type_catalog, ops.scalar_bounds)
            if _returns_owned_pointer(ownership, func, ops):
                free_fn = ops.lookup_free_fn(func.return_base, source_text, function_decls)
                if free_fn:
                    cleanup.insert(0, f"if ({out_var}) {free_fn}({out_var});")
        else:
            outputs.append(out_var)
        if result_val is not None:
            sentinel = "out_sentinel"
            lines.append(f"int {sentinel} = ({out_var} == {result_val}) ? 1 : 0;")
            outputs.append(sentinel)
    else:
        lines.append(f"{func.name}({args_str});")
        append_source_witness_outputs(
            lines,
            outputs,
            func,
            active_params,
            param_refs,
            source_text,
            type_catalog,
            ops,
            ownership,
        )
        if outputs or _has_pending_witness_outputs(
            postcondition_witness,
            ir_assignment_witness,
            post_state_witness,
            extra_outputs=extra_outputs,
        ):
            lines.append("/* oracle-deferred: post-call witness will be emitted below */")
        else:
            _append_call_completed_witness(lines, outputs)

    lines.extend(postcondition_witness.after_lines)
    outputs.extend(postcondition_witness.outputs)
    lines.extend(ir_assignment_witness.after_lines)
    outputs.extend(ir_assignment_witness.outputs)
    lines.extend(post_state_witness.after_lines)
    outputs.extend(post_state_witness.outputs)

    if witness_setup:
        lines.extend(ops.rewrite_setup_with_param_args(witness_setup, param_args))
    if extra_outputs:
        outputs.extend(extra_outputs)

    return BodyResult(lines, outputs, cleanup, preamble, inputs)


def gen_mixed_test(
    func: CFunction,
    behavior: ACSLBehavior,
    source_text: str | None,
    type_catalog: CTypeCatalog | None,
    function_decls: dict[str, CFunction] | None,
    shaping_features: set[str] | None,
    ops: BodyGenOps,
    ownership: OwnershipSummary | None = None,
) -> BodyResult:
    """
    Generate body for a mixed behavior where some params are null
    and some are valid. Common for functions with multiple pointer params
    where the contract covers all-null or specific combos.
    """
    null_params = ops.extract_null_params(behavior.assumes)
    valid_params = ops.extract_valid_params(behavior.assumes)

    if null_params and not valid_params:
        return gen_null_setup_body(
            func, null_params, behavior, source_text, type_catalog, function_decls, shaping_features, ops, ownership
        )

    if valid_params and not null_params:
        return gen_valid_setup_body(
            func, valid_params, behavior, source_text, type_catalog, function_decls, None, shaping_features, False, False, ops, ownership
        )

    lines: list[str] = []
    call_args: list[str] = []
    outputs: list[str] = []
    cleanup: list[str] = []
    preamble: list[str] = []
    inputs: list[GeneratedInput] = []
    used_names: set[str] = set()
    active_params: dict[str, CParam] = {}
    param_args: dict[str, str] = {}
    param_refs: dict[str, tuple[str, str]] = {}
    enabled_features = shaping_features if shaping_features is not None else set(ops.default_shaping_features)
    non_null_params = set(ops.extract_non_null_params(behavior.assumes))
    nonzero_params = set(ops.extract_nonzero_params(behavior.assumes))
    scalar_values = ops.scalar_values_from_assumptions(behavior.assumes)

    for p in func.params:
        if p.name in null_params:
            call_args.append("NULL")
            param_args[p.name] = "NULL"
        elif p.is_pointer:
            frees_param = _param_frees(ownership, p.name, source_text, func.name, ops)
            takes_ownership = _param_takes_ownership(ownership, p.name, source_text, func.name, ops)
            owns_or_frees_param = frees_param or takes_ownership
            suppress_constructor_guard = (
                frees_param and
                _param_accepts_null(ownership, p.name, source_text, func.name, ops)
            )
            prefer_raw_heap = (
                frees_param
            )
            setup, arg, cleanup_for_param = ops.pointer_argument_setup(
                p,
                source_text,
                type_catalog,
                function_decls,
                func.name,
                used_names,
                prefer_constructor=frees_param,
                suppress_constructor_guard=suppress_constructor_guard,
                prefer_raw_heap=prefer_raw_heap,
            )
            lines.extend(setup)
            if _param_needs_len_data_shape(ownership, func.name, p, source_text, type_catalog, ops):
                ops.append_len_data_shape(lines, arg)
            call_args.append(arg)
            param_args[p.name] = arg
            ref = ops.param_ref_from_arg(arg)
            if ref:
                param_refs[p.name] = ref
            if not owns_or_frees_param:
                cleanup.extend(cleanup_for_param)
            if arg != "NULL":
                active_params[p.name] = p
        elif type_catalog and "function-pointers" in enabled_features and (fp_decl := type_catalog.function_pointer(p.base_type)):
            if p.name in non_null_params:
                arg = _function_pointer_param_setup(p, fp_decl, lines, preamble, ops)
                call_args.append(arg)
                param_args[p.name] = arg
            else:
                call_args.append("NULL")
                param_args[p.name] = "NULL"
        elif p.base_type in ops.scalar_bounds:
            lo, _ = ops.scalar_bounds[p.base_type]
            value = scalar_values.get(p.name)
            if value is None:
                value = "1" if p.name in nonzero_params and lo == 0 else str(lo)
            call_args.append(value)
            param_args[p.name] = value
        else:
            value = scalar_values.get(p.name, "0")
            call_args.append(value)
            param_args[p.name] = value

    lines.extend(ops.assumption_setup_lines(behavior.assumes, active_params, source_text, param_refs, param_args, shaping_features))

    args_str = ", ".join(call_args)

    if func.return_type.strip() not in ("void", ""):
        out_var = "out_ret"
        lines.append(f"{func.return_type} {out_var} = {func.name}({args_str});")
        outputs.append(out_var)
    else:
        lines.append(f"{func.name}({args_str});")
        _append_call_completed_witness(lines, outputs)

    return BodyResult(lines, outputs, cleanup, preamble, inputs)
