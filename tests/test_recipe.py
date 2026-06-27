import unittest

from kleva.recipe import expand_guard


class RecipeTests(unittest.TestCase):
    def test_probe_guard_expands_to_assumption(self):
        self.assertEqual(
            expand_guard("__GUARD__(ptr)", is_probe=True),
            "Frama_C_assume(ptr != 0);",
        )
        self.assertEqual(
            expand_guard("__GUARD__(ret == 0)", is_probe=True),
            "Frama_C_assume(ret == 0);",
        )

    def test_unit_and_klee_guards_keep_runtime_checks(self):
        self.assertEqual(
            expand_guard("__GUARD__(ptr)", is_probe=False),
            "assert(ptr != NULL);",
        )
        self.assertEqual(
            expand_guard("__GUARD__(ptr)", is_probe=True, is_klee=True),
            "if (!ptr) return 0;",
        )

    def test_probe_cleanup_guard_expands_to_assumption(self):
        self.assertEqual(
            expand_guard("__GUARD_WITH_CLEANUP__(ptr, cleanup(ptr);)", is_probe=True),
            "Frama_C_assume(ptr != 0);",
        )


if __name__ == "__main__":
    unittest.main()
