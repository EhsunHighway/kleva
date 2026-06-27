import unittest

from kleva.ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from kleva.fixtures.construction import (
    complete_struct_setup,
    default_scalar_value,
    forward_typedefs_for_function,
    function_prototype,
    function_pointer_stub_preamble,
    lookup_constructor,
    lookup_free_fn,
    pointer_argument_setup,
    unique_name,
)


def _param(name, raw_type, base_type, is_pointer=False, is_array=False, array_size=0):
    return CParam(name, raw_type, base_type, is_pointer, False, is_array, array_size)


class FixtureConstructionTests(unittest.TestCase):
    def test_unique_name_dedupes_generic_identifiers(self):
        used = set()

        self.assertEqual(unique_name("obj", used), "obj")
        self.assertEqual(unique_name("obj", used), "obj_2")

    def test_lookup_constructor_and_free_use_visible_generic_naming(self):
        ctor = CFunction("widget_create", "Widget *", "Widget", True, [])
        free = CFunction("widget_free", "void", "void", False, [])
        decls = {"widget_create": ctor, "widget_free": free}
        source = "void widget_free(Widget *w) { (void)w; }"

        self.assertIs(lookup_constructor("Widget", decls), ctor)
        self.assertEqual(lookup_free_fn("Widget", source), "widget_free")
        self.assertEqual(lookup_free_fn("Widget", None, decls), "widget_free")

    def test_default_scalar_value_uses_common_shape_hints(self):
        self.assertEqual(default_scalar_value(_param("length", "uint16_t", "uint16_t")), "1")
        self.assertEqual(default_scalar_value(_param("count", "int", "int")), "4")
        self.assertEqual(default_scalar_value(_param("value", "int", "int")), "1")

    def test_pointer_argument_setup_allocates_buffer_or_complete_struct(self):
        catalog = CTypeCatalog(
            complete_structs={"Widget", "Queue", "Store"},
            struct_fields={
                "Widget": {
                    "queue": _param("queue", "Queue *queue", "Queue", True),
                    "stores": _param("stores", "Store **stores", "Store", True),
                },
                "Queue": {},
                "Store": {},
            },
        )

        setup, arg, cleanup = pointer_argument_setup(_param("bytes", "uint8_t *", "uint8_t", True))
        self.assertIn("uint8_t bytes_buf[64];", setup)
        self.assertEqual(arg, "bytes_buf")
        self.assertEqual(cleanup, [])

        setup, arg, cleanup = pointer_argument_setup(
            _param("widget", "Widget *", "Widget", True),
            type_catalog=catalog,
        )
        self.assertIn("Widget widget;", setup)
        self.assertIn("Queue widget_queue;", setup)
        self.assertIn("widget.queue = &widget_queue;", setup)
        self.assertIn("Store widget_stores;", setup)
        self.assertIn("Store *widget_stores_slot = &widget_stores;", setup)
        self.assertIn("widget.stores = &widget_stores_slot;", setup)
        self.assertEqual(arg, "&widget")
        self.assertEqual(cleanup, [])

    def test_complete_struct_setup_shapes_struct_and_pointer_arrays(self):
        catalog = CTypeCatalog(
            complete_structs={"Owner", "Item", "Slot"},
            struct_fields={
                "Owner": {
                    "items": _param("items", "Item items[4]", "Item", False, True, 4),
                    "slots": _param("slots", "Slot *slots[4]", "Slot", True, True, 4),
                },
                "Item": {},
                "Slot": {},
            },
        )

        setup = complete_struct_setup("Owner", "owner", catalog, set())

        self.assertIn("Item owner_items_0;", setup)
        self.assertIn("owner.items[0] = owner_items_0;", setup)
        self.assertIn("Slot owner_slots_0;", setup)
        self.assertIn("owner.slots[0] = &owner_slots_0;", setup)

    def test_complete_struct_setup_can_follow_only_required_paths(self):
        catalog = CTypeCatalog(
            complete_structs={"Widget", "Queue", "Store"},
            struct_fields={
                "Widget": {
                    "queue": _param("queue", "Queue *queue", "Queue", True),
                    "store": _param("store", "Store *store", "Store", True),
                },
                "Queue": {},
                "Store": {},
            },
        )

        setup = complete_struct_setup("Widget", "widget", catalog, set(), required_paths=[("queue", "size")])

        self.assertIn("Widget widget;", setup)
        self.assertIn("Queue widget_queue;", setup)
        self.assertIn("widget.queue = &widget_queue;", setup)
        self.assertNotIn("Store widget_store;", setup)
        self.assertNotIn("widget.store = &widget_store;", setup)

    def test_pointer_argument_setup_uses_visible_constructor_and_cleanup(self):
        ctor = CFunction(
            "widget_create",
            "Widget *",
            "Widget",
            True,
            [_param("capacity", "size_t", "size_t")],
        )
        setup, arg, cleanup = pointer_argument_setup(
            _param("widget", "Widget *", "Widget", True),
            source_text="void widget_free(Widget *w) {}",
            function_decls={"widget_create": ctor},
        )

        self.assertIn("Widget * widget_create(size_t capacity);", setup)
        self.assertIn("void widget_free(Widget *arg0);", setup)
        self.assertIn("Widget *widget = widget_create(64);", setup)
        self.assertEqual(arg, "widget")
        self.assertEqual(cleanup, ["widget_free(widget);"])

    def test_pointer_argument_setup_prefers_constructor_for_complete_struct(self):
        catalog = CTypeCatalog(
            complete_structs={"Queue", "Item"},
            struct_fields={
                "Queue": {
                    "items": _param("items", "Item **items", "Item", True),
                    "count": _param("count", "size_t count", "size_t"),
                    "capacity": _param("capacity", "size_t capacity", "size_t"),
                },
                "Item": {},
            },
        )
        ctor = CFunction(
            "queue_create",
            "Queue *",
            "Queue",
            True,
            [_param("capacity", "size_t capacity", "size_t")],
        )

        setup, arg, cleanup = pointer_argument_setup(
            _param("queue", "Queue *queue", "Queue", True),
            type_catalog=catalog,
            function_decls={"queue_create": ctor},
        )

        self.assertIn("Queue * queue_create(size_t capacity);", setup)
        self.assertIn("Queue *queue = queue_create(64);", setup)
        self.assertNotIn("Queue queue;", setup)
        self.assertEqual(arg, "queue")
        self.assertEqual(cleanup, [])

    def test_function_prototype_preserves_declared_param_shapes(self):
        decl = CFunction(
            "thing_create",
            "Thing *",
            "Thing",
            True,
            [
                _param("name", "const char *name", "char", True),
                _param("digest", "const uint8_t digest[6]", "uint8_t", False, True, 6),
            ],
        )

        self.assertEqual(
            function_prototype(decl),
            "Thing * thing_create(const char *name, const uint8_t digest[6]);",
        )

    def test_forward_typedefs_cover_custom_pointer_types(self):
        decl = CFunction(
            "manager_create",
            "Manager *",
            "Manager",
            True,
            [
                _param("store", "Store *store", "Store", True),
                _param("queue", "Queue *queue", "Queue", True),
                _param("capacity", "size_t capacity", "size_t"),
            ],
        )

        self.assertEqual(
            forward_typedefs_for_function(decl),
            [
                "typedef struct Manager Manager;",
                "typedef struct Store Store;",
                "typedef struct Queue Queue;",
            ],
        )

    def test_function_pointer_stub_preamble_is_typed(self):
        decl = CFunctionPointerTypedef(
            "RecvFn",
            "int",
            [_param("ctx", "void *ctx", "void", True)],
        )

        preamble = function_pointer_stub_preamble(decl)

        self.assertEqual(preamble[0], "static int kleva_stub_RecvFn(void *ctx) {")
        self.assertIn("    (void)ctx;", preamble)
        self.assertIn("    return 0;", preamble)


if __name__ == "__main__":
    unittest.main()
