from types import SimpleNamespace
import unittest

from kleva.pipeline import candidate_recipe_hint


class PipelineTests(unittest.TestCase):
    def test_candidate_without_recipes_gets_klee_hint(self):
        spec = SimpleNamespace(
            candidate=True,
            ktest_dir="klee_build/klee_out_run_ir_if_0",
        )

        hint = candidate_recipe_hint(spec, 0)

        self.assertIn("candidate has no recipes", hint)
        self.assertIn("mode all/klee", hint)
        self.assertIn("klee_build/klee_out_run_ir_if_0", hint)

    def test_regular_function_without_recipes_gets_no_candidate_hint(self):
        spec = SimpleNamespace(candidate=False, ktest_dir="klee_build/klee_out_run")

        self.assertIsNone(candidate_recipe_hint(spec, 0))

    def test_candidate_with_recipes_gets_no_hint(self):
        spec = SimpleNamespace(candidate=True, ktest_dir="klee_build/klee_out_run")

        self.assertIsNone(candidate_recipe_hint(spec, 2))


if __name__ == "__main__":
    unittest.main()
