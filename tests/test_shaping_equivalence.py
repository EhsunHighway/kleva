import unittest

from kleva.ast.model import CFunction, CParam
from kleva.fixtures.construction import safe_c_name
from kleva.ir.model import BinaryOp, FieldAccess, FunctionIR, IfStmt, IntLiteral, VarRef
from kleva.shaping.branches import source_branch_candidates
from kleva.shaping.ir_conditions import IrConditionOps, condition_candidates_from_ir

from tests.test_shaping_branches import _ops


def _param(name, raw_type, base_type):
    return CParam(name, raw_type, base_type, True, False, False, 0)


class ShapingEquivalenceTests(unittest.TestCase):
    def test_simple_field_equality_regex_and_ir_emit_same_setup(self):
        body = "if (ctx->state == 1) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [IfStmt(BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(1)))],
        )

        regex_candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"regex-fallbacks"},
            _ops(body),
        )
        ir_candidates = condition_candidates_from_ir(
            function_ir,
            IrConditionOps(safe_c_name, lambda value: "1" if value == "0" else "0"),
        )

        regex_setup = {
            tuple(candidate.setup)
            for candidate in regex_candidates
            if candidate.name == "source_ctx_state_eq_1"
        }
        ir_setup = {
            tuple(candidate.setup)
            for candidate in ir_candidates
            if candidate.name == "ir_if_0_ctx_state_eq_1"
        }

        self.assertEqual(regex_setup, {("ctx->state = 1;",)})
        self.assertEqual(regex_setup, ir_setup)


if __name__ == "__main__":
    unittest.main()
