import unittest

from kleva.acsl import ACSLBehavior
from kleva.ast.model import CFunction
from kleva.yaml_emit import emit_fact_list, emit_output_list, emit_str_list, emit_yaml_function


class YamlEmitTests(unittest.TestCase):
    def test_emit_str_and_output_lists(self):
        self.assertEqual(emit_str_list([]), "[]")
        self.assertEqual(emit_output_list([]), "[]")
        self.assertEqual(emit_output_list(["out_ret", "out_ok"]), "[out_ret, out_ok]")
        self.assertIn('- "line with \\\\ slash"', emit_str_list(["line with \\ slash"]))
        self.assertIn('target: "ctx->state"', emit_fact_list([
            {"kind": "branch", "target": "ctx->state", "relation": "==", "value": "1"},
        ]))

    def test_emit_yaml_function_includes_candidate_and_headers(self):
        func = CFunction("widget_run", "int", "int", False, [])
        behavior = ACSLBehavior("valid", [], [], "", "")

        lines = emit_yaml_function(
            func,
            behavior,
            ["Widget state;", "out_ret = widget_run();"],
            ["out_ret"],
            ["cleanup();"],
            "klee_build/klee_out_widget_run_valid",
            source_include_names=["widget.h"],
            candidate=True,
            source_location="ir:widget_run:if[0]",
            target_branch="if state == 1",
            candidate_origin="ir",
            candidate_facts=[
                {"kind": "branch", "target": "state", "relation": "==", "value": "1"},
                {"kind": "call", "callee": "prepare", "mode": "equals_-1", "outcome": "success"},
                {"kind": "post_state", "target": "ctx->ready", "relation": "!=", "value": "0"},
            ],
        )

        text = "\n".join(lines)
        self.assertIn("# widget_run — behavior: valid", text)
        self.assertIn("name:      widget_run_valid", text)
        self.assertIn('#include \\"widget.h\\"', text)
        self.assertIn("candidate: true", text)
        self.assertIn('source_location: "ir:widget_run:if[0]"', text)
        self.assertIn('target_branch: "if state == 1"', text)
        self.assertIn('candidate_origin: "ir"', text)
        self.assertIn("candidate_facts:", text)
        self.assertIn("- kind: branch", text)
        self.assertIn('callee: "prepare"', text)
        self.assertIn("- kind: post_state", text)
        self.assertIn('target: "ctx->ready"', text)


if __name__ == "__main__":
    unittest.main()
