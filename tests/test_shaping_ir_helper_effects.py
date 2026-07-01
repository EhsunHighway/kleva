from __future__ import annotations

import unittest

from kleva.ir.model import ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, ReturnStmt, VarRef
from kleva.shaping.candidates import OwnershipPathFact, PostStateFact
from kleva.shaping.ir_helper_effects import HelperSideEffect, helper_effect_summary


class IrHelperEffectSummaryTests(unittest.TestCase):
    def test_infers_success_and_failure_fixtures_from_guard(self):
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    BinaryOp("==", FieldAccess(VarRef("item"), "ready"), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        summary = helper_effect_summary(
            helper,
            ("item",),
            ["ctx->slot"],
            "equals_-1",
            "prepare",
        )

        self.assertEqual(summary.failure_setup, ("ctx->slot->ready = 0;",))
        self.assertEqual(summary.success_setup, ("ctx->slot->ready = 1;",))

    def test_infers_field_slot_call_and_ownership_effects(self):
        helper = FunctionIR(
            "attach",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("owner"), "slot"),
                    VarRef("item"),
                ),
                AssignmentStmt(
                    FieldAccess(VarRef("owner"), "ready"),
                    IntLiteral(1),
                ),
                ExprStmt(CallExpr("schedule", [VarRef("owner")])),
                ExprStmt(CallExpr("free", [VarRef("tmp")])),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        summary = helper_effect_summary(
            helper,
            ("owner", "item", "tmp"),
            ["queue", "node", "scratch"],
            "equals_-1",
            "attach",
        )

        self.assertIn(PostStateFact("queue->slot", "==", "node"), summary.post_state)
        self.assertIn(PostStateFact("queue->ready", "==", "1"), summary.post_state)
        self.assertIn(
            OwnershipPathFact("node", "transferred", "attach:owner->slot"),
            summary.ownership,
        )
        self.assertIn(
            OwnershipPathFact("scratch", "consumed", "attach:free"),
            summary.ownership,
        )
        self.assertIn(
            HelperSideEffect("field-changed", "queue->ready", "1", "assignment"),
            summary.side_effects,
        )
        self.assertIn(
            HelperSideEffect("call", "schedule", "queue", "call"),
            summary.side_effects,
        )

    def test_classifies_array_slot_assignment_as_slot_filled(self):
        helper = FunctionIR(
            "insert",
            [
                AssignmentStmt(
                    ArraySubscript(FieldAccess(VarRef("queue"), "items"), IntLiteral(0)),
                    VarRef("item"),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        summary = helper_effect_summary(
            helper,
            ("queue", "item"),
            ["ctx->queue", "node"],
            "equals_-1",
            "insert",
        )

        self.assertIn(
            HelperSideEffect("array-slot-filled", "ctx->queue->items[0]", "node", "assignment"),
            summary.side_effects,
        )


if __name__ == "__main__":
    unittest.main()
