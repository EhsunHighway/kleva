from __future__ import annotations

import unittest

from kleva.ir.model import (
    AddressOf,
    ArraySubscript,
    BinaryOp,
    CallExpr,
    CastExpr,
    Dereference,
    FieldAccess,
    IntLiteral,
    UnaryOp,
    VarRef,
)
from kleva.ir.render import assignable_expr, is_pointer_expr, value_expr


class IrRenderTests(unittest.TestCase):
    def test_renders_assignable_nested_field_and_array(self):
        expr = FieldAccess(
            ArraySubscript(FieldAccess(VarRef("table"), "items"), VarRef("i")),
            "state",
        )

        self.assertEqual(assignable_expr(expr), "table->items[i]->state")

    def test_renders_embedded_struct_field_with_dot(self):
        expr = FieldAccess(
            FieldAccess(VarRef("host", "Host *"), "base", "Device"),
            "interfaces",
            "Interface **",
        )

        self.assertEqual(assignable_expr(expr), "host->base.interfaces")

    def test_renders_assignable_cast_and_dereference(self):
        self.assertEqual(
            assignable_expr(FieldAccess(CastExpr("Header *", VarRef("raw")), "type")),
            "((Header *)raw)->type",
        )
        self.assertEqual(
            assignable_expr(FieldAccess(CastExpr("Header", VarRef("raw")), "type")),
            "((Header)raw).type",
        )
        self.assertEqual(assignable_expr(Dereference(VarRef("ptr"))), "*ptr")

    def test_casted_computed_value_is_not_assignable(self):
        expr = CastExpr(
            "size_t",
            BinaryOp(
                "-",
                FieldAccess(VarRef("p", "Packet *"), "data", "uint8_t *"),
                FieldAccess(VarRef("p", "Packet *"), "head", "uint8_t *"),
                "long",
            ),
            c_type="size_t",
        )

        self.assertIsNone(assignable_expr(expr))
        self.assertEqual(value_expr(expr), "((size_t)(p->data - p->head))")

    def test_renders_value_forms(self):
        self.assertEqual(value_expr(IntLiteral(7)), "7")
        self.assertEqual(value_expr(AddressOf(FieldAccess(VarRef("ctx"), "slot"))), "&ctx->slot")
        self.assertEqual(value_expr(UnaryOp("!", VarRef("ready"))), "!ready")
        self.assertEqual(value_expr(BinaryOp("+", VarRef("len"), IntLiteral(1))), "(len + 1)")
        self.assertEqual(value_expr(CallExpr("ok", [VarRef("ctx"), IntLiteral(2)])), "ok(ctx, 2)")

    def test_detects_pointer_expr_from_type(self):
        self.assertTrue(is_pointer_expr(VarRef("node", "Node *")))
        self.assertFalse(is_pointer_expr(VarRef("count", "size_t")))


if __name__ == "__main__":
    unittest.main()
