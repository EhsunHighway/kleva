import unittest

from kleva.ast.parser import (
    build_type_catalog,
    function_decl_map,
    parse_function_decls,
    split_call_args,
    strip_comments,
)


class AstParserTests(unittest.TestCase):
    def test_strip_comments_removes_c_and_acsl_comments(self):
        text = "int a; /* block */ //@ line\n/*@ ensures true; */ int b;"

        stripped = strip_comments(text)

        self.assertIn("int a;", stripped)
        self.assertIn("int b;", stripped)
        self.assertNotIn("block", stripped)
        self.assertNotIn("ensures", stripped)

    def test_split_call_args_keeps_nested_commas_together(self):
        args = "one, fn(two, three), arr[i, j], (x ? y : z)"

        self.assertEqual(
            split_call_args(args),
            ["one", "fn(two, three)", "arr[i, j]", "(x ? y : z)"],
        )

    def test_parse_function_decls_extracts_public_functions(self):
        header = """
            #define THING 1
            typedef struct Entry Entry;
            int table_lookup(Table *table, const uint16_t key);
            void _private_helper(void);
        """

        funcs = parse_function_decls(header)

        self.assertEqual([func.name for func in funcs], ["table_lookup"])
        self.assertEqual(funcs[0].params[0].name, "table")
        self.assertTrue(funcs[0].params[0].is_pointer)
        self.assertEqual(funcs[0].params[1].base_type, "uint16_t")

    def test_function_decl_map_includes_static_definitions(self):
        source = """
            static Entry *find_entry(Table *table, int key) {
                return 0;
            }
        """

        decls = function_decl_map(source)

        self.assertIn("find_entry", decls)
        self.assertTrue(decls["find_entry"].return_is_pointer)
        self.assertEqual(decls["find_entry"].params[1].name, "key")

    def test_build_type_catalog_tracks_struct_fields_and_function_pointers(self):
        text = """
            typedef void (*RecvFn)(int value, void *ctx);
            typedef struct Entry {
                int valid;
                RecvFn recv;
            } Entry;
            typedef struct Opaque Opaque;
        """

        catalog = build_type_catalog(text)

        self.assertIn("Entry", catalog.complete_structs)
        self.assertIn("Opaque", catalog.opaque_structs)
        self.assertEqual(catalog.field_type("Entry", "recv").base_type, "RecvFn")
        self.assertEqual(catalog.function_pointer("RecvFn").params[0].name, "value")


if __name__ == "__main__":
    unittest.main()
