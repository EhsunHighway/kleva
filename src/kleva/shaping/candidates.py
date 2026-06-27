from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from ..ir.model import Expr, FieldAccess, SourceLocation, VarRef


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
class PostStateFact:
    """A typed object state expected after a candidate reaches a side effect."""
    target:   str
    relation: str
    value:    str


SemanticFact = Union[BranchFact, CallOutcomeFact, PostStateFact]


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
    post_state_facts:  list[PostStateFact] = field(default_factory=list)
    call_arg_overrides: dict[str, str] = field(default_factory=dict)
    witness_setup:     list[str] = field(default_factory=list)
    extra_outputs:     list[str] = field(default_factory=list)

    def semantic_facts(self) -> tuple[SemanticFact, ...]:
        return (*self.branch_facts, *self.call_facts, *self.post_state_facts)

    def semantic_fact_dicts(self) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
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
        for fact in self.post_state_facts:
            facts.append({
                "kind": "post_state",
                "target": fact.target,
                "relation": fact.relation,
                "value": fact.value,
            })
        return facts


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
