from __future__ import annotations

import unittest

from kleva.ir.model import (
    ArraySubscript,
    BinaryOp,
    FieldAccess,
    FunctionIR,
    IfStmt,
    LoopStmt,
    SourceLocation,
    VarRef,
)
from kleva.shaping.candidates import BranchFact
from kleva.shaping.ir_tables import lookup_loops_from_ir, table_candidates_from_ir


class IrTableShapingTests(unittest.TestCase):
    def test_detects_lookup_loop_from_array_field_comparison(self):
        func = FunctionIR(
            "lookup",
            [
                LoopStmt(
                    "for",
                    BinaryOp("<", VarRef("i"), VarRef("count")),
                    [
                        IfStmt(BinaryOp(
                            "==",
                            FieldAccess(ArraySubscript(FieldAccess(VarRef("table"), "items"), VarRef("i")), "key"),
                            VarRef("key"),
                        )),
                    ],
                    SourceLocation("sample.c", 11, 5),
                )
            ],
        )

        lookups = lookup_loops_from_ir(func)

        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0].array_expr, "table->items")
        self.assertEqual(lookups[0].field, "key")
        self.assertEqual(lookups[0].key_expr, "key")
        self.assertEqual(lookups[0].bound_expr, "count")

    def test_detects_lookup_loop_inside_nested_body(self):
        func = FunctionIR(
            "lookup",
            [
                IfStmt(
                    VarRef("enabled", "int"),
                    [
                        LoopStmt(
                            "for",
                            BinaryOp("<", VarRef("i"), VarRef("count")),
                            [
                                IfStmt(BinaryOp(
                                    "==",
                                    FieldAccess(ArraySubscript(VarRef("items"), VarRef("i")), "id"),
                                    VarRef("wanted"),
                                )),
                            ],
                            SourceLocation("sample.c", 21, 9),
                        )
                    ],
                )
            ],
        )

        lookups = lookup_loops_from_ir(func)

        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0].array_expr, "items")
        self.assertEqual(lookups[0].field, "id")
        self.assertEqual(lookups[0].loc, SourceLocation("sample.c", 21, 9))

    def test_generates_hit_and_miss_candidates(self):
        func = FunctionIR(
            "lookup",
            [
                LoopStmt(
                    "for",
                    BinaryOp("<", VarRef("i"), VarRef("count")),
                    [
                        IfStmt(BinaryOp(
                            "==",
                            FieldAccess(ArraySubscript(VarRef("items"), VarRef("i")), "id"),
                            VarRef("wanted"),
                        )),
                    ],
                    SourceLocation("sample.c", 11, 5),
                )
            ],
        )

        candidates = table_candidates_from_ir(func)

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_table_items_id_hit", ["items[0].id = wanted;"]),
                ("ir_table_items_id_miss", ["items[0].id = 0;"]),
                ("ir_table_items_id_full", ["count = 1;", "items[0].id = wanted;"]),
                ("ir_table_items_id_first_free", ["count = 1;", "items[0].id = 0;"]),
                (
                    "ir_table_items_id_duplicate",
                    ["count = 2;", "items[0].id = wanted;", "items[1].id = wanted;"],
                ),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:11:5")
        self.assertEqual(candidates[0].target_branch, "table items.id hit")
        self.assertEqual(candidates[0].branch_facts, [
            BranchFact("items[0].id", "==", "wanted"),
        ])
        self.assertEqual(candidates[1].branch_facts, [
            BranchFact("items[0].id", "!=", "wanted"),
        ])


if __name__ == "__main__":
    unittest.main()
