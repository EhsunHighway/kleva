import unittest

from kleva.builder import build_recipe, scalar_sweep_values
from kleva.config import Bounds, FunctionSpec, InputSpec
from kleva.ktest import KTestObject


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
        self.assertEqual(values[-1], {"header_len": 21})
        self.assertEqual(len(values), 22)

    def test_large_bounded_scalar_is_left_to_klee(self):
        spec = FunctionSpec(
            name="packet_create",
            ktest_dir="klee_out",
            inputs=[InputSpec("capacity", "size_t", "cap", bounds=Bounds(1, 1024))],
            body=["packet_create(cap);"],
            outputs=[],
            cleanup=[],
        )

        self.assertEqual(scalar_sweep_values(spec, [_obj("capacity", 64)]), [])


if __name__ == "__main__":
    unittest.main()
