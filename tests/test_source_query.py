import unittest

from kleva.ast.model import CFunction
from kleva.ast.source_query import (
    camel_to_snake,
    function_accepts_null_param,
    function_body,
    function_definition_body,
    function_frees_param,
    function_returns_owned_pointer,
    function_takes_param_ownership,
    lower_first,
    visible_function,
)


class SourceQueryTests(unittest.TestCase):
    def test_naming_helpers_are_generic(self):
        self.assertEqual(lower_first("Widget"), "widget")
        self.assertEqual(camel_to_snake("HTTPServerState"), "http_server_state")

    def test_finds_visible_function_and_body(self):
        source = """
            int declared_only(int x);
            int compute(int x) {
                if (x) { return 1; }
                return 0;
            }
        """

        self.assertTrue(visible_function("compute", source))
        self.assertIn("return 1", function_body(source, "compute"))
        self.assertEqual(function_body(source, "declared_only"), "")
        self.assertIn("return 0", function_definition_body(source, "compute"))

    def test_detects_cleanup_and_ownership_patterns(self):
        source = """
            void release(Node *node) { node_free(node); }
            int maybe_null(Node *node) { if (!node) return -1; return 0; }
            int enqueue_item(Queue *q, Node *node) { queue_enqueue(q, node); return 0; }
        """

        self.assertTrue(function_frees_param(source, "release", "node"))
        self.assertTrue(function_accepts_null_param(source, "maybe_null", "node"))
        self.assertTrue(function_takes_param_ownership(source, "enqueue_item", "node"))

    def test_detects_owned_pointer_return_by_constructor_name(self):
        func = CFunction("widget_create", "Widget *", "Widget", True, [])
        plain = CFunction("widget_find", "Widget *", "Widget", True, [])

        self.assertTrue(function_returns_owned_pointer(func))
        self.assertFalse(function_returns_owned_pointer(plain))


if __name__ == "__main__":
    unittest.main()
