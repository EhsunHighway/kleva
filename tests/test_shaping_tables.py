import unittest

from kleva.ast.model import CFunctionPointerTypedef, CParam, CTypeCatalog
from kleva.shaping.candidates import BranchFact
from kleva.shaping.tables import TableShapeOps, loop_table_candidates


def _param(name, raw_type, base_type, is_pointer=False):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


class TableShapingTests(unittest.TestCase):
    def test_loop_table_candidates_create_match_and_miss_fixture_shapes(self):
        catalog = CTypeCatalog(
            complete_structs={"Context", "Table", "Entry"},
            function_pointers={
                "RecvFn": CFunctionPointerTypedef(
                    name="RecvFn",
                    return_type="void",
                    params=[],
                ),
            },
            struct_fields={
                "Context": {
                    "table": _param("table", "Table *", "Table", True),
                },
                "Table": {
                    "entries": _param("entries", "Entry entries[4]", "Entry"),
                },
                "Entry": {
                    "valid":   _param("valid", "int", "int"),
                    "id":      _param("id", "int", "int"),
                    "handler": _param("handler", "RecvFn", "RecvFn"),
                },
            },
        )
        body = """
        if (ctx->table->entries[i].valid == 1 &&
            ctx->table->entries[i].id == wanted) {
            return 0;
        }
        """

        candidates = loop_table_candidates(
            body,
            aliases={"ctx": ("Context", "ctx_arg")},
            decoded_aliases={},
            direct_aliases={},
            derived_aliases={},
            type_catalog=catalog,
            shaping_features={"loop-tables", "function-pointers"},
            ops=TableShapeOps(
                good_path_setup_from_source=lambda *_args: ["base_ok = 1;"],
                host_to_network_fn=lambda name: name.replace("ntoh", "hton", 1),
                cast_field_expr=lambda cast_type, expr, field: (
                    f"(({cast_type} *){expr})->{field}"
                ),
                function_pointer_stub_preamble=lambda fp_decl: [
                    f"static void stub_{fp_decl.name}(void) {{}}"
                ],
                function_pointer_stub_name=lambda name: f"stub_{name}",
                safe_c_name=lambda value: value,
            ),
        )

        self.assertEqual([candidate.name for candidate in candidates], [
            "source_ctx_entries_match",
            "source_ctx_entries_miss",
        ])
        match = candidates[0]
        self.assertIn("base_ok = 1;", match.setup)
        self.assertIn("Table kleva_ctx_table;", match.setup)
        self.assertIn("((Context *)ctx_arg)->table = &kleva_ctx_table;", match.setup)
        self.assertIn("((Context *)ctx_arg)->table->entries[0].valid = 1;", match.setup)
        self.assertIn("((Context *)ctx_arg)->table->entries[0].id = wanted;", match.setup)
        self.assertIn("((Context *)ctx_arg)->table->entries[0].handler = stub_RecvFn;", match.setup)
        self.assertEqual(match.preamble, ["static void stub_RecvFn(void) {}"])
        self.assertEqual(match.branch_facts, [
            BranchFact("((Context *)ctx_arg)->table->entries[0].id", "==", "wanted"),
        ])

        miss = candidates[1]
        self.assertIn("((Context *)ctx_arg)->table->entries[0].valid = 0;", miss.setup)
        self.assertEqual(miss.preamble, [])
        self.assertEqual(miss.branch_facts, [
            BranchFact("((Context *)ctx_arg)->table->entries[0].valid", "==", "0"),
        ])

    def test_loop_table_candidates_can_map_decoded_match_values_back_to_storage(self):
        catalog = CTypeCatalog(
            complete_structs={"Context", "Table", "Entry", "Header"},
            struct_fields={
                "Context": {
                    "table": _param("table", "Table *", "Table", True),
                },
                "Table": {
                    "entries": _param("entries", "Entry entries[4]", "Entry"),
                },
                "Entry": {
                    "valid": _param("valid", "int", "int"),
                    "id":    _param("id", "int", "int"),
                },
            },
        )
        body = """
        if (ctx->table->entries[i].valid == 1 &&
            ctx->table->entries[i].id == wanted_id) {
            return 0;
        }
        """

        candidates = loop_table_candidates(
            body,
            aliases={
                "ctx": ("Context", "ctx_arg"),
                "hdr": ("Header", "buf->data"),
            },
            decoded_aliases={"wanted_id": ("ns_ntohs", "hdr", "id")},
            direct_aliases={},
            derived_aliases={},
            type_catalog=catalog,
            shaping_features={"loop-tables"},
            ops=TableShapeOps(
                good_path_setup_from_source=lambda *_args: [],
                host_to_network_fn=lambda name: name.replace("ntoh", "hton", 1),
                cast_field_expr=lambda cast_type, expr, field: (
                    f"(({cast_type} *){expr})->{field}"
                ),
                function_pointer_stub_preamble=lambda _fp_decl: [],
                function_pointer_stub_name=lambda name: f"stub_{name}",
                safe_c_name=lambda value: value,
            ),
        )

        self.assertIn("((Header *)buf->data)->id = ns_htons(1);", candidates[0].setup)
        self.assertIn("((Context *)ctx_arg)->table->entries[0].id = 1;", candidates[0].setup)


if __name__ == "__main__":
    unittest.main()
