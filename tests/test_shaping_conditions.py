import unittest

from kleva.shaping.conditions import (
    ConditionSetupOps,
    condition_setup_lines,
    rewrite_source_alias_exprs,
    split_conjuncts,
    strip_outer_parens,
)


def _append_unique(lines, line, seen):
    if line not in seen:
        seen.add(line)
        lines.append(line)


class ConditionShapingTests(unittest.TestCase):
    def test_splits_conjuncts_without_splitting_nested_expressions(self):
        expr = "a && fn(b && c) && (d && e)"

        self.assertEqual(split_conjuncts(expr), ["a", "fn(b && c)", "(d && e)"])
        self.assertEqual(strip_outer_parens("((value))"), "value")

    def test_condition_setup_shapes_simple_comparisons(self):
        ops = ConditionSetupOps(
            lambda local, value, *_args: [f"{local} |= {value};"],
            lambda local, value, *_args: [f"{local} = {value};"],
            _append_unique,
            lambda value: f"not_{value}",
        )

        setup = condition_setup_lines(
            "flags & READY && size > limit && state != CLOSED",
            {},
            {},
            {},
            {},
            ops,
        )

        self.assertEqual(setup, ["flags |= READY;", "size = ((limit) + 1);", "state = not_CLOSED;"])

    def test_rewrites_aliases_and_result_var(self):
        line = "item->ready = other->value;"
        aliases = {"other": ("Record", "ctx->data")}

        self.assertEqual(
            rewrite_source_alias_exprs(line, aliases, "item", "table.items[0]"),
            "table.items[0].ready = ((Record *)ctx->data)->value;",
        )


if __name__ == "__main__":
    unittest.main()
