import unittest

from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.shaping.lookups import (
    FallbackLookupOps,
    LookupFixtureOps,
    LookupInferOps,
    LookupSetupOps,
    LookupShape,
    alias_pointer_guard_setup,
    infer_lookup_shape,
    lookup_condition_setup,
    lookup_container_setup,
    fallback_lookup_candidates,
)


def _param(name, raw_type, base_type, is_pointer=False):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


def _function(name, return_type, return_base, params):
    return CFunction(name, return_type, return_base, "*" in return_type, params)


def _append_unique(lines, line, seen):
    if line not in seen:
        seen.add(line)
        lines.append(line)


class LookupShapingTests(unittest.TestCase):
    def test_infers_generic_array_lookup_shape(self):
        source = "source text is supplied through callbacks"
        body = "Record *rec = find_record(db, wanted); switch (rec->state) { case OPEN: break; }"

        def decls(_source):
            return {
                "find_record": _function(
                    "find_record",
                    "Record *",
                    "Record",
                    [
                        _param("table", "Table *", "Table", True),
                        _param("key", "int", "int"),
                    ],
                )
            }

        def function_body(_source, name):
            self.assertEqual(name, "find_record")
            return """
                for (int i = 0; i < 4; i++) {
                    Record *slot = &table->items[i];
                    if (slot->valid && slot->id == key) {
                        return slot;
                    }
                }
                return NULL;
            """

        ops = LookupInferOps(decls, function_body, lambda raw: [p.strip() for p in raw.split(",")])
        shapes = infer_lookup_shape(body, source, CTypeCatalog(), ops)

        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].callee, "find_record")
        self.assertEqual(shapes[0].result_var, "rec")
        self.assertEqual(shapes[0].container_expr, "db")
        self.assertEqual(shapes[0].array_field, "items")
        self.assertIn("slot->id == key)", "\n".join(shapes[0].conditions))

    def test_lookup_condition_setup_shapes_hit_without_domain_names(self):
        shape = infer_lookup_shape(
            "Record *rec = find_record(db, wanted); switch (rec->state) { case OPEN: break; }",
            "source",
            CTypeCatalog(),
            LookupInferOps(
                lambda _source: {
                    "find_record": _function(
                        "find_record",
                        "Record *",
                        "Record",
                        [
                            _param("table", "Table *", "Table", True),
                            _param("key", "int", "int"),
                        ],
                    )
                },
                lambda _source, _name: (
                    "Record *slot = &table->items[i];"
                    "if (slot->valid && slot->id == key) return slot;"
                ),
                lambda raw: [p.strip() for p in raw.split(",")],
            ),
        )[0]
        ops = LookupSetupOps(
            lambda expr, _aliases: expr,
            _append_unique,
            lambda local, value, *_rest: [f"{local} = {value};"],
            lambda rhs: f"(({rhs}) + 1)",
        )

        setup = lookup_condition_setup(shape, {}, {}, {}, {}, ops)

        self.assertIn("db->items[0].valid = 1;", setup)
        self.assertIn("db->items[0].id = 1;", setup)
        self.assertIn("wanted = 1;", setup)

    def test_fallback_lookup_candidate_uses_generic_exact_miss_then_fallback_hit(self):
        body = """
            Record *exact = find_exact(db, wanted);
            if (!exact && allow_fallback) { fallback = find_any(db, wanted); }
            if (fallback) { return 1; }
        """

        def decls(_source):
            params = [
                _param("table", "Table *", "Table", True),
                _param("key", "int", "int"),
            ]
            return {
                "find_exact": _function("find_exact", "Record *", "Record", params),
                "find_any": _function("find_any", "Record *", "Record", params),
            }

        def function_body(_source, name):
            if name == "find_exact":
                return "Record *slot = &table->items[i]; if (slot->id == key) return slot;"
            return "Record *slot = &table->items[i]; if (slot->valid) return slot;"

        infer_ops = LookupInferOps(decls, function_body, lambda raw: [p.strip() for p in raw.split(",")])
        fallback_ops = FallbackLookupOps(
            lambda text: text,
            lambda *_args: [],
            lambda *_args: [],
            lambda *_args: ["allow_fallback = 1;"],
            lambda shape, *_args: [f"Table owner; {shape.container_expr} = &owner;"],
            lambda *_args: ["db->items[0].valid = 1;"],
            lambda *_args: ["db->items[0].id = 0;", "wanted = 1;"],
            lambda name: name,
        )

        candidates = fallback_lookup_candidates(
            body,
            "source",
            {},
            {},
            {},
            {},
            CTypeCatalog(),
            {"fallback-lookups"},
            infer_ops,
            fallback_ops,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "source_fallback_lookup_hit")
        self.assertIn("allow_fallback = 1;", candidates[0].setup)
        self.assertIn("db->items[0].id = 0;", candidates[0].setup)

    def test_lookup_container_setup_builds_owner_for_plain_container_expr(self):
        shape = LookupShape(
            callee="find_record",
            result_var="rec",
            element_type="Record",
            element_alias="slot",
            container_type="Table",
            container_expr="db",
            array_field="items",
            param_args={},
            conditions=[],
        )
        ops = LookupFixtureOps(
            lambda expr, _aliases: expr,
            lambda *_args: [],
            lambda cast_type, expr, field: f"(({cast_type} *){expr})->{field}",
            _append_unique,
            lambda value: value,
        )

        self.assertEqual(
            lookup_container_setup(shape, {}, CTypeCatalog(), ops),
            [
                "Table kleva_rec_items_owner;",
                "memset(&kleva_rec_items_owner, 0, sizeof(kleva_rec_items_owner));",
                "db = &kleva_rec_items_owner;",
            ],
        )

    def test_lookup_container_and_pointer_guard_shape_casted_owner_fields(self):
        catalog = CTypeCatalog(
            complete_structs={"Context", "Table"},
            struct_fields={
                "Context": {
                    "table": _param("table", "Table *", "Table", True),
                    "opaque": _param("opaque", "Opaque *", "Opaque", True),
                },
            },
        )
        shape = LookupShape(
            callee="find_record",
            result_var="rec",
            element_type="Record",
            element_alias="slot",
            container_type="Table",
            container_expr="ctx->table",
            array_field="items",
            param_args={},
            conditions=[],
        )
        ops = LookupFixtureOps(
            lambda expr, aliases: expr.replace("ctx", "((Context *)raw)") if aliases else expr,
            lambda alias, cast_type, expr, _params: [
                f"{cast_type} backing;",
                f"{alias} uses {expr};",
            ],
            lambda cast_type, expr, field: f"(({cast_type} *){expr})->{field}",
            _append_unique,
            lambda value: value,
        )

        aliases = {"ctx": ("Context", "raw")}
        self.assertEqual(
            lookup_container_setup(shape, aliases, catalog, ops),
            [
                "Table kleva_rec_items_owner;",
                "memset(&kleva_rec_items_owner, 0, sizeof(kleva_rec_items_owner));",
                "Context backing;",
                "ctx uses raw;",
                "((Context *)raw)->table = &kleva_rec_items_owner;",
            ],
        )
        self.assertEqual(
            alias_pointer_guard_setup("if (!ctx->table && !ctx->opaque) return -1;", aliases, catalog, {"ctx->table"}, ops),
            ["((Context *)raw)->opaque = (Opaque *)1;"],
        )


if __name__ == "__main__":
    unittest.main()
