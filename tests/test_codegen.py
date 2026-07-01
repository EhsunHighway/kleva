import tempfile
import unittest
from pathlib import Path

from kleva.codegen import write_probe_standalone, write_unit_tests
from kleva.eva import parse_eva_report
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
            self.assertIn("Reason category: implementation_bug", diagnostic)
            self.assertIn("KLEE status: ktest_available", diagnostic)
            self.assertIn("KLEE artifact: klee_build/klee_out_case/test000001.ktest", diagnostic)
            self.assertIn("Source location: ir:run:switch[0]", diagnostic)
            self.assertIn("Target branch: switch ctx->state case 1", diagnostic)
            self.assertIn("Candidate origin: ir", diagnostic)
            self.assertIn("Candidate facts: branch:ctx->state case 1", diagnostic)
            self.assertIn("case_tv001", report)
            self.assertIn("reason=implementation_bug", report)
            self.assertIn("klee_status=ktest_available", report)
            self.assertIn("klee_artifact=klee_build/klee_out_case/test000001.ktest", report)
            self.assertIn("source_location=ir:run:switch[0]", report)
            self.assertIn("target_branch=switch ctx->state case 1", report)
            self.assertIn("candidate_origin=ir", report)
            self.assertIn("candidate_facts=branch:ctx->state case 1", report)

    def test_emit_unproved_non_candidate_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="base_tv001",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=False,
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
            self.assertNotIn("test_base_tv001();", trusted)
            self.assertIn("EVA_UNPROVED", diagnostic)
            self.assertIn("base_tv001", report)

    def test_removes_stale_unproved_artifacts_when_all_outputs_prove(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            diagnostic_path = unit_path.with_name("test_mod_unproved.c")
            report_path = unit_path.with_name("test_mod_unproved_report.md")
            diagnostic_path.write_text("stale diagnostic")
            report_path.write_text("stale report")
            recipe = Recipe(
                fn_id="base_tv001",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=False,
            )

            proven, unproven, skipped = write_unit_tests(
                [recipe],
                {"probe_base_tv001": {"out_ret": 0}},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            self.assertEqual(proven, 1)
            self.assertEqual(unproven, 0)
            self.assertEqual(skipped, 0)
            self.assertFalse(diagnostic_path.exists())
            self.assertFalse(report_path.exists())

    def test_unproved_report_classifies_weak_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_fixture_gap",
                decl_lines=[],
                body_lines=[
                    "/* fixture-failed: unknown allocator for Thing *thing; using NULL */",
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

            self.assertIn("reason=weak_fixture", report)
            self.assertIn("Reason category: weak_fixture", diagnostic)

    def test_allocator_control_recipe_emits_native_compile_note(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="alloc_failure",
                decl_lines=[],
                body_lines=[
                    "__kleva_alloc_fail_on(0);",
                    "int out_ret = run_case();",
                ],
                cleanup=[],
                outputs=["out_ret"],
                preamble=[
                    "static void __kleva_alloc_fail_on(long index) {",
                    "    (void)index;",
                    "}",
                ],
            )

            write_unit_tests(
                [recipe],
                {"probe_alloc_failure": {"out_ret": -1}},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
            )

            text = unit_path.read_text()

            self.assertIn("KLEVA native compile note", text)
            self.assertIn("-Dmalloc=__kleva_malloc", text)
            self.assertIn("-Dfree=__kleva_free", text)

    def test_unproved_report_classifies_missing_acsl(self):
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

            self.assertIn("reason=missing_acsl", report)

    def test_unproved_report_classifies_weak_oracle(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_weak_oracle",
                decl_lines=[],
                body_lines=[
                    "run_case();",
                    "/* oracle-missing: void function has no return value or post-call witness */",
                    "int out_missing_oracle;",
                ],
                cleanup=[],
                outputs=["out_missing_oracle"],
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

            self.assertIn("reason=weak_oracle", report)
            self.assertIn("Reason category: weak_oracle", diagnostic)

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

    def test_unproved_report_uses_eva_parse_error_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_bad_probe",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=True,
            )
            eva_report = parse_eva_report(
                "[kernel] probe.c:7: User Error:\n"
                "  Return statement with a value in function returning void\n"
                "[kernel] Frama-C aborted: invalid user input.\n",
                raw_log_path="eva/logs/probe_case_bad_probe.txt",
            )

            write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
                eva_reports_by_probe={"probe_case_bad_probe": eva_report},
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()
            diagnostic = unit_path.with_name("test_mod_unproved.c").read_text()

            self.assertIn("reason=invalid_generated_c", report)
            self.assertIn("eva_log=eva/logs/probe_case_bad_probe.txt", report)
            self.assertIn("Reason category: invalid_generated_c", diagnostic)
            self.assertIn("EVA raw log: eva/logs/probe_case_bad_probe.txt", diagnostic)

    def test_unproved_report_uses_eva_non_singleton_output_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_nonsingleton",
                decl_lines=[],
                body_lines=["int out_ret = run_case();"],
                cleanup=[],
                outputs=["out_ret"],
                candidate=True,
            )
            eva_report = parse_eva_report(
                "[eva:final-states] Values at end of function probe_case_nonsingleton:\n"
                "  out_ret ∈ {0; 1}\n",
                raw_log_path="eva/logs/probe_case_nonsingleton.txt",
            )

            write_unit_tests(
                [recipe],
                {},
                str(unit_path),
                "mod.h",
                ts="now",
                emit_unproved="all",
                eva_reports_by_probe={"probe_case_nonsingleton": eva_report},
            )

            report = unit_path.with_name("test_mod_unproved_report.md").read_text()

            self.assertIn("reason=non_singleton_output", report)

    def test_unproved_report_classifies_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            unit_path = Path(td) / "test_mod.c"
            recipe = Recipe(
                fn_id="case_timeout",
                decl_lines=[],
                body_lines=[
                    "/* EVA timeout while checking this candidate */",
                    "int out_ret = run_case();",
                ],
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

            self.assertIn("reason=timeout", report)

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
