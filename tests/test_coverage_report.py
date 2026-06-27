import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kleva.coverage_report import (
    assess_regex_retirement,
    BranchCoverage,
    CandidateCoverage,
    load_coverage_summary,
    render_coverage_summary,
    summarize_candidate_coverage,
    write_coverage_report,
)


class CoverageReportTests(unittest.TestCase):
    def test_maps_candidates_to_coverage_by_source_and_branch(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_open",
                    "ir:run:switch[0]",
                    "switch ctx->state case 1",
                )
            ],
            [
                BranchCoverage(
                    "ir:run:switch[0]",
                    "switch ctx->state case 1",
                    covered=True,
                )
            ],
        )

        self.assertEqual(len(summary.matched), 1)
        self.assertTrue(summary.matched[0].branch.covered)
        self.assertEqual(summary.uncovered_without_candidate, [])

    def test_reports_uncovered_branch_without_candidate(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_open",
                    "ir:run:switch[0]",
                    "switch ctx->state case 1",
                )
            ],
            [
                BranchCoverage(
                    "ir:run:switch[0]",
                    "switch ctx->state case 1",
                    covered=True,
                ),
                BranchCoverage(
                    "ir:run:switch[0]",
                    "switch ctx->state case 2",
                    covered=False,
                ),
            ],
        )

        self.assertEqual(
            summary.uncovered_without_candidate,
            [
                BranchCoverage(
                    "ir:run:switch[0]",
                    "switch ctx->state case 2",
                    covered=False,
                )
            ],
        )

    def test_keeps_unproved_candidates_separate_from_uncovered_branches(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_unproved",
                    "ir:run:if[0]",
                    "if ctx->ready",
                    proven=False,
                    origin="ir",
                    facts=[
                        {"kind": "branch", "target": "ctx->ready", "relation": "!=", "value": "0"},
                    ],
                )
            ],
            [
                BranchCoverage(
                    "ir:run:if[0]",
                    "if ctx->ready",
                    covered=False,
                )
            ],
        )

        self.assertEqual([c.name for c in summary.unproved_candidates], ["case_unproved"])
        self.assertEqual(summary.uncovered_without_candidate, [])

    def test_render_summary_is_report_only_and_grouped(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_unproved",
                    "ir:run:if[0]",
                    "if ctx->ready",
                    proven=False,
                    origin="ir",
                    facts=[
                        {"kind": "branch", "target": "ctx->ready", "relation": "!=", "value": "0"},
                    ],
                )
            ],
            [
                BranchCoverage(
                    "ir:run:if[0]",
                    "if ctx->ready",
                    covered=False,
                ),
                BranchCoverage(
                    "ir:run:if[1]",
                    "if ctx->closed",
                    covered=False,
                ),
            ],
        )

        text = render_coverage_summary(summary)

        self.assertIn("Coverage is report-only", text)
        self.assertIn("case_unproved: coverage=uncovered proof=unproved origin=ir", text)
        self.assertIn("facts=[branch:ctx->ready != 0]", text)
        self.assertIn("## Uncovered Branches Without Candidate", text)
        self.assertIn("branch=if ctx->closed", text)
        self.assertIn("## Unproved Candidates", text)
        self.assertIn("## Regex Fallback Retirement", text)
        self.assertIn("status=blocked", text)
        self.assertIn("unproved candidates remain: case_unproved", text)

    def test_loads_external_yaml_facts(self):
        with TemporaryDirectory() as td:
            facts = Path(td) / "facts.yaml"
            facts.write_text(
                """
candidates:
  - name: case_open
    source_location: ir:run:switch[0]
    target_branch: switch ctx->state case 1
    proven: true
    origin: ir
    candidate_facts:
      - kind: branch
        target: ctx->state
        relation: case
        value: "1"
branches:
  - source_location: ir:run:switch[0]
    target_branch: switch ctx->state case 1
    covered: true
"""
            )

            summary = load_coverage_summary(facts)

            self.assertEqual(summary.matched[0].candidate.name, "case_open")
            self.assertEqual(summary.matched[0].candidate.origin, "ir")
            self.assertEqual(summary.matched[0].candidate.facts, [
                {"kind": "branch", "target": "ctx->state", "relation": "case", "value": "1"},
            ])
            self.assertTrue(summary.matched[0].branch.covered)

    def test_writes_coverage_report_file(self):
        with TemporaryDirectory() as td:
            facts = Path(td) / "facts.yaml"
            out = Path(td) / "report.md"
            facts.write_text(
                """
candidates:
  - name: case_unproved
    source_location: ir:run:if[0]
    target_branch: if ctx->ready
    proven: false
    origin: regex
branches:
  - source_location: ir:run:if[0]
    target_branch: if ctx->ready
    covered: false
"""
            )

            write_coverage_report(facts, out)

            text = out.read_text()
            self.assertIn("case_unproved: coverage=uncovered proof=unproved origin=regex", text)
            self.assertIn("Coverage is report-only", text)

    def test_regex_retirement_ready_only_for_ir_proven_covered_candidates(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_ir",
                    "ir:run:if[0]",
                    "if ctx->ready",
                    proven=True,
                    origin="ir",
                )
            ],
            [
                BranchCoverage(
                    "ir:run:if[0]",
                    "if ctx->ready",
                    covered=True,
                )
            ],
        )

        decision = assess_regex_retirement(summary)

        self.assertTrue(decision.can_retire)
        self.assertEqual(decision.blockers, [])
        self.assertIn("status=ready", render_coverage_summary(summary))

    def test_regex_retirement_blocks_on_regex_unknown_and_uncovered(self):
        summary = summarize_candidate_coverage(
            [
                CandidateCoverage(
                    "case_regex",
                    "ir:run:if[0]",
                    "if ctx->ready",
                    proven=True,
                    origin="regex",
                ),
                CandidateCoverage(
                    "case_unknown",
                    "ir:run:if[1]",
                    "if ctx->other",
                    proven=True,
                ),
            ],
            [
                BranchCoverage(
                    "ir:run:if[0]",
                    "if ctx->ready",
                    covered=True,
                ),
                BranchCoverage(
                    "ir:run:if[2]",
                    "if ctx->closed",
                    covered=False,
                ),
            ],
        )

        decision = assess_regex_retirement(summary)

        self.assertFalse(decision.can_retire)
        self.assertIn("regex-origin candidates remain: case_regex", decision.blockers)
        self.assertIn("unknown-origin candidates remain: case_unknown", decision.blockers)
        self.assertIn("uncovered branches without candidates remain: 1", decision.blockers)


if __name__ == "__main__":
    unittest.main()
