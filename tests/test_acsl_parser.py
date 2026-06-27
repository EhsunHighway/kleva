import tempfile
import unittest
from pathlib import Path

from kleva.acsl import ACSLBehavior, ACSLSpec, RegexAcslParser, ScannerAcslParser, parse_acsl_from_text
from kleva.synth_generate import generate_yaml_from_header


HEADER = r"""
/*@
    behavior null:
        assumes p == \null;
        assigns \nothing;
        ensures \result == -1;

    behavior valid:
        assumes \valid(p);
        assigns *p;
        ensures \result == 0;
*/
int demo(int *p);
"""


class AcslParserTests(unittest.TestCase):
    def test_scanner_parser_implements_parser_boundary(self):
        parser = ScannerAcslParser()

        specs = parser.parse_text(HEADER)

        self.assertEqual(parser.name, "scanner")
        self.assertIn("demo", specs)
        self.assertEqual([b.name for b in specs["demo"].behaviors], ["null", "valid"])
        self.assertEqual(specs["demo"].behaviors[0].ensures, [r"\result == -1"])

    def test_regex_parser_name_is_backward_compatible_alias(self):
        parser = RegexAcslParser()

        specs = parser.parse_text(HEADER)

        self.assertEqual(parser.name, "scanner")
        self.assertIn("demo", specs)

    def test_public_text_parser_uses_same_contract(self):
        specs = parse_acsl_from_text(HEADER)

        self.assertIn("demo", specs)
        self.assertEqual(specs["demo"].behaviors[1].assigns, "*p")

    def test_scanner_parser_handles_multiline_declaration_and_requires(self):
        header = r"""
/*@
    requires p != \null &&
             count > 0;
    assigns *p;
    ensures \result == 0;
*/
int multiline_demo(
    int *p,
    int count
);
"""

        specs = ScannerAcslParser().parse_text(header)

        self.assertIn("multiline_demo", specs)
        behavior = specs["multiline_demo"].behaviors[0]
        self.assertEqual(behavior.name, "valid")
        self.assertEqual(behavior.assumes, [r"p != \null && count > 0"])
        self.assertEqual(behavior.assigns, "*p")
        self.assertEqual(behavior.ensures, [r"\result == 0"])

    def test_synthesis_accepts_injected_acsl_parser(self):
        class FakeParser:
            name = "fake"

            def parse_text(self, _header_text):
                return {}

            def parse_file(self, _header_path):
                return {
                    "demo": ACSLSpec("demo", [
                        ACSLBehavior(
                            name="null",
                            assumes=[r"p == \null"],
                            ensures=[r"\result == -1"],
                        )
                    ])
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "demo.h"
            source = root / "demo.c"
            header.write_text("int demo(int *p);\n", encoding="utf-8")
            source.write_text(
                """
#include "demo.h"
int demo(int *p) {
    if (!p) return -1;
    return 0;
}
""",
                encoding="utf-8",
            )

            yaml_text = generate_yaml_from_header(
                str(header),
                source_path=str(source),
                include_dir=str(root),
                acsl_parser=FakeParser(),
            )

        self.assertIn("demo_null", yaml_text)
        self.assertIn("int out_ret = demo(NULL);", yaml_text)


if __name__ == "__main__":
    unittest.main()
