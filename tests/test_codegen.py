import tempfile
import unittest
from pathlib import Path

from kleva.codegen import write_unit_tests
from kleva.recipe import Recipe


class CodegenTests(unittest.TestCase):
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
            self.assertIn("case_tv001", report)


if __name__ == "__main__":
    unittest.main()
