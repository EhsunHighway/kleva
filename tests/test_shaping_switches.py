import unittest

from kleva.ast.model import CTypeCatalog
from kleva.shaping.lookups import LookupShape
from kleva.shaping.switches import StateSwitchOps, state_switch_candidates, switch_case_blocks


class SwitchShapingTests(unittest.TestCase):
    def test_switch_case_blocks_extracts_cases_without_default_tail(self):
        body = """
            switch (item->state) {
                case INIT:
                    if (ready) return 1;
                    break;
                case DONE:
                    return 2;
                default:
                    return -1;
            }
        """
        start = body.index("switch")
        cases = switch_case_blocks(body, start + len("switch"))

        self.assertEqual([case for case, _block in cases], ["INIT", "DONE"])
        self.assertIn("ready", cases[0][1])
        self.assertNotIn("default", cases[1][1])

    def test_state_switch_candidates_are_generic_over_lookup_result_field(self):
        shape = LookupShape(
            callee="find_item",
            result_var="item",
            element_type="Item",
            element_alias="slot",
            container_type="Table",
            container_expr="table",
            array_field="items",
            param_args={"id": "wanted"},
            conditions=[],
        )
        body = """
            Item *item = find_item(table, wanted);
            switch (item->state) {
                case INIT:
                    if (item->ready) return 1;
                    break;
                case DONE:
                    return 2;
            }
        """
        ops = StateSwitchOps(
            lambda *_args: [shape],
            lambda *_args: ["base_ready = 1;"],
            lambda *_args: [],
            lambda *_args: ["Table owner;", "table = &owner;"],
            lambda *_args: ["table->items[0].valid = 1;"],
            lambda expr, _aliases: expr,
            lambda cond, *_args: [f"/* shaped {cond.strip()} */"],
            lambda *_args: ([], []),
            lambda *_args: ([], []),
            lambda line, *_args: line,
            lambda name: name,
        )

        candidates = state_switch_candidates(
            body,
            "source",
            {},
            {},
            {},
            {},
            CTypeCatalog(),
            {"state-switches"},
            ops,
        )

        names = [candidate.name for candidate in candidates]
        self.assertIn("source_item_state_INIT", names)
        self.assertIn("source_item_state_DONE", names)
        self.assertIn("source_item_state_INIT_guard_1", names)
        init = next(candidate for candidate in candidates if candidate.name == "source_item_state_INIT")
        self.assertIn("table->items[0].state = INIT;", init.setup)


if __name__ == "__main__":
    unittest.main()
