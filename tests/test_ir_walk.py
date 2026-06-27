from __future__ import annotations

import unittest

from kleva.ir.model import ExprStmt, FunctionIR, IfStmt, LoopStmt, ReturnStmt, SwitchStmt, VarRef
from kleva.ir.walk import body_has_return, walk_if_statements, walk_statements


class IrWalkTests(unittest.TestCase):
    def test_walks_nested_control_flow_bodies(self):
        nested_call = ExprStmt(VarRef("leaf", "int"))
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    VarRef("a", "int"),
                    [
                        LoopStmt(
                            "while",
                            VarRef("b", "int"),
                            [
                                SwitchStmt(
                                    VarRef("c", "int"),
                                    body=[nested_call],
                                )
                            ],
                        )
                    ],
                )
            ],
        )

        walked = list(walk_statements(func))

        self.assertIs(walked[-1], nested_call)
        self.assertEqual(len(walked), 4)

    def test_walk_if_statements_returns_nested_ifs_only(self):
        outer_if = IfStmt(VarRef("outer", "int"))
        switch_if = IfStmt(VarRef("inside_switch", "int"))
        loop_if = IfStmt(VarRef("inside_loop", "int"))
        func = FunctionIR(
            "run",
            [
                outer_if,
                SwitchStmt(VarRef("state", "int"), body=[switch_if]),
                LoopStmt("while", VarRef("keep", "int"), body=[loop_if]),
            ],
        )

        self.assertEqual(list(walk_if_statements(func)), [outer_if, switch_if, loop_if])

    def test_body_has_return_checks_direct_body_statements(self):
        self.assertTrue(body_has_return([ExprStmt(VarRef("x")), ReturnStmt()]))
        self.assertFalse(body_has_return([ExprStmt(VarRef("x"))]))


if __name__ == "__main__":
    unittest.main()
