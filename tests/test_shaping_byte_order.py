import unittest

from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.shaping.byte_order import decoded_field_aliases, host_to_network_fn
from kleva.shaping.candidates import (
    BranchCandidate,
    BranchFact,
    CallOutcomeFact,
    NullnessFact,
    ObjectPathFact,
    OwnershipPathFact,
    PostStateFact,
    ScalarIntervalFact,
)


class ByteOrderShapingTests(unittest.TestCase):
    def test_host_to_network_fn_uses_generic_ntoh_to_hton_pattern(self):
        self.assertEqual(host_to_network_fn("ntohs"), "htons")
        self.assertEqual(host_to_network_fn("ns_ntohl"), "ns_htonl")
        self.assertEqual(host_to_network_fn("project_ntohs"), "project_htons")
        self.assertEqual(host_to_network_fn("decode16"), "")

    def test_decoded_field_aliases_tracks_direct_and_copied_locals(self):
        body = """
            uint16_t port = custom_ntohs(hdr->dst_port);
            uint16_t same = port;
            alias = same;
        """

        aliases = decoded_field_aliases(body)

        self.assertEqual(aliases["port"], ("custom_ntohs", "hdr", "dst_port"))
        self.assertEqual(aliases["same"], ("custom_ntohs", "hdr", "dst_port"))
        self.assertEqual(aliases["alias"], ("custom_ntohs", "hdr", "dst_port"))


class ModelTests(unittest.TestCase):
    def test_type_catalog_returns_declared_fields_and_function_pointers(self):
        cb_param = CParam("arg0", "int", "int", False, False, False, 0)
        catalog = CTypeCatalog()
        catalog.struct_fields["Table"] = {
            "count": CParam("count", "int", "int", False, False, False, 0),
        }

        self.assertEqual(catalog.field_type("Table", "count").base_type, "int")
        self.assertIsNone(catalog.field_type("Table", "missing"))
        self.assertIsNone(catalog.function_pointer("MissingCb"))
        self.assertEqual(cb_param.name, "arg0")

    def test_candidate_and_function_models_are_typed_containers(self):
        branch_fact = BranchFact("x", "==", "1")
        call_fact = CallOutcomeFact("prepare", "equals_-1", "success")
        nullness_fact = NullnessFact("ptr", "non-null")
        interval_fact = ScalarIntervalFact("n", lower="1", upper="8")
        ownership_fact = OwnershipPathFact("item", "transferred", "enqueue:queue->slot")
        post_fact = PostStateFact("ctx->ready", "!=", "0")
        object_path = ObjectPathFact("ctx", ("ready",))
        candidate = BranchCandidate(
            "source_case",
            ["x = 1;"],
            witness_outputs=True,
            object_paths=[object_path],
            branch_facts=[branch_fact],
            call_facts=[call_fact],
            nullness_facts=[nullness_fact],
            interval_facts=[interval_fact],
            ownership_facts=[ownership_fact],
            post_state_facts=[post_fact],
        )
        func = CFunction("f", "int", "int", False, [])

        self.assertEqual(candidate.name, "source_case")
        self.assertTrue(candidate.witness_outputs)
        self.assertEqual(candidate.semantic_facts(), (
            branch_fact,
            call_fact,
            object_path,
            nullness_fact,
            interval_fact,
            ownership_fact,
            post_fact,
        ))
        self.assertEqual(candidate.semantic_fact_dicts(), [
            {"kind": "branch", "target": "x", "relation": "==", "value": "1"},
            {"kind": "call", "callee": "prepare", "mode": "equals_-1", "outcome": "success"},
            {"kind": "object_path", "root": "ctx", "path": "ready"},
            {"kind": "nullness", "target": "ptr", "state": "non-null"},
            {"kind": "interval", "target": "x", "exact": "1"},
            {"kind": "interval", "target": "n", "lower": "1", "upper": "8"},
            {"kind": "ownership", "target": "item", "action": "transferred", "via": "enqueue:queue->slot"},
            {"kind": "post_state", "target": "ctx->ready", "relation": "!=", "value": "0"},
        ])
        self.assertEqual(func.return_base, "int")

    def test_candidate_derives_nullness_and_interval_facts_from_branch_facts(self):
        candidate = BranchCandidate(
            "selected_path",
            [],
            branch_facts=[
                BranchFact("ptr", "!=", "0"),
                BranchFact("len", ">=", "4"),
                BranchFact("cap", "<", "16"),
            ],
        )

        self.assertIn(
            {"kind": "nullness", "target": "ptr", "state": "non-null"},
            candidate.semantic_fact_dicts(),
        )
        self.assertIn(
            {"kind": "interval", "target": "len", "lower": "4"},
            candidate.semantic_fact_dicts(),
        )
        self.assertIn(
            {"kind": "interval", "target": "cap", "upper": "(16) - 1"},
            candidate.semantic_fact_dicts(),
        )


if __name__ == "__main__":
    unittest.main()
