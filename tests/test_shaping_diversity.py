import unittest

from kleva.ast.model import CFunction, CParam
from kleva.shaping.diversity import curated_diversity_candidates


class DiversityShapingTests(unittest.TestCase):
    def test_generates_curated_scalar_candidates_without_cartesian_product(self):
        func = CFunction(
            name="resize",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("len", "size_t len", "size_t", False, False, False, 0),
                CParam("mode", "uint8_t mode", "uint8_t", False, False, False, 0),
            ],
        )

        candidates = curated_diversity_candidates(
            func,
            {
                "size_t": (0, 1024),
                "uint8_t": (0, 255),
            },
        )

        self.assertEqual(
            [(candidate.name, candidate.call_arg_overrides) for candidate in candidates],
            [
                ("ir_diversity_len_zero", {"len": "0"}),
                ("ir_diversity_len_one", {"len": "1"}),
                ("ir_diversity_len_two", {"len": "2"}),
                ("ir_diversity_mode_zero", {"mode": "0"}),
                ("ir_diversity_mode_one", {"mode": "1"}),
                ("ir_diversity_mode_max", {"mode": "255"}),
            ],
        )
        self.assertTrue(all(candidate.origin == "ir" for candidate in candidates))
        self.assertEqual(candidates[0].semantic_fact_dicts()[0]["kind"], "diversity")

    def test_scalar_diversity_respects_positive_acsl_bounds(self):
        func = CFunction(
            name="resize",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("len", "size_t len", "size_t", False, False, False, 0)],
        )

        candidates = curated_diversity_candidates(
            func,
            {"size_t": (0, 1024)},
            assumes=["len > 0"],
        )

        self.assertEqual(
            [(candidate.name, candidate.call_arg_overrides) for candidate in candidates],
            [
                ("ir_diversity_len_one", {"len": "1"}),
                ("ir_diversity_len_two", {"len": "2"}),
            ],
        )

    def test_scalar_diversity_respects_flipped_acsl_bounds(self):
        func = CFunction(
            name="resize",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("len", "size_t len", "size_t", False, False, False, 0)],
        )

        candidates = curated_diversity_candidates(
            func,
            {"size_t": (0, 1024)},
            assumes=["0 < len"],
        )

        self.assertEqual(
            [(candidate.name, candidate.call_arg_overrides) for candidate in candidates],
            [
                ("ir_diversity_len_one", {"len": "1"}),
                ("ir_diversity_len_two", {"len": "2"}),
            ],
        )

    def test_generates_byte_buffer_content_candidates(self):
        func = CFunction(
            name="consume",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("data", "uint8_t *data", "uint8_t", True, False, False, 0)],
        )

        candidates = curated_diversity_candidates(func, {})

        self.assertEqual(
            [(candidate.name, candidate.fixture_requirements[0].content) for candidate in candidates],
            [
                ("ir_diversity_data_all_zero", "all-zero"),
                ("ir_diversity_data_all_0xff", "all-0xff"),
                ("ir_diversity_data_first_byte_set", "first-byte-set"),
            ],
        )
        self.assertEqual(candidates[2].semantic_fact_dicts()[0]["value"], "first-byte-set")

    def test_skips_unknown_typedef_scalars(self):
        func = CFunction(
            name="install",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[CParam("fn", "RxHandler fn", "RxHandler", False, False, False, 0)],
        )

        self.assertEqual(curated_diversity_candidates(func, {"int": (0, 10)}), [])


if __name__ == "__main__":
    unittest.main()
