import unittest

from kleva.fixtures.construction import safe_c_name
from kleva.ir.model import AddressOf, ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, DeclarationStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, LoopStmt, ReturnStmt, UnaryOp, VarRef
from kleva.shaping.candidates import BranchFact
from kleva.shaping.ir_conditions import IrConditionOps
from kleva.shaping.ir_lookups import fallback_lookup_candidates_from_ir


def _ops():
    return IrConditionOps(
        safe_c_name,
        lambda value: "1" if value == "0" else "0",
    )


class IrLookupShapingTests(unittest.TestCase):
    def test_generates_fallback_lookup_hit_from_helper_returned_array_slot(self):
        caller = FunctionIR(
            "handle",
            [
                DeclarationStmt(
                    "exact",
                    "Record *",
                    CallExpr("find_exact", [VarRef("table", "Table *"), VarRef("wanted", "int")], "Record *"),
                ),
                IfStmt(
                    BinaryOp(
                        "&&",
                        UnaryOp("!", VarRef("exact", "Record *")),
                        VarRef("allow_fallback", "int"),
                    ),
                    [
                        AssignmentStmt(
                            VarRef("fallback", "Record *"),
                            CallExpr("find_any", [VarRef("table", "Table *"), VarRef("wanted", "int")], "Record *"),
                        )
                    ],
                ),
                IfStmt(VarRef("fallback", "Record *"), [ReturnStmt(IntLiteral(1))]),
            ],
        )
        exact = _lookup_helper("find_exact", BinaryOp("==", FieldAccess(VarRef("slot", "Record *"), "id", "int"), VarRef("key", "int")))
        fallback = _lookup_helper("find_any", FieldAccess(VarRef("slot", "Record *"), "valid", "int"))

        candidates = fallback_lookup_candidates_from_ir(
            caller,
            _ops(),
            {"find_exact": exact, "find_any": fallback},
            {"find_exact": ("table", "key"), "find_any": ("table", "key")},
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "ir_fallback_lookup_fallback_1_1")
        self.assertEqual(candidates[0].setup, [
            "allow_fallback = 1;",
            "table->items[0].id = 0;",
            "table->items[0].valid = 1;",
        ])
        self.assertEqual(candidates[0].branch_facts, [
            BranchFact("table->items[0].valid", "!=", "0"),
        ])


def _lookup_helper(name, condition):
    return FunctionIR(
        name,
        [
            LoopStmt(
                "for",
                body=[
                    DeclarationStmt(
                        "slot",
                        "Record *",
                        AddressOf(
                            ArraySubscript(
                                FieldAccess(VarRef("table", "Table *"), "items", "Record[]"),
                                VarRef("i", "int"),
                                "Record",
                            ),
                            "Record *",
                        ),
                    ),
                    IfStmt(condition, [ReturnStmt(VarRef("slot", "Record *"))]),
                ],
            ),
            ReturnStmt(IntLiteral(0)),
        ],
    )


if __name__ == "__main__":
    unittest.main()
