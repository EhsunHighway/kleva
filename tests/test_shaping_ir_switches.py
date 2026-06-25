import unittest

from kleva.ir.model import FieldAccess, FunctionIR, SwitchCase, SwitchStmt, VarRef
from kleva.shaping.ir_switches import state_switch_candidates_from_ir


class IrSwitchShapingTests(unittest.TestCase):
    def test_generates_state_switch_candidates_from_typed_ir(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx"), "state"),
                    [SwitchCase(1), SwitchCase(2)],
                )
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [
                ("ir_case_state_1", ["ctx->state = 1;"]),
                ("ir_case_state_2", ["ctx->state = 2;"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
