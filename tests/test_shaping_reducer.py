import unittest

from kleva.shaping.candidates import BranchCandidate, BranchFact, DiversityFact
from kleva.shaping.reducer import reduce_branch_candidates


class CandidateReducerTests(unittest.TestCase):
    def test_dedupes_equivalent_branch_facts_with_different_indexes(self):
        candidates = [
            BranchCandidate(
                "ir_if_0_items_i_id_eq_key",
                ["items[i].id = key;"],
                branch_facts=[BranchFact("items[i].id", "==", "key")],
                origin="ir",
            ),
            BranchCandidate(
                "ir_table_items_id_hit",
                ["items[0].id = key;"],
                branch_facts=[BranchFact("items[0].id", "==", "key")],
                origin="ir",
            ),
        ]

        result = reduce_branch_candidates(candidates)

        self.assertEqual([candidate.name for candidate in result.kept], ["ir_if_0_items_i_id_eq_key"])
        self.assertEqual(result.deduped_count, 1)
        self.assertEqual(result.budget_skip_count, 0)

    def test_caps_noisy_candidate_families(self):
        candidates = [
            BranchCandidate(
                f"ir_if_3_case_{index}",
                [f"value = {index};"],
                branch_facts=[BranchFact("value", "==", str(index))],
                origin="ir",
            )
            for index in range(6)
        ]

        result = reduce_branch_candidates(candidates, max_per_family=3)

        self.assertEqual([candidate.name for candidate in result.kept], [
            "ir_if_3_case_0",
            "ir_if_3_case_1",
            "ir_if_3_case_2",
        ])
        self.assertEqual(result.deduped_count, 0)
        self.assertEqual(result.budget_skip_count, 3)

    def test_caps_diversity_without_affecting_core_candidates(self):
        candidates = [
            BranchCandidate(
                f"ir_diversity_value_{index}",
                [],
                diversity_facts=[DiversityFact("value", "scalar", str(index))],
                origin="ir",
            )
            for index in range(4)
        ]
        candidates.append(BranchCandidate(
            "ir_if_0_value_eq_1",
            ["value = 1;"],
            branch_facts=[BranchFact("value", "==", "1")],
            origin="ir",
        ))

        result = reduce_branch_candidates(candidates, max_diversity=2)

        self.assertEqual([candidate.name for candidate in result.kept], [
            "ir_diversity_value_0",
            "ir_diversity_value_1",
            "ir_if_0_value_eq_1",
        ])
        self.assertEqual(result.budget_skip_count, 2)


if __name__ == "__main__":
    unittest.main()
