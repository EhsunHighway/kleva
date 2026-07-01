import unittest

from kleva.ir.model import AssignmentStmt, DeclarationStmt, FieldAccess, VarRef
from kleva.shaping.ir_poststate import post_state_facts_from_direct_assignments


class IrPostStateTests(unittest.TestCase):
    def test_skips_witness_that_references_callee_local_rhs(self):
        facts = post_state_facts_from_direct_assignments([
            DeclarationStmt("new_events", "Event **", None),
            AssignmentStmt(
                FieldAccess(VarRef("eq", "EventQueue *"), "events"),
                VarRef("new_events"),
            ),
        ])

        self.assertEqual(facts, [])

    def test_keeps_visible_parameter_rhs(self):
        facts = post_state_facts_from_direct_assignments([
            AssignmentStmt(
                FieldAccess(VarRef("iface", "Interface *"), "link"),
                VarRef("link"),
            ),
        ])

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].target, "iface->link")
        self.assertEqual(facts[0].value, "link")


if __name__ == "__main__":
    unittest.main()
