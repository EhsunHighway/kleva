import unittest

from kleva.builder import build_recipe, reduce_equivalent_candidate_recipes, scalar_sweep_values
from kleva.config import Bounds, FunctionSpec, InputSpec
from kleva.ktest import KTestObject
from kleva.recipe import Recipe


def _obj(name, value, size=8):
    return KTestObject(name, size, int(value).to_bytes(size, "little"))


class BuilderTests(unittest.TestCase):
    def test_bounded_scalar_sweep_enumerates_small_range(self):
        spec = FunctionSpec(
            name="packet_prepend",
            ktest_dir="klee_out",
            inputs=[
                InputSpec("header", "uint8_t[]", "hdr", length_from="header_len", max_length=4),
                InputSpec("header_len", "size_t", "hlen", bounds=Bounds(1, 4)),
            ],
            body=["packet_prepend(p, hdr, hlen);"],
            outputs=[],
            cleanup=[],
        )
        objs = [
            KTestObject("header", 4, b"\xAA\xBB\xCC\xDD"),
            _obj("header_len", 2),
        ]

        self.assertEqual(
            scalar_sweep_values(spec, objs),
            [{"header_len": 1}, {"header_len": 2}, {"header_len": 3}, {"header_len": 4}],
        )
        recipe = build_recipe(spec, objs, 1, scalar_overrides={"header_len": 3})
        self.assertIsNotNone(recipe)
        self.assertIn("uint8_t hdr[3] = {0xAA, 0xBB, 0xCC};", recipe.decl_lines)
        self.assertIn("size_t hlen = (size_t)3ULL;", recipe.decl_lines)

    def test_lone_unbounded_scalar_gets_default_small_sweep(self):
        spec = FunctionSpec(
            name="packet_strip",
            ktest_dir="klee_out",
            inputs=[InputSpec("header_len", "size_t", "hlen")],
            body=["packet_strip(p, hlen);"],
            outputs=[],
            cleanup=[],
        )

        values = scalar_sweep_values(spec, [_obj("header_len", 999)])

        self.assertEqual(values[0], {"header_len": 0})
        self.assertEqual(values[-1], {"header_len": 2})
        self.assertEqual(len(values), 3)

    def test_large_bounded_scalar_uses_boundary_sweep(self):
        spec = FunctionSpec(
            name="packet_create",
            ktest_dir="klee_out",
            inputs=[InputSpec("capacity", "size_t", "cap", bounds=Bounds(1, 1024))],
            body=["packet_create(cap);"],
            outputs=[],
            cleanup=[],
        )

        self.assertEqual(
            scalar_sweep_values(spec, [_obj("capacity", 64)]),
            [{"capacity": 1}, {"capacity": 2}, {"capacity": 1023}, {"capacity": 1024}, {"capacity": 512}],
        )

    def test_recipe_reducer_preserves_direct_recipes(self):
        recipes = [
            Recipe(f"direct_tv{i:03d}", [f"int x = {i};"], ["run(x);"], [], ["out_ret"])
            for i in range(1, 4)
        ]

        kept, skipped = reduce_equivalent_candidate_recipes(recipes)

        self.assertEqual(skipped, 0)
        self.assertEqual([recipe.fn_id for recipe in kept], ["direct_tv001", "direct_tv002", "direct_tv003"])

    def test_recipe_reducer_caps_equivalent_candidate_recipes(self):
        recipes = [
            Recipe(
                f"shape_tv{i:03d}",
                [f"int x = {i};"],
                ["int out_ret = run(x);"],
                [],
                ["out_ret"],
                candidate=True,
                source_location="source.c:10:5",
                target_branch="if x > 0",
                candidate_origin="ir",
                candidate_facts=[{"kind": "branch", "expr": "x > 0"}],
            )
            for i in range(1, 4)
        ]

        kept, skipped = reduce_equivalent_candidate_recipes(recipes)

        self.assertEqual(skipped, 2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].fn_id, "shape_tv001")


if __name__ == "__main__":
    unittest.main()
