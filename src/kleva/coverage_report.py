from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class CandidateCoverage:
    name:            str
    source_location: str | None = None
    target_branch:   str | None = None
    proven:          bool = True
    origin:          str | None = None
    facts:           list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class BranchCoverage:
    source_location: str | None
    target_branch:   str | None
    covered:         bool


@dataclass(frozen=True)
class CandidateCoverageMatch:
    candidate: CandidateCoverage
    branch:    BranchCoverage | None


@dataclass(frozen=True)
class CoverageSummary:
    matched:                  list[CandidateCoverageMatch] = field(default_factory=list)
    uncovered_without_candidate: list[BranchCoverage] = field(default_factory=list)
    unproved_candidates:      list[CandidateCoverage] = field(default_factory=list)


@dataclass(frozen=True)
class RegexRetirementDecision:
    can_retire: bool
    blockers:   list[str] = field(default_factory=list)


def summarize_candidate_coverage(
    candidates: list[CandidateCoverage],
    branches:   list[BranchCoverage],
) -> CoverageSummary:
    """
    Match generated candidates to coverage facts by explicit source metadata.

    This is deliberately report-only: it consumes already-produced candidate
    and branch facts, and returns a summary for humans. It does not generate,
    mutate, or rank future candidates.
    """
    branch_by_key = {
        _branch_key(branch.source_location, branch.target_branch): branch
        for branch in branches
        if _branch_key(branch.source_location, branch.target_branch) is not None
    }
    candidate_keys = {
        key
        for candidate in candidates
        if (key := _branch_key(candidate.source_location, candidate.target_branch)) is not None
    }

    matched: list[CandidateCoverageMatch] = []
    unproved: list[CandidateCoverage] = []
    for candidate in candidates:
        key = _branch_key(candidate.source_location, candidate.target_branch)
        branch = branch_by_key.get(key) if key is not None else None
        matched.append(CandidateCoverageMatch(candidate, branch))
        if not candidate.proven:
            unproved.append(candidate)

    uncovered_without_candidate = [
        branch for branch in branches
        if not branch.covered
        and (key := _branch_key(branch.source_location, branch.target_branch)) is not None
        and key not in candidate_keys
    ]

    return CoverageSummary(matched, uncovered_without_candidate, unproved)


def assess_regex_retirement(summary: CoverageSummary) -> RegexRetirementDecision:
    """
    Decide whether regex fallback shapers are safe to remove.

    This is intentionally conservative. Regex fallback removal is allowed only
    when the report has no regex-origin candidates, no unknown-origin
    candidates, no unproved candidates, and no uncovered branches without a
    candidate.
    """
    blockers: list[str] = []

    regex_candidates = [
        match.candidate.name
        for match in summary.matched
        if match.candidate.origin == "regex"
    ]
    unknown_candidates = [
        match.candidate.name
        for match in summary.matched
        if not match.candidate.origin
    ]

    if regex_candidates:
        blockers.append(
            "regex-origin candidates remain: " + ", ".join(regex_candidates)
        )
    if unknown_candidates:
        blockers.append(
            "unknown-origin candidates remain: " + ", ".join(unknown_candidates)
        )
    if summary.unproved_candidates:
        blockers.append(
            "unproved candidates remain: "
            + ", ".join(candidate.name for candidate in summary.unproved_candidates)
        )
    if summary.uncovered_without_candidate:
        blockers.append(
            f"uncovered branches without candidates remain: {len(summary.uncovered_without_candidate)}"
        )

    return RegexRetirementDecision(not blockers, blockers)


def render_coverage_summary(summary: CoverageSummary) -> str:
    retirement = assess_regex_retirement(summary)
    lines: list[str] = [
        "# KLEVA Coverage Candidate Report",
        "",
        "Coverage is report-only. It is not used to generate or rank candidates.",
        "",
        "## Candidate Mapping",
        "",
    ]

    if summary.matched:
        for match in summary.matched:
            candidate = match.candidate
            branch = match.branch
            coverage = "unknown" if branch is None else ("covered" if branch.covered else "uncovered")
            proof = "proven" if candidate.proven else "unproved"
            lines.append(
                f"- {candidate.name}: coverage={coverage} proof={proof} "
                f"origin={_display(candidate.origin)} "
                f"source={_display(candidate.source_location)} branch={_display(candidate.target_branch)}"
                f"{_facts_suffix(candidate.facts)}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Uncovered Branches Without Candidate", ""])
    if summary.uncovered_without_candidate:
        for branch in summary.uncovered_without_candidate:
            lines.append(
                f"- source={_display(branch.source_location)} branch={_display(branch.target_branch)}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Unproved Candidates", ""])
    if summary.unproved_candidates:
        for candidate in summary.unproved_candidates:
            lines.append(
                f"- {candidate.name}: origin={_display(candidate.origin)} "
                f"source={_display(candidate.source_location)} "
                f"branch={_display(candidate.target_branch)}"
                f"{_facts_suffix(candidate.facts)}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Regex Fallback Retirement", ""])
    if retirement.can_retire:
        lines.append("- status=ready")
    else:
        lines.append("- status=blocked")
        for blocker in retirement.blockers:
            lines.append(f"- blocker: {blocker}")

    return "\n".join(lines) + "\n"


def load_coverage_summary(path: str | Path) -> CoverageSummary:
    """
    Load external coverage facts from YAML and summarize them.

    Expected shape:

        candidates:
          - name: case_open
            source_location: ir:run:switch[0]
            target_branch: switch ctx->state case 1
            proven: true
        branches:
          - source_location: ir:run:switch[0]
            target_branch: switch ctx->state case 1
            covered: true

    These facts are a report input only. They are never fed back into
    synthesis.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    candidates = [
        CandidateCoverage(
            name=str(item["name"]),
            source_location=item.get("source_location"),
            target_branch=item.get("target_branch"),
            proven=bool(item.get("proven", True)),
            origin=item.get("origin"),
            facts=[
                {str(k): str(v) for k, v in fact.items()}
                for fact in item.get("candidate_facts", item.get("facts", []))
            ],
        )
        for item in data.get("candidates", [])
    ]
    branches = [
        BranchCoverage(
            source_location=item.get("source_location"),
            target_branch=item.get("target_branch"),
            covered=bool(item.get("covered", False)),
        )
        for item in data.get("branches", [])
    ]
    return summarize_candidate_coverage(candidates, branches)


def write_coverage_report(facts_path: str | Path, out_path: str | Path) -> None:
    summary = load_coverage_summary(facts_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_coverage_summary(summary))


def _branch_key(source_location: str | None, target_branch: str | None) -> tuple[str, str] | None:
    if not source_location or not target_branch:
        return None
    return (source_location, target_branch)


def _display(value: str | None) -> str:
    return value if value else "not_recorded"


def _facts_suffix(facts: list[dict[str, str]]) -> str:
    if not facts:
        return ""
    rendered: list[str] = []
    for fact in facts:
        if fact.get("kind") == "branch":
            rendered.append(
                f"branch:{fact.get('target', '?')} {fact.get('relation', '?')} {fact.get('value', '?')}"
            )
        elif fact.get("kind") == "call":
            rendered.append(
                f"call:{fact.get('callee', '?')} {fact.get('mode', '?')} {fact.get('outcome', '?')}"
            )
        elif fact.get("kind") == "post_state":
            rendered.append(
                f"post:{fact.get('target', '?')} {fact.get('relation', '?')} {fact.get('value', '?')}"
            )
        else:
            rendered.append(str(fact))
    return " facts=[" + "; ".join(rendered) + "]"
