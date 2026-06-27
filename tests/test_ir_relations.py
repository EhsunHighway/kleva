from __future__ import annotations

import unittest

from kleva.ir.model import IntLiteral, UnaryOp
from kleva.ir.relations import flipped_relation, int_value, negated_relation, relation_name


class IrRelationTests(unittest.TestCase):
    def test_extracts_integer_literals_and_unary_negative(self):
        self.assertEqual(int_value(IntLiteral(4)), 4)
        self.assertEqual(int_value(UnaryOp("-", IntLiteral(7))), -7)

    def test_flips_ordering_relations(self):
        self.assertEqual(flipped_relation("<"), ">")
        self.assertEqual(flipped_relation("<="), ">=")
        self.assertEqual(flipped_relation(">"), "<")
        self.assertEqual(flipped_relation(">="), "<=")
        self.assertEqual(flipped_relation("=="), "==")

    def test_negates_relations(self):
        self.assertEqual(negated_relation("=="), "!=")
        self.assertEqual(negated_relation("!="), "==")
        self.assertEqual(negated_relation(">"), "<=")
        self.assertEqual(negated_relation("<="), ">")

    def test_relation_names_are_stable(self):
        self.assertEqual(relation_name("=="), "eq")
        self.assertEqual(relation_name("!="), "ne")
        self.assertEqual(relation_name(">="), "ge")


if __name__ == "__main__":
    unittest.main()
