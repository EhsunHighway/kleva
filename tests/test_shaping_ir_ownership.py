from __future__ import annotations

import unittest

from kleva.ir.model import AssignmentStmt, ArraySubscript, CallExpr, CastExpr, DeclarationStmt, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, ReturnStmt, SwitchStmt, UnaryOp, VarRef
from kleva.shaping.ir_ownership import (
    BORROWED,
    CONSUMED,
    OwnershipFact,
    TRANSFERRED,
    classify_ownership_from_ir,
    consumed_params_from_ir,
    ownership_facts_from_ir,
    returns_owned_pointer_from_ir,
    transferred_params_from_ir,
)


class IrOwnershipShapingTests(unittest.TestCase):
    def test_detects_direct_free_of_parameter(self):
        func = FunctionIR(
            "release",
            [ExprStmt(CallExpr("free", [VarRef("item")]))],
        )

        self.assertEqual(consumed_params_from_ir(func, {"item"}), {"item"})

    def test_detects_nested_free_of_parameter(self):
        func = FunctionIR(
            "release",
            [
                IfStmt(
                    VarRef("ready", "int"),
                    [ExprStmt(CallExpr("free", [VarRef("item")]))],
                )
            ],
        )

        self.assertEqual(consumed_params_from_ir(func, {"item"}), {"item"})

    def test_detects_generic_destructor_names(self):
        func = FunctionIR(
            "release",
            [ExprStmt(CallExpr("record_destroy", [VarRef("record")]))],
        )

        self.assertEqual(consumed_params_from_ir(func, {"record"}), {"record"})

    def test_ignores_non_parameter_arguments(self):
        func = FunctionIR(
            "release",
            [ExprStmt(CallExpr("free", [VarRef("local")]))],
        )

        self.assertEqual(consumed_params_from_ir(func, {"item"}), set())

    def test_accepts_explicit_consuming_callee_set(self):
        func = FunctionIR(
            "submit",
            [ExprStmt(CallExpr("take_ownership", [VarRef("item")]))],
        )

        self.assertEqual(
            consumed_params_from_ir(func, {"item"}, {"take_ownership"}),
            {"item"},
        )

    def test_detects_parameter_stored_into_owner_field(self):
        func = FunctionIR(
            "store",
            [AssignmentStmt(FieldAccess(VarRef("owner"), "slot"), VarRef("item"))],
        )

        self.assertEqual(transferred_params_from_ir(func, {"item"}), {"item"})

    def test_detects_nested_parameter_transfer(self):
        func = FunctionIR(
            "store",
            [
                SwitchStmt(
                    VarRef("state", "int"),
                    body=[
                        AssignmentStmt(FieldAccess(VarRef("owner"), "slot"), VarRef("item"))
                    ],
                )
            ],
        )

        self.assertEqual(transferred_params_from_ir(func, {"item"}), {"item"})

    def test_ignores_assignment_of_non_parameter_value(self):
        func = FunctionIR(
            "store",
            [AssignmentStmt(FieldAccess(VarRef("owner"), "slot"), VarRef("local"))],
        )

        self.assertEqual(transferred_params_from_ir(func, {"item"}), set())

    def test_detects_parameter_stored_into_array_slot(self):
        func = FunctionIR(
            "store",
            [AssignmentStmt(ArraySubscript(VarRef("items"), IntLiteral(0)), VarRef("item"))],
        )

        self.assertEqual(transferred_params_from_ir(func, {"item"}), {"item"})

    def test_detects_parameter_stored_into_struct_array_field(self):
        func = FunctionIR(
            "store",
            [
                AssignmentStmt(
                    FieldAccess(
                        ArraySubscript(
                            FieldAccess(VarRef("owner"), "items"),
                            IntLiteral(0),
                        ),
                        "payload",
                    ),
                    VarRef("item"),
                )
            ],
        )

        self.assertEqual(transferred_params_from_ir(func, {"item"}), {"item"})
        self.assertEqual(ownership_facts_from_ir(func, {"item"}), [
            OwnershipFact("item", TRANSFERRED, "owner->items[]->payload"),
        ])

    def test_classifies_pointer_parameter_behavior(self):
        func = FunctionIR(
            "update",
            [
                IfStmt(UnaryOp("!", VarRef("borrowed")), [ReturnStmt(IntLiteral(-1))]),
                AssignmentStmt(FieldAccess(VarRef("owner"), "slot"), VarRef("stored")),
                ExprStmt(CallExpr("free", [VarRef("done")])),
                ExprStmt(CallExpr("read", [FieldAccess(VarRef("buffer"), "len")])),
                ExprStmt(CallExpr("read", [FieldAccess(VarRef("buffer"), "data")])),
                DeclarationStmt(
                    "typed",
                    "Context *",
                    CastExpr("Context *", VarRef("ctx"), "BitCast", "Context *"),
                ),
            ],
        )

        summary = classify_ownership_from_ir(
            func,
            {"borrowed", "stored", "done", "buffer", "ctx"},
            void_param_names={"ctx"},
        )

        self.assertEqual(summary.param_behavior["borrowed"], BORROWED)
        self.assertEqual(summary.param_behavior["stored"], TRANSFERRED)
        self.assertEqual(summary.param_behavior["done"], CONSUMED)
        self.assertEqual(summary.param_behavior["buffer"], BORROWED)
        self.assertEqual(summary.param_behavior["ctx"], BORROWED)
        self.assertFalse(summary.returns_owned_pointer)
        self.assertEqual(summary.nullable_params, {"borrowed"})
        self.assertEqual(summary.buffer_params, {"buffer"})
        self.assertEqual(summary.void_cast_types, {"ctx": "Context"})

    def test_reports_typed_ownership_facts(self):
        func = FunctionIR(
            "update",
            [
                AssignmentStmt(FieldAccess(VarRef("owner"), "slot"), VarRef("stored")),
                AssignmentStmt(ArraySubscript(VarRef("items"), IntLiteral(0)), VarRef("queued")),
                ExprStmt(CallExpr("free", [VarRef("done")])),
            ],
        )

        self.assertEqual(ownership_facts_from_ir(func, {"stored", "queued", "done"}), [
            OwnershipFact("stored", TRANSFERRED, "owner->slot"),
            OwnershipFact("queued", TRANSFERRED, "items[]"),
            OwnershipFact("done", CONSUMED, "free"),
        ])

    def test_propagates_helper_transfer_to_caller_parameter(self):
        caller = FunctionIR(
            "schedule",
            [ReturnStmt(CallExpr("queue_push", [FieldAccess(VarRef("s"), "queue"), VarRef("event")]))],
        )

        summary = classify_ownership_from_ir(
            caller,
            {"s", "event"},
            helper_ownership={"queue_push": {1: TRANSFERRED}},
        )

        self.assertEqual(summary.param_behavior["event"], TRANSFERRED)
        self.assertEqual(summary.param_behavior["s"], BORROWED)

    def test_detects_returned_owned_pointer_from_allocation_call(self):
        func = FunctionIR(
            "make",
            [ReturnStmt(CallExpr("malloc", [IntLiteral(8)]))],
        )

        self.assertTrue(returns_owned_pointer_from_ir(func))
        self.assertTrue(classify_ownership_from_ir(func, set()).returns_owned_pointer)

    def test_detects_nested_returned_owned_pointer(self):
        func = FunctionIR(
            "make",
            [
                IfStmt(
                    VarRef("need", "int"),
                    [ReturnStmt(CallExpr("malloc", [IntLiteral(8)]))],
                )
            ],
        )

        self.assertTrue(returns_owned_pointer_from_ir(func))

    def test_accepts_explicit_allocation_callee_set(self):
        func = FunctionIR(
            "make",
            [ReturnStmt(CallExpr("make_object", []))],
        )

        self.assertTrue(returns_owned_pointer_from_ir(func, {"make_object"}))


if __name__ == "__main__":
    unittest.main()
