import unittest

from kleva.synth_config import SHAPING_FEATURES, normalize_shaping_features


class SynthConfigTests(unittest.TestCase):
    def test_normalize_shaping_features_defaults_to_all_features(self):
        self.assertEqual(normalize_shaping_features(), set(SHAPING_FEATURES))

    def test_normalize_shaping_features_accepts_enable_disable_flags(self):
        self.assertEqual(
            normalize_shaping_features(["state-switches,loop-tables"], ["loop-tables"]),
            {"state-switches"},
        )
        self.assertEqual(normalize_shaping_features(["none"], []), set())
        self.assertEqual(normalize_shaping_features(["all"], ["all"]), set())

    def test_normalize_shaping_features_rejects_unknown_names(self):
        with self.assertRaisesRegex(ValueError, "unknown shaping"):
            normalize_shaping_features(["imaginary-feature"], [])


if __name__ == "__main__":
    unittest.main()
