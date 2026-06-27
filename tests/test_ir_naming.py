from __future__ import annotations

import unittest

from kleva.ir.naming import safe_name


class IrNamingTests(unittest.TestCase):
    def test_preserves_identifier_characters(self):
        self.assertEqual(safe_name("abc_123"), "abc_123")

    def test_replaces_non_identifier_characters(self):
        self.assertEqual(safe_name("ctx->state == 1"), "ctx__state____1")

    def test_uses_fallback_for_empty_result(self):
        self.assertEqual(safe_name("!!!", "switch"), "switch")


if __name__ == "__main__":
    unittest.main()
