import tempfile
import unittest
from pathlib import Path

from kleva.quality_report import (
    collect_quality,
    compare_generated_tests_by_api,
    render_generated_test_comparison,
    render_quality_report,
    write_quality_report,
)


class QualityReportTests(unittest.TestCase):
    def test_collects_generated_test_quality_from_unit_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unit = root / "test_packet_kleva.c"
            unit.write_text(
                """
static void test_one(void) {
    assert(out_ret == 0); /* EVA-proven oracle */
}
static void test_two(void) {
    assert(out_ok == 1); /* EVA-proven oracle */
    assert(out_extra == 1);
}
""",
                encoding="utf-8",
            )
            (root / "test_packet_kleva_unproved.c").write_text(
                """
static void test_diag(void) {
    /* EVA_UNPROVED: out_ret; assertion omitted for review. */
    assert(out_side == 1); /* EVA-proven oracle */
}
""",
                encoding="utf-8",
            )
            (root / "test_packet_kleva_unproved_report.md").write_text(
                "- packet_case: EVA_UNPROVED missing=out_ret reason=eva_imprecision\n",
                encoding="utf-8",
            )
            (root / "test_packet_kleva_summary.json").write_text(
                '{"recipes": 3, "skipped_candidates": 4, "duration_seconds": 1.25}\n',
                encoding="utf-8",
            )

            modules = collect_quality(root)

        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0].module, "packet")
        self.assertEqual(modules[0].trusted_tests, 2)
        self.assertEqual(modules[0].trusted_assertions, 3)
        self.assertEqual(modules[0].eva_proven_assertions, 2)
        self.assertEqual(modules[0].unproved_tests, 1)
        self.assertEqual(modules[0].unproved_assertions, 1)
        self.assertEqual(modules[0].unproved_items, 1)
        self.assertEqual(modules[0].skipped_candidates, 4)
        self.assertEqual(modules[0].recipes, 3)
        self.assertEqual(modules[0].runtime_seconds, 1.25)

    def test_renders_quality_report_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            unit = root / "test_event_kleva.c"
            unit.write_text(
                """
static void test_one(void) {
    assert(out_ret == 0); /* EVA-proven oracle */
}
""",
                encoding="utf-8",
            )

            report = render_quality_report(collect_quality(root))

        self.assertIn("| event | 1 | 1 | 1 | 0 | n/a | n/a |", report)
        self.assertIn("- trusted tests: 1", report)

    def test_writes_quality_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = root / "quality.md"
            (root / "test_scheduler_kleva.c").write_text(
                """
static void test_one(void) {
    assert(out_ret == -1); /* EVA-proven oracle */
}
""",
                encoding="utf-8",
            )

            write_quality_report(root, out)

            self.assertIn("| scheduler | 1 | 1 | 1 | 0 | n/a | n/a |", out.read_text())

    def test_compares_generated_tests_by_explicit_api_names(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "old.c"
            new = root / "new.c"
            old.write_text(
                """
static void test_packet_create_valid(void) {}
static void test_packet_free_null(void) {}
""",
                encoding="utf-8",
            )
            new.write_text(
                """
static void test_packet_create_valid(void) {}
static void test_packet_create_ir_diversity_size_zero(void) {}
static void test_packet_free_null(void) {}
""",
                encoding="utf-8",
            )

            comparisons = compare_generated_tests_by_api(
                old,
                new,
                ["packet_create", "packet_free"],
            )
            report = render_generated_test_comparison(comparisons)

        self.assertEqual(comparisons[0].api, "packet_create")
        self.assertEqual(comparisons[0].old_tests, 1)
        self.assertEqual(comparisons[0].new_tests, 2)
        self.assertEqual(comparisons[0].added, ("test_packet_create_ir_diversity_size_zero",))
        self.assertEqual(comparisons[1].added, ())
        self.assertIn("| packet_create | 1 | 2 | test_packet_create_ir_diversity_size_zero | none |", report)


if __name__ == "__main__":
    unittest.main()
