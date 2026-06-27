import unittest

from kleva.config import load_config_text


class ConfigTests(unittest.TestCase):
    def test_loads_candidate_facts(self):
        cfg = load_config_text(
            """
module:
  name: mod
  header: mod.h
  source: mod.c
functions:
  - name: case_open
    ktest_dir: klee_build/klee_out_case_open
    inputs: []
    body: []
    outputs: []
    cleanup: []
    candidate: true
    candidate_facts:
      - kind: branch
        target: ctx->state
        relation: case
        value: OPEN
"""
        )

        self.assertEqual(cfg.functions[0].candidate_facts, [
            {"kind": "branch", "target": "ctx->state", "relation": "case", "value": "OPEN"},
        ])


if __name__ == "__main__":
    unittest.main()
