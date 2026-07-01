from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from ..fixtures.requirements import FixtureRequirement
from ..ir.model import ArraySubscript, Expr, FieldAccess, SourceLocation, VarRef


@dataclass(frozen=True)
class ObjectPathFact:
    """A typed path that must be backed before a candidate can assign through it."""
    root:      str
    path:      tuple[str, ...]
    root_type: str | None = None
    value_type: str | None = None


@dataclass(frozen=True)
class BranchFact:
    """A typed branch fact a candidate tries to make true."""
    target:   str
    relation: str
    value:    str


@dataclass(frozen=True)
class CallOutcomeFact:
    """A typed callee outcome a candidate tries to make reachable."""
    callee: str
    mode:   str
    outcome: str


@dataclass(frozen=True)
class NullnessFact:
    """A selected-path nullness fact."""
    target: str
    state:  str


@dataclass(frozen=True)
class ScalarIntervalFact:
    """A selected-path scalar interval fact."""
    target: str
    lower:  str | None = None
    upper:  str | None = None
    exact:  str | None = None


@dataclass(frozen=True)
class OwnershipPathFact:
    """A selected-path ownership fact propagated from a call chain."""
    target: str
    action: str
    via:    str


@dataclass(frozen=True)
class HelperSideEffectFact:
    """A generic helper side effect observed on a selected path."""
    kind:     str
    target:   str
    value:    str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class StateTransitionFact:
    """A generic state-machine transition fact."""
    selector: str
    source:   str
    target:   str
    guard:    str | None = None
    via:      str | None = None


@dataclass(frozen=True)
class DiversityFact:
    """A curated value/content diversity goal."""
    target: str
    kind:   str
    value:  str


@dataclass(frozen=True)
class PostStateFact:
    """A typed object state expected after a candidate reaches a side effect."""
    target:   str
    relation: str
    value:    str


SemanticFact = Union[
    BranchFact,
    CallOutcomeFact,
    NullnessFact,
    ScalarIntervalFact,
    OwnershipPathFact,
    HelperSideEffectFact,
    StateTransitionFact,
    DiversityFact,
    ObjectPathFact,
    PostStateFact,
]


@dataclass
class BranchCandidate:
    """A source-derived path goal that can be added to a valid fixture."""
    name:              str
    setup:             list[str]
    preamble:          list[str] = field(default_factory=list)
    oracle:            bool = True
    witness_outputs:   bool = False
    source_location:   str | None = None
    target_branch:     str | None = None
    origin:            str | None = None
    object_paths:      list[ObjectPathFact] = field(default_factory=list)
    branch_facts:      list[BranchFact] = field(default_factory=list)
    call_facts:        list[CallOutcomeFact] = field(default_factory=list)
    nullness_facts:    list[NullnessFact] = field(default_factory=list)
    interval_facts:    list[ScalarIntervalFact] = field(default_factory=list)
    ownership_facts:   list[OwnershipPathFact] = field(default_factory=list)
    side_effect_facts: list[HelperSideEffectFact] = field(default_factory=list)
    transition_facts:  list[StateTransitionFact] = field(default_factory=list)
    diversity_facts:   list[DiversityFact] = field(default_factory=list)
    post_state_facts:  list[PostStateFact] = field(default_factory=list)
    fixture_requirements: list[FixtureRequirement] = field(default_factory=list)
    call_arg_overrides: dict[str, str] = field(default_factory=dict)
    witness_setup:     list[str] = field(default_factory=list)
    extra_outputs:     list[str] = field(default_factory=list)

    def semantic_facts(self) -> tuple[SemanticFact, ...]:
        return (
            *self.branch_facts,
            *self.call_facts,
            *self.object_paths,
            *self.nullness_facts,
            *self.interval_facts,
            *self.ownership_facts,
            *self.side_effect_facts,
            *self.transition_facts,
            *self.diversity_facts,
            *self.post_state_facts,
        )

    def semantic_fact_dicts(self) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        nullness_facts = _dedup_nullness([
            *_nullness_facts_from_branch_facts(self.branch_facts),
            *self.nullness_facts,
        ])
        interval_facts = _dedup_intervals([
            *_interval_facts_from_branch_facts(self.branch_facts),
            *self.interval_facts,
        ])
        for fact in self.branch_facts:
            facts.append({
                "kind": "branch",
                "target": fact.target,
                "relation": fact.relation,
                "value": fact.value,
            })
        for fact in self.call_facts:
            facts.append({
                "kind": "call",
                "callee": fact.callee,
                "mode": fact.mode,
                "outcome": fact.outcome,
            })
        for fact in self.object_paths:
            facts.append({
                "kind": "object_path",
                "root": fact.root,
                "path": "->".join(fact.path),
            })
        for fact in nullness_facts:
            facts.append({
                "kind": "nullness",
                "target": fact.target,
                "state": fact.state,
            })
        for fact in interval_facts:
            entry = {
                "kind": "interval",
                "target": fact.target,
            }
            if fact.exact is not None:
                entry["exact"] = fact.exact
            if fact.lower is not None:
                entry["lower"] = fact.lower
            if fact.upper is not None:
                entry["upper"] = fact.upper
            facts.append(entry)
        for fact in self.ownership_facts:
            facts.append({
                "kind": "ownership",
                "target": fact.target,
                "action": fact.action,
                "via": fact.via,
            })
        for fact in self.side_effect_facts:
            entry = {
                "kind": "helper_effect",
                "effect": fact.kind,
                "target": fact.target,
            }
            if fact.value is not None:
                entry["value"] = fact.value
            if fact.evidence is not None:
                entry["evidence"] = fact.evidence
            facts.append(entry)
        for fact in self.transition_facts:
            entry = {
                "kind": "transition",
                "selector": fact.selector,
                "from": fact.source,
                "to": fact.target,
            }
            if fact.guard is not None:
                entry["guard"] = fact.guard
            if fact.via is not None:
                entry["via"] = fact.via
            facts.append(entry)
        for fact in self.diversity_facts:
            facts.append({
                "kind": "diversity",
                "target": fact.target,
                "diversity": fact.kind,
                "value": fact.value,
            })
        for fact in self.post_state_facts:
            facts.append({
                "kind": "post_state",
                "target": fact.target,
                "relation": fact.relation,
                "value": fact.value,
            })
        return facts


def _nullness_facts_from_branch_facts(facts: list[BranchFact]) -> list[NullnessFact]:
    out: list[NullnessFact] = []
    for fact in facts:
        if fact.value not in {"0", "NULL", "\\null"}:
            continue
        if fact.relation in {"==", "is"}:
            out.append(NullnessFact(fact.target, "null"))
        elif fact.relation in {"!=", "is_not"}:
            out.append(NullnessFact(fact.target, "non-null"))
    return out


def _interval_facts_from_branch_facts(facts: list[BranchFact]) -> list[ScalarIntervalFact]:
    out: list[ScalarIntervalFact] = []
    for fact in facts:
        if not _looks_scalar_bound(fact.value):
            continue
        if fact.relation == "==":
            out.append(ScalarIntervalFact(fact.target, exact=fact.value))
        elif fact.relation == ">=":
            out.append(ScalarIntervalFact(fact.target, lower=fact.value))
        elif fact.relation == ">":
            out.append(ScalarIntervalFact(fact.target, lower=f"({fact.value}) + 1"))
        elif fact.relation == "<=":
            out.append(ScalarIntervalFact(fact.target, upper=fact.value))
        elif fact.relation == "<":
            out.append(ScalarIntervalFact(fact.target, upper=f"({fact.value}) - 1"))
    return out


def _looks_scalar_bound(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Za-z_]\w*", value))


def _dedup_nullness(facts: list[NullnessFact]) -> list[NullnessFact]:
    out: list[NullnessFact] = []
    seen: set[NullnessFact] = set()
    for fact in facts:
        if fact in seen:
            continue
        seen.add(fact)
        out.append(fact)
    return out


def _dedup_intervals(facts: list[ScalarIntervalFact]) -> list[ScalarIntervalFact]:
    out: list[ScalarIntervalFact] = []
    seen: set[ScalarIntervalFact] = set()
    for fact in facts:
        if fact in seen:
            continue
        seen.add(fact)
        out.append(fact)
    return out


def display_source_location(loc: SourceLocation | None, fallback: str) -> str:
    if loc:
        display = loc.display()
        if display:
            return display
    return fallback


def object_path_fact(expr: Expr) -> ObjectPathFact | None:
    path: list[str] = []
    value_type = getattr(expr, "c_type", None)
    current = expr
    while isinstance(current, FieldAccess):
        path.append(current.field)
        current = current.base
        if isinstance(current, ArraySubscript):
            current = current.base
    if not isinstance(current, VarRef) or not path:
        return None
    return ObjectPathFact(
        root=current.name,
        path=tuple(reversed(path)),
        root_type=current.c_type,
        value_type=value_type,
    )


def object_path_facts_from_expr(expr: Expr) -> list[ObjectPathFact]:
    facts: list[ObjectPathFact] = []

    def visit(node: Expr) -> None:
        fact = object_path_fact(node)
        if fact is not None:
            facts.append(fact)
        for value in vars(node).values():
            if isinstance(value, Expr):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Expr):
                        visit(item)

    visit(expr)
    return _dedup_object_path_facts(facts)


def _dedup_object_path_facts(facts: list[ObjectPathFact]) -> list[ObjectPathFact]:
    out: list[ObjectPathFact] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for fact in facts:
        key = (fact.root, fact.path)
        if key in seen:
            continue
        if any(
            other.root == fact.root
            and len(other.path) > len(fact.path)
            and other.path[:len(fact.path)] == fact.path
            for other in facts
        ):
            continue
        seen.add(key)
        out.append(fact)
    return out
