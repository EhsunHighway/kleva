from __future__ import annotations

import re

from ..ast.model import CFunction, CParam
from ..fixtures.requirements import byte_buffer
from ..ir.naming import safe_name
from .candidates import BranchCandidate, DiversityFact


def curated_diversity_candidates(
    func: CFunction,
    scalar_bounds: dict[str, tuple[int, int]],
    assumes: list[str] | None = None,
) -> list[BranchCandidate]:
    """
    Generate bounded value/content diversity candidates.

    This shaper is deliberately not a Cartesian-product fuzzer. Each candidate
    changes one scalar or one byte buffer at a time, so KLEE/EVA can still
    certify the resulting recipe before it becomes a trusted unit test.
    """
    scalar_constraints = _scalar_constraints_from_assumptions(assumes or [])
    candidates: list[BranchCandidate] = []
    for param in func.params:
        candidates.extend(_scalar_param_candidates(param, scalar_bounds, scalar_constraints.get(param.name)))
        candidates.extend(_byte_buffer_candidates(param))
    return _dedupe_by_name(candidates)


def _scalar_param_candidates(
    param: CParam,
    scalar_bounds: dict[str, tuple[int, int]],
    constraint: "ScalarConstraint | None" = None,
) -> list[BranchCandidate]:
    if param.is_pointer or param.is_array:
        return []
    if param.base_type not in scalar_bounds and not _is_size_like(param):
        return []
    values = _curated_scalar_values(param, scalar_bounds, constraint)
    candidates: list[BranchCandidate] = []
    for label, value in values:
        candidates.append(BranchCandidate(
            name=f"ir_diversity_{safe_name(param.name)}_{label}",
            setup=[],
            origin="ir",
            source_location=f"ir:{param.name}:diversity",
            target_branch=f"diversity scalar {param.name} {label}",
            diversity_facts=[DiversityFact(param.name, "scalar", value)],
            call_arg_overrides={param.name: value},
        ))
    return candidates


def _byte_buffer_candidates(param: CParam) -> list[BranchCandidate]:
    if not param.is_pointer or param.base_type != "uint8_t":
        return []
    return [
        BranchCandidate(
            name=f"ir_diversity_{safe_name(param.name)}_all_zero",
            setup=[f"/* diversity: byte-buffer {param.name} all-zero */"],
            origin="ir",
            source_location=f"ir:{param.name}:diversity",
            target_branch=f"diversity byte-buffer {param.name} all-zero",
            diversity_facts=[DiversityFact(param.name, "byte-buffer", "all-zero")],
            fixture_requirements=[byte_buffer(param.name, content="all-zero")],
        ),
        BranchCandidate(
            name=f"ir_diversity_{safe_name(param.name)}_all_0xff",
            setup=[f"/* diversity: byte-buffer {param.name} all-0xff */"],
            origin="ir",
            source_location=f"ir:{param.name}:diversity",
            target_branch=f"diversity byte-buffer {param.name} all-0xff",
            diversity_facts=[DiversityFact(param.name, "byte-buffer", "all-0xff")],
            fixture_requirements=[byte_buffer(param.name, content="all-0xff")],
        ),
        BranchCandidate(
            name=f"ir_diversity_{safe_name(param.name)}_first_byte_set",
            setup=[f"/* diversity: byte-buffer {param.name} first-byte-set */"],
            origin="ir",
            source_location=f"ir:{param.name}:diversity",
            target_branch=f"diversity byte-buffer {param.name} first-byte-set",
            diversity_facts=[DiversityFact(param.name, "byte-buffer", "first-byte-set")],
            fixture_requirements=[byte_buffer(param.name, content="first-byte-set")],
        ),
    ]


def _curated_scalar_values(
    param: CParam,
    scalar_bounds: dict[str, tuple[int, int]],
    constraint: "ScalarConstraint | None" = None,
) -> list[tuple[str, str]]:
    lo, hi = scalar_bounds.get(param.base_type, (0, 1))
    raw_values: list[tuple[str, int]] = []
    if _is_size_like(param):
        raw_values.extend([
            ("zero", 0),
            ("one", 1),
            ("two", 2),
        ])
    elif param.base_type == "uint8_t":
        raw_values.extend([
            ("zero", 0),
            ("one", 1),
            ("max", 255),
        ])
    else:
        raw_values.extend([
            ("min", lo),
            ("one", 1),
        ])
        if hi <= 65535:
            raw_values.append(("max", hi))

    out: list[tuple[str, str]] = []
    seen_values: set[int] = set()
    for label, value in raw_values:
        below_allowed = _is_size_like(param) and value == 0
        if ((value < lo and not below_allowed) or value > hi or value in seen_values):
            continue
        if constraint is not None and not constraint.allows(value):
            continue
        seen_values.add(value)
        out.append((label, str(value)))
    return out


def _is_size_like(param: CParam) -> bool:
    name = param.name.lower()
    return (
        param.base_type == "size_t"
        or name.endswith("len")
        or "len" in name
        or "size" in name
        or "count" in name
        or "capacity" in name
    )


def _dedupe_by_name(candidates: list[BranchCandidate]) -> list[BranchCandidate]:
    out: list[BranchCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.name in seen:
            continue
        seen.add(candidate.name)
        out.append(candidate)
    return out


class ScalarConstraint:
    def __init__(self) -> None:
        self.min_value: int | None = None
        self.min_inclusive = True
        self.max_value: int | None = None
        self.max_inclusive = True
        self.forbidden: set[int] = set()
        self.allowed_exact: int | None = None

    def add_lower_bound(self, value: int, inclusive: bool) -> None:
        if self.min_value is None or value > self.min_value:
            self.min_value = value
            self.min_inclusive = inclusive
            return
        if value == self.min_value:
            self.min_inclusive = self.min_inclusive and inclusive

    def add_upper_bound(self, value: int, inclusive: bool) -> None:
        if self.max_value is None or value < self.max_value:
            self.max_value = value
            self.max_inclusive = inclusive
            return
        if value == self.max_value:
            self.max_inclusive = self.max_inclusive and inclusive

    def allows(self, value: int) -> bool:
        if self.allowed_exact is not None and value != self.allowed_exact:
            return False
        if value in self.forbidden:
            return False
        if self.min_value is not None:
            if value < self.min_value or (value == self.min_value and not self.min_inclusive):
                return False
        if self.max_value is not None:
            if value > self.max_value or (value == self.max_value and not self.max_inclusive):
                return False
        return True


def _scalar_constraints_from_assumptions(assumes: list[str]) -> dict[str, ScalarConstraint]:
    constraints: dict[str, ScalarConstraint] = {}
    for expr in assumes:
        for part in re.split(r"\|\||&&", expr):
            part = part.strip()
            if part:
                _record_scalar_constraint(part, constraints)
    return constraints


def _constraint_for(name: str, constraints: dict[str, ScalarConstraint]) -> ScalarConstraint:
    if name not in constraints:
        constraints[name] = ScalarConstraint()
    return constraints[name]


def _record_scalar_constraint(part: str, constraints: dict[str, ScalarConstraint]) -> None:
    m = re.fullmatch(r"([A-Za-z_]\w*)\s*(==|!=|>=|>|<=|<)\s*(-?0x[0-9a-fA-F]+|-?\d+)", part)
    if m:
        lhs, op, raw = m.groups()
        _apply_relation(_constraint_for(lhs, constraints), op, _parse_int(raw))
        return

    m = re.fullmatch(r"(-?0x[0-9a-fA-F]+|-?\d+)\s*(==|!=|>=|>|<=|<)\s*([A-Za-z_]\w*)", part)
    if m:
        raw, op, rhs = m.groups()
        _apply_relation(_constraint_for(rhs, constraints), _flip_relation(op), _parse_int(raw))


def _apply_relation(constraint: ScalarConstraint, op: str, value: int) -> None:
    if op == "==":
        constraint.allowed_exact = value
    elif op == "!=":
        constraint.forbidden.add(value)
    elif op == ">":
        constraint.add_lower_bound(value, inclusive=False)
    elif op == ">=":
        constraint.add_lower_bound(value, inclusive=True)
    elif op == "<":
        constraint.add_upper_bound(value, inclusive=False)
    elif op == "<=":
        constraint.add_upper_bound(value, inclusive=True)


def _flip_relation(op: str) -> str:
    return {
        "==": "==",
        "!=": "!=",
        ">": "<",
        ">=": "<=",
        "<": ">",
        "<=": ">=",
    }[op]


def _parse_int(raw: str) -> int:
    return int(raw, 16) if raw.lower().startswith(("0x", "-0x")) else int(raw)
