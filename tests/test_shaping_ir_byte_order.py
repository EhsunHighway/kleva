from __future__ import annotations

import unittest

from kleva.ir.model import AssignmentStmt, CallExpr, CastExpr, DeclarationStmt, FieldAccess, FunctionIR, IfStmt, VarRef
from kleva.shaping.ir_byte_order import DecodedFieldAlias, decoded_field_aliases_from_ir


class IrByteOrderShapingTests(unittest.TestCase):
    def test_detects_decoded_field_declaration(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt(
                    "port",
                    "uint16_t",
                    CallExpr("ns_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "port", "uint16_t")]),
                )
            ],
        )

        self.assertEqual(
            decoded_field_aliases_from_ir(func),
            {"port": DecodedFieldAlias("ns_ntohs", "hdr->port")},
        )

    def test_propagates_copied_decoded_locals(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt(
                    "port",
                    "uint16_t",
                    CallExpr("project_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "port", "uint16_t")]),
                ),
                DeclarationStmt("same", "uint16_t", VarRef("port", "uint16_t")),
                AssignmentStmt(VarRef("alias", "uint16_t"), VarRef("same", "uint16_t")),
            ],
        )

        aliases = decoded_field_aliases_from_ir(func)

        self.assertEqual(aliases["port"], DecodedFieldAlias("project_ntohs", "hdr->port"))
        self.assertEqual(aliases["same"], DecodedFieldAlias("project_ntohs", "hdr->port"))
        self.assertEqual(aliases["alias"], DecodedFieldAlias("project_ntohs", "hdr->port"))

    def test_resolves_cast_alias_before_decoded_field_target(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt("hdr", "Header *", CastExpr("Header *", FieldAccess(VarRef("pkt"), "data"))),
                DeclarationStmt(
                    "port",
                    "uint16_t",
                    CallExpr("ns_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "port", "uint16_t")]),
                ),
            ],
        )

        self.assertEqual(
            decoded_field_aliases_from_ir(func),
            {"port": DecodedFieldAlias("ns_ntohs", "((Header *)pkt->data)->port")},
        )

    def test_detects_decoded_alias_inside_nested_body(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    VarRef("ready", "int"),
                    [
                        DeclarationStmt(
                            "id",
                            "uint16_t",
                            CallExpr("custom_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "id", "uint16_t")]),
                        )
                    ],
                )
            ],
        )

        self.assertEqual(
            decoded_field_aliases_from_ir(func),
            {"id": DecodedFieldAlias("custom_ntohs", "hdr->id")},
        )

    def test_ignores_non_ntoh_calls_and_non_field_arguments(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt("a", "int", CallExpr("decode16", [FieldAccess(VarRef("hdr"), "id")])),
                DeclarationStmt("b", "int", CallExpr("ntohs", [CallExpr("load", [VarRef("ptr")])])),
            ],
        )

        self.assertEqual(decoded_field_aliases_from_ir(func), {})


if __name__ == "__main__":
    unittest.main()
