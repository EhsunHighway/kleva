import unittest

from kleva.eva import EvaValueLine, parse_eva_report, parse_singletons, tokenize_eva_log


class EvaReportTests(unittest.TestCase):
    def test_parse_report_keeps_singleton_and_non_singleton_outputs(self):
        log = """
[eva] ====== VALUES COMPUTED ======
[eva:final-states] Values at end of function probe_case:
  out_a ∈ {7}
  out_b ∈ {0; 1}
[eva:summary] ====== ANALYSIS SUMMARY ======
  Preconditions     4 valid     0 unknown     0 invalid      4 total
"""

        report = parse_eva_report(log, raw_log_path="eva/logs/probe_case.txt")

        self.assertEqual(report.raw_log_path, "eva/logs/probe_case.txt")
        self.assertTrue(report.has_final_state("probe_case"))
        self.assertEqual(report.singletons_for("probe_case"), {"out_a": 7})
        self.assertEqual(report.values_for("probe_case")["out_b"], "{0; 1}")
        self.assertEqual(report.value_nodes_for("probe_case")["out_a"].kind, "singleton")
        self.assertEqual(report.value_nodes_for("probe_case")["out_b"].kind, "set")
        self.assertEqual(report.preconditions.valid, 4)

    def test_tokenizer_classifies_eva_value_kinds(self):
        log = """
  out_single ∈ {-1}
  out_set ∈ {0; 1}
  out_interval ∈ [0..255]
  out_unknown ∈ [--..--]
  p ∈ {{ &__malloc_p }}
"""

        values = [
            token.value
            for token in tokenize_eva_log(log)
            if isinstance(token, EvaValueLine)
        ]

        self.assertEqual(
            [(value.name, value.kind, value.singleton) for value in values],
            [
                ("out_single", "singleton", -1),
                ("out_set", "set", None),
                ("out_interval", "interval", None),
                ("out_unknown", "unknown", None),
                ("p", "address_or_pointer_set", None),
            ],
        )

    def test_parse_report_records_parse_errors_and_alarms(self):
        log = """
[kernel] probe.c:10: User Error:
  Return statement with a value in function returning void
[kernel] Frama-C aborted: invalid user input.
[eva:alarm] probe.c:20: Warning:
  function f: precondition valid_read got status unknown.
"""

        report = parse_eva_report(log)

        self.assertTrue(report.parse_errors)
        self.assertTrue(report.alarms)
        self.assertTrue(report.warnings)

    def test_legacy_singleton_parser_still_works(self):
        log = """
[eva:final-states] Values at end of function probe_case:
  out_a ∈ {7}
  out_b ∈ {0; 1}
"""

        self.assertEqual(parse_singletons(log), {"probe_case": {"out_a": 7}})


if __name__ == "__main__":
    unittest.main()
