import unittest

from kleva.ast.model import CFunctionPointerTypedef, CParam, CTypeCatalog
from kleva.fixtures.construction import function_pointer_stub_name, function_pointer_stub_preamble
from kleva.shaping.conditions import (
    ConditionSetupOps,
    FunctionPointerConditionOps,
    condition_function_pointer_setup,
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

    def test_function_pointer_condition_setup(self):
        catalog = CTypeCatalog(
            function_pointers={
                "RecvFn": CFunctionPointerTypedef(
                    "RecvFn",
                    "void",
                    [CParam("ctx", "void *ctx", "void", True, False, False, 0)],
                )
            },
            struct_fields={
                "Item": {
                    "handler": CParam("handler", "RecvFn handler", "RecvFn", False, False, False, 0)
                }
            },
        )
        ops = FunctionPointerConditionOps(
            split_conjuncts,
            strip_outer_parens,
            _append_unique,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )

        setup, preamble = condition_function_pointer_setup(
            "item->handler",
            "item",
            "table.items[0]",
            "Item",
            catalog,
            ops,
        )

        self.assertEqual(setup, ["table.items[0].handler = kleva_stub_RecvFn;"])
        self.assertTrue(preamble[0].startswith("static void kleva_stub_RecvFn"))


if __name__ == "__main__":
    unittest.main()
