from types import SimpleNamespace
import unittest

from kleva.pipeline import _native_compile_macros_for_recipes, candidate_recipe_hint
from kleva.recipe import Recipe


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

    def test_allocator_control_recipes_report_native_compile_macros(self):
        recipe = Recipe(
            fn_id="alloc_failure",
            decl_lines=[],
            body_lines=["__kleva_alloc_fail_on(0);", "int out_ret = run_case();"],
            cleanup=[],
            outputs=["out_ret"],
        )

        self.assertEqual(
            _native_compile_macros_for_recipes([recipe]),
            [
                "malloc=__kleva_malloc",
                "calloc=__kleva_calloc",
                "realloc=__kleva_realloc",
                "free=__kleva_free",
            ],
        )


if __name__ == "__main__":
    unittest.main()
