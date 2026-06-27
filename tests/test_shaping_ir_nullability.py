from __future__ import annotations

import unittest

from kleva.ir.model import BinaryOp, FunctionIR, IfStmt, IntLiteral, ReturnStmt, UnaryOp, VarRef
from kleva.shaping.ir_nullability import accepts_null_param_from_ir


class IrNullabilityTests(unittest.TestCase):
    def test_detects_unary_null_guard_with_return(self):
        func = FunctionIR(
            "maybe",
            [IfStmt(UnaryOp("!", VarRef("item")), [ReturnStmt(IntLiteral(-1))])],
        )

        self.assertTrue(accepts_null_param_from_ir(func, "item"))

    def test_detects_compound_null_guard_with_return(self):
        func = FunctionIR(
            "maybe",
            [IfStmt(
                BinaryOp("||", UnaryOp("!", VarRef("ctx")), UnaryOp("!", VarRef("item"))),
                [ReturnStmt(IntLiteral(-1))],
            )],
        )

        self.assertTrue(accepts_null_param_from_ir(func, "ctx"))
        self.assertTrue(accepts_null_param_from_ir(func, "item"))

    def test_detects_zero_comparison_guard(self):
        func = FunctionIR(
            "maybe",
            [
                IfStmt(BinaryOp("==", VarRef("item"), IntLiteral(0)), [ReturnStmt()]),
                IfStmt(BinaryOp("==", IntLiteral(0), VarRef("other")), [ReturnStmt()]),
            ],
        )

        self.assertTrue(accepts_null_param_from_ir(func, "item"))
        self.assertTrue(accepts_null_param_from_ir(func, "other"))

    def test_ignores_guard_without_return_body(self):
        func = FunctionIR(
            "maybe",
            [IfStmt(UnaryOp("!", VarRef("item")), [])],
        )

        self.assertFalse(accepts_null_param_from_ir(func, "item"))

    def test_ignores_other_parameters(self):
        func = FunctionIR(
            "maybe",
            [IfStmt(UnaryOp("!", VarRef("other")), [ReturnStmt()])],
        )

        self.assertFalse(accepts_null_param_from_ir(func, "item"))


if __name__ == "__main__":
    unittest.main()
