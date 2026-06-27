import tempfile
import unittest
from pathlib import Path

from kleva.codegen import write_probe_standalone, write_unit_tests
from kleva.recipe import Recipe


class CodegenTests(unittest.TestCase):
    def test_probe_drops_local_typedefs(self):
        with tempfile.TemporaryDirectory() as td:
            probe_path = Path(td) / "probe.c"
            recipe = Recipe(
                fn_id="case_tv001",
                decl_lines=[],
                body_lines=[
                    "typedef struct Widget Widget;",
                    "Widget *a = make_widget();",
                    "typedef struct Widget Widget;",
                    "Widget *b = make_widget();",
                ],
                cleanup=[],
                outputs=[],
            )

            write_probe_standalone(recipe, str(probe_path), "mod.h", ts="now")

            text = probe_path.read_text()
            self.assertNotIn("typedef struct Widget Widget;", text)
            self.assertIn("Widget *a = make_widget();", text)
            self.assertIn("Widget *b = make_widget();", text)

    def test_emit_unproved_candidate_tests_separately(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_tv001",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=True,
                ktest_path="klee_build/klee_out_case/test000001.ktest",
                source_location="ir:run:switch[0]",
                target_branch="switch ctx->state case 1",
                candidate_origin="ir",
                candidate_facts=[
                    {"kind": "branch", "target": "ctx->state", "relation": "case", "value": "1"},
                ],
            )

            proven, unproven, skipped = write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            trusted = unit_path.read_text()
            diagnostic = unit_path.with_name("test_mod_unproved.c").read_text()
            report = unit_path.with_name("test_mod_unproved_report.md").read_text()

            self.assertEqual(proven, 0)
            self.assertEqual(unproven, 1)
            self.assertEqual(skipped, 0)
            self.assertNotIn("test_case_tv001();", trusted)
            self.assertIn("EVA_UNPROVED", diagnostic)
            self.assertIn("assertion omitted", diagnostic)
            self.assertIn("Reason category: possible_implementation_bug", diagnostic)
            self.assertIn("KLEE status: ktest_available", diagnostic)
            self.assertIn("KLEE artifact: klee_build/klee_out_case/test000001.ktest", diagnostic)
            self.assertIn("Source location: ir:run:switch[0]", diagnostic)
            self.assertIn("Target branch: switch ctx->state case 1", diagnostic)
            self.assertIn("Candidate origin: ir", diagnostic)
            self.assertIn("Candidate facts: branch:ctx->state case 1", diagnostic)
            self.assertIn("case_tv001", report)
            self.assertIn("reason=possible_implementation_bug", report)
            self.assertIn("klee_status=ktest_available", report)
            self.assertIn("klee_artifact=klee_build/klee_out_case/test000001.ktest", report)
            self.assertIn("source_location=ir:run:switch[0]", report)
            self.assertIn("target_branch=switch ctx->state case 1", report)
            self.assertIn("candidate_origin=ir", report)
            self.assertIn("candidate_facts=branch:ctx->state case 1", report)

    def test_unproved_report_classifies_fixture_gap(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_fixture_gap",
                decl_lines=[],
                body_lines=[
                    "/* kleva synth: no visible allocation strategy for Thing *thing; using NULL */",
                    "int out_status = run_case();",
                ],
                cleanup=[],
                outputs=["out_status"],
                candidate=True,
            )

            write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()
            diagnostic = unit_path.with_name("test_mod_unproved.c").read_text()

            self.assertIn("reason=fixture_gap", report)
            self.assertIn("Reason category: fixture_gap", diagnostic)

    def test_unproved_report_classifies_missing_observable(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_missing_output",
                decl_lines=[],
                body_lines=["run_case();"],
                cleanup=[],
                outputs=["out_value"],
                candidate=True,
            )

            write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()

            self.assertIn("reason=missing_contract_or_observable", report)

    def test_unproved_report_classifies_partial_eva_precision_gap(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_partial",
                decl_lines=[],
                body_lines=[
                    "int out_a = run_a();",
                    "int out_b = run_b();",
                ],
                cleanup=[],
                outputs=["out_a", "out_b"],
                candidate=True,
            )

            write_unit_tests(
                [recipe],
                {"probe_case_partial": {"out_a": 1}},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()

            self.assertIn("reason=eva_imprecision", report)

    def test_unproved_report_marks_missing_ktest_status(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_without_ktest",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=True,
            )

            write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()
            diagnostic = unit_path.with_name("test_mod_unproved.c").read_text()

            self.assertIn("klee_status=not_recorded", report)
            self.assertIn("KLEE status: not_recorded", diagnostic)
            self.assertIn("source_location=not_recorded", report)
            self.assertIn("Source location: not_recorded", diagnostic)
            self.assertIn("candidate_origin=not_recorded", report)


if __name__ == "__main__":
    unittest.main()
