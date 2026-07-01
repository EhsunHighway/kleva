from __future__ import annotations

import re
from dataclasses import dataclass

from .candidates import BranchCandidate


@dataclass(frozen=True)
class CandidateReduction:
    kept:             list[BranchCandidate]
    original_count:   int
    deduped_count:    int
    budget_skip_count: int


def reduce_branch_candidates(
    candidates:       list[BranchCandidate],
    *,
    max_candidates:   int = 12,
    max_per_family:   int = 4,
    max_diversity:    int = 3,
) -> CandidateReduction:
    """
    Collapse equivalent implementation-shaped candidates and cap noisy families.

    This is intentionally semantic, not domain-specific. It does not know
    packets, ARP, sockets, or TCP states. It looks at candidate facts and
    generic candidate classes such as condition, table, parser, and diversity.
    """
    kept: list[BranchCandidate] = []
    seen_semantics: set[tuple] = set()
    family_counts: dict[tuple[str, str], int] = {}
    diversity_count = 0
    deduped = 0
    budget_skipped = 0

    for candidate in candidates:
        semantic_key = _semantic_key(candidate)
        if semantic_key in seen_semantics:
            deduped += 1
            continue

        family_key = _family_key(candidate)
        if family_key[0] == "diversity":
            if diversity_count >= max_diversity:
                budget_skipped += 1
                continue
        elif family_counts.get(family_key, 0) >= max_per_family:
            budget_skipped += 1
            continue

        if len(kept) >= max_candidates:
            budget_skipped += 1
            continue

        kept.append(candidate)
        seen_semantics.add(semantic_key)
        family_counts[family_key] = family_counts.get(family_key, 0) + 1
        if family_key[0] == "diversity":
            diversity_count += 1

    return CandidateReduction(
        kept=kept,
        original_count=len(candidates),
        deduped_count=deduped,
        budget_skip_count=budget_skipped,
    )


def _semantic_key(candidate: BranchCandidate) -> tuple:
    facts = tuple(sorted(_normalized_fact_dict(fact) for fact in candidate.semantic_fact_dicts()))
    if facts:
        return ("facts", facts)
    return (
        "shape",
        candidate.origin or "",
        tuple(_normalize_setup_line(line) for line in candidate.setup),
        tuple(candidate.call_arg_overrides.items()),
    )


def _normalized_fact_dict(fact: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, _normalize_fact_value(key, value)) for key, value in fact.items()))


def _normalize_fact_value(key: str, value: str) -> str:
    if key in {"target", "root"}:
        return _normalize_path(value)
    if key == "path":
        return _normalize_path(value)
    return value


def _normalize_setup_line(line: str) -> str:
    return _normalize_path(re.sub(r"\s+", " ", line.strip()))


def _normalize_path(value: str) -> str:
    value = re.sub(r"\[\s*(?:\d+|[A-Za-z_]\w*)\s*\]", "[]", value)
    value = re.sub(r"\b[A-Za-z_]\w*->", "ROOT->", value, count=1)
    return value


def _family_key(candidate: BranchCandidate) -> tuple[str, str]:
    name = candidate.name
    if name.startswith("ir_diversity_"):
        return ("diversity", "all")

    if match := re.match(r"ir_if_(\d+)_", name):
        return ("condition", match.group(1))

    if match := re.match(r"ir_table_(.+?)_(hit|miss|full|first_free|duplicate)$", name):
        return ("table", match.group(1))

    if match := re.match(r"ir_forbidden_value_(\d+)_", name):
        return ("forbidden", match.group(1))

    if match := re.match(r"ir_parser_(\d+)_", name):
        return ("parser", match.group(1))

    if candidate.source_location:
        return (candidate.origin or "candidate", candidate.source_location)

    return (candidate.origin or "candidate", name)
