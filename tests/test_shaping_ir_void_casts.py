import unittest

from kleva.ir.model import CastExpr, DeclarationStmt, FunctionIR, VarRef
from kleva.shaping.ir_void_casts import void_param_cast_types_from_ir


class IrVoidCastShapingTests(unittest.TestCase):
    def test_detects_void_param_cast_declarations(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "ctx_typed",
                    "Context *",
                    CastExpr("Context *", VarRef("ctx"), "BitCast", "Context *"),
                )
            ],
        )

        self.assertEqual(void_param_cast_types_from_ir(func, {"ctx"}), {"ctx": "Context"})

    def test_ignores_casts_from_non_void_params(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "typed",
                    "Context *",
                    CastExpr("Context *", VarRef("other"), "BitCast", "Context *"),
                )
            ],
        )

        self.assertEqual(void_param_cast_types_from_ir(func, {"ctx"}), {})

    def test_accepts_struct_qualified_pointer_casts(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "typed",
                    "struct Context *",
                    CastExpr("const struct Context *", VarRef("ctx"), "BitCast", "const struct Context *"),
                )
            ],
        )

        self.assertEqual(void_param_cast_types_from_ir(func, {"ctx"}), {"ctx": "Context"})


if __name__ == "__main__":
    unittest.main()
