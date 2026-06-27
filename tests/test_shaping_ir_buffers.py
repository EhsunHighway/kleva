import unittest

from kleva.ir.model import BinaryOp, CallExpr, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, VarRef
from kleva.shaping.ir_buffers import len_data_buffer_params_from_ir, param_uses_len_data_buffer_from_ir


class IrBufferShapingTests(unittest.TestCase):
    def test_detects_parameter_with_len_and_data_field_uses(self):
        func = FunctionIR(
            "consume",
            [
                IfStmt(
                    BinaryOp(">", FieldAccess(VarRef("buf"), "len"), IntLiteral(0)),
                    [ExprStmt(CallExpr("use", [FieldAccess(VarRef("buf"), "data")]))],
                )
            ],
        )

        self.assertTrue(param_uses_len_data_buffer_from_ir(func, "buf"))
        self.assertEqual(len_data_buffer_params_from_ir(func, {"buf", "other"}), {"buf"})

    def test_requires_both_len_and_data_on_same_parameter(self):
        func = FunctionIR(
            "consume",
            [
                ExprStmt(CallExpr("use_len", [FieldAccess(VarRef("left"), "len")])),
                ExprStmt(CallExpr("use_data", [FieldAccess(VarRef("right"), "data")])),
            ],
        )

        self.assertEqual(len_data_buffer_params_from_ir(func, {"left", "right"}), set())

    def test_ignores_non_parameter_roots(self):
        func = FunctionIR(
            "consume",
            [
                ExprStmt(CallExpr("use_len", [FieldAccess(VarRef("local"), "len")])),
                ExprStmt(CallExpr("use_data", [FieldAccess(VarRef("local"), "data")])),
            ],
        )

        self.assertFalse(param_uses_len_data_buffer_from_ir(func, "buf"))


if __name__ == "__main__":
    unittest.main()
