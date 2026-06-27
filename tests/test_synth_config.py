import unittest
import tempfile
from pathlib import Path

from kleva.synth_config import DEFAULT_SHAPING_FEATURES, SHAPING_FEATURES, load_helper_call_rules, normalize_shaping_features


class SynthConfigTests(unittest.TestCase):
    def test_normalize_shaping_features_defaults_to_ast_ir_features(self):
        self.assertEqual(normalize_shaping_features(), set(DEFAULT_SHAPING_FEATURES))
        self.assertNotIn("regex-fallbacks", normalize_shaping_features())

    def test_normalize_shaping_features_accepts_enable_disable_flags(self):
        self.assertEqual(
            normalize_shaping_features(["state-switches,loop-tables"], ["loop-tables"]),
            {"state-switches"},
        )
        self.assertEqual(normalize_shaping_features(["none"], []), set())
        self.assertEqual(normalize_shaping_features(["all"], ["all"]), set())
        self.assertIn("regex-fallbacks", normalize_shaping_features(["all"], []))
        self.assertNotIn("regex-fallbacks", normalize_shaping_features(None, ["regex-fallbacks"]))

    def test_normalize_shaping_features_rejects_unknown_names(self):
        with self.assertRaisesRegex(ValueError, "unknown shaping"):
            normalize_shaping_features(["imaginary-feature"], [])

    def test_load_helper_call_rules_from_yaml_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            path.write_text(
                """
helper_call_rules:
  - callee: validate
    success_setup:
      - "{arg0}->status = 0;"
    failure_setup:
      - "{arg0}->status = 1;"
""",
                encoding="utf-8",
            )

            rules = load_helper_call_rules([str(path)])

        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].callee, "validate")
        self.assertEqual(rules[0].success_setup, ("{arg0}->status = 0;",))
        self.assertEqual(rules[0].failure_setup, ("{arg0}->status = 1;",))

    def test_load_helper_call_rules_rejects_bad_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.yaml"
            path.write_text(
                """
helper_call_rules:
  - success_setup:
      - "missing callee"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "callee"):
                load_helper_call_rules([str(path)])


if __name__ == "__main__":
    unittest.main()
