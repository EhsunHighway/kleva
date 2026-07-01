import unittest

from kleva.acsl_contract import (
    extract_non_null_params,
    extract_nonzero_params,
    extract_null_params,
    extract_result_value,
    extract_valid_params,
    scalar_values_from_assumptions,
)


class AcslContractTests(unittest.TestCase):
    def test_extracts_null_and_valid_params(self):
        assumes = [
            r"iface == \null || pkt == \NULL",
            r"\valid(sim) && \valid_read(buf + (0 .. len - 1))",
            r"\valid_read((uint8_t *)header + (0 .. header_len - 1))",
        ]

        self.assertEqual(extract_null_params(assumes), ["iface", "pkt"])
        self.assertEqual(extract_valid_params(assumes), ["sim", "buf", "header"])

    def test_extracts_non_null_and_nonzero_params(self):
        assumes = [
            r"ctx != \null && \null != table",
            "port != 0 && bw > 0 && 0 < mtu",
        ]

        self.assertEqual(extract_non_null_params(assumes), ["ctx", "table"])
        self.assertEqual(extract_nonzero_params(assumes), ["port", "bw", "mtu"])

    def test_extracts_scalar_values_from_assumptions(self):
        assumes = ["state == 3 && 0x10 == mask && depth > 0"]

        self.assertEqual(
            scalar_values_from_assumptions(assumes),
            {"state": "3", "mask": "0x10", "depth": "1"},
        )

    def test_extracts_single_result_value(self):
        self.assertEqual(extract_result_value([r"\result == -1"]), -1)
        self.assertEqual(extract_result_value([r"0xFFFF == \result"]), 0xFFFF)
        self.assertIsNone(extract_result_value([r"\result == 0", r"\result == -1"]))


if __name__ == "__main__":
    unittest.main()
