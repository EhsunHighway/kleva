from __future__ import annotations

import unittest

from kleva.ir.aliases import record_alias, resolve_aliases
from kleva.ir.model import (
    AssignmentStmt,
    BinaryOp,
    CallExpr,
    DeclarationStmt,
    FieldAccess,
    IntLiteral,
    VarRef,
)


class IrAliasTests(unittest.TestCase):
    def test_records_declaration_alias_and_resolves_chained_var_ref(self):
        aliases = {}
        record_alias(
            DeclarationStmt(
                "state",
                "int",
                FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
            ),
            aliases,
        )
        record_alias(
            DeclarationStmt("copy", "int", VarRef("state", "int")),
            aliases,
        )

        resolved = resolve_aliases(VarRef("copy", "int"), aliases)

        self.assertEqual(
            resolved,
            FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
        )

    def test_records_assignment_alias_and_resolves_inside_expression(self):
        aliases = {}
        record_alias(
            AssignmentStmt(
                VarRef("kind", "int"),
                FieldAccess(VarRef("item", "Item *"), "kind", "int"),
            ),
            aliases,
        )

        resolved = resolve_aliases(
            BinaryOp("==", VarRef("kind", "int"), IntLiteral(3, "int")),
            aliases,
        )

        self.assertEqual(
            resolved,
            BinaryOp(
                "==",
                FieldAccess(VarRef("item", "Item *"), "kind", "int"),
                IntLiteral(3, "int"),
            ),
        )

    def test_does_not_record_call_result_as_plain_alias(self):
        aliases = {}
        record_alias(
            DeclarationStmt("ready", "int", CallExpr("is_ready", [VarRef("ctx")])),
            aliases,
        )

        self.assertEqual(aliases, {})


if __name__ == "__main__":
    unittest.main()
