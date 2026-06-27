import unittest

from kleva.acsl import ACSLBehavior
from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.bodygen import (
    BodyGenOps,
    gen_null_setup_body,
    gen_valid_setup_body,
    param_ref_from_arg,
)
from kleva.fixtures.construction import pointer_argument_setup, unique_name
from kleva.shaping.candidates import ObjectPathFact
from kleva.shaping.ir_ownership import CONSUMED, TRANSFERRED, OwnershipSummary


def _ops() -> BodyGenOps:
    return BodyGenOps(
        scalar_bounds={"int": (0, 10)},
        default_shaping_features=frozenset({"function-pointers"}),
        scalar_values_from_assumptions=lambda _assumes: {},
        extract_result_value=lambda ensures: -1 if any("-1" in e for e in ensures) else None,
        extract_non_null_params=lambda _assumes: [],
        extract_nonzero_params=lambda _assumes: [],
        extract_null_params=lambda _assumes: [],
        extract_valid_params=lambda _assumes: [],
        is_void_star=lambda p: p.raw_type.strip() == "void *",
        pointer_argument_setup=lambda p, *_args, **_kwargs: (
            [f"{p.base_type} {p.name}_obj;"],
            f"&{p.name}_obj",
            [],
        ),
        needs_len_data_shape=lambda *_args: False,
        append_len_data_shape=lambda _lines, _arg: None,
        param_ref_from_arg=param_ref_from_arg,
        function_frees_param=lambda *_args: False,
        function_takes_param_ownership=lambda *_args: False,
        function_accepts_null_param=lambda *_args: False,
        function_returns_owned_pointer=lambda _func: False,
        lookup_free_fn=lambda *_args: None,
        assumption_setup_lines=lambda *_args: [],
        source_for_branch_shaping=lambda source_text, _func_name: source_text or "",
        void_param_cast_types=lambda *_args: {},
        unique_name=lambda base, _used: base,
        function_pointer_stub_preamble=lambda _fp_decl: [],
        function_pointer_stub_name=lambda name: f"{name}_stub",
        rewrite_setup_with_param_args=lambda lines, _param_args: lines,
        safe_c_name=lambda value: value,
    )


class BodyGenTests(unittest.TestCase):
    def test_param_ref_from_arg_detects_object_and_pointer_access(self):
        self.assertEqual(param_ref_from_arg("&ctx"), ("ctx", "."))
        self.assertEqual(param_ref_from_arg("ctx"), ("ctx", "->"))
        self.assertIsNone(param_ref_from_arg("NULL"))
        self.assertIsNone(param_ref_from_arg("items[0]"))

    def test_null_setup_body_passes_null_and_emits_result_sentinel(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("ctx", "Context *", "Context", True, False, False, 0),
                CParam("count", "int", "int", False, False, False, 0),
            ],
        )
        behavior = ACSLBehavior(
            name="null_ctx",
            assumes=[r"ctx == \null"],
            ensures=[r"\result == -1"],
        )

        lines, outputs, cleanup, preamble = gen_null_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            None,
            None,
            None,
            _ops(),
        )

        self.assertIn("int out_ret = run(NULL, 0);", lines)
        self.assertIn("int out_sentinel = (out_ret == -1) ? 1 : 0;", lines)
        self.assertEqual(outputs, ["out_ret", "out_sentinel"])
        self.assertEqual(cleanup, [])
        self.assertEqual(preamble, [])

    def test_valid_setup_body_uses_pointer_setup_for_valid_pointer_param(self):
        func = CFunction(
            name="open_ctx",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        behavior = ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"])

        lines, outputs, cleanup, preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
        )

        self.assertIn("Context ctx_obj;", lines)
        self.assertIn("int out_ret = open_ctx(&ctx_obj);", lines)
        self.assertEqual(outputs, ["out_ret"])
        self.assertEqual(cleanup, [])
        self.assertEqual(preamble, [])

    def test_valid_setup_body_applies_call_argument_overrides(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("handler", "Handler handler", "Handler", False, False, False, 0),
                CParam("count", "int", "int", False, False, False, 0),
            ],
        )
        catalog = CTypeCatalog()
        behavior = ACSLBehavior(name="valid", assumes=[])

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            behavior,
            None,
            catalog,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            call_arg_overrides={"handler": "NULL"},
        )

        self.assertIn("int out_ret = run(NULL, 0);", lines)

    def test_valid_setup_body_uses_mutable_scalar_when_candidate_assigns_it(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("limit", "int limit", "int", False, False, False, 0)],
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            ACSLBehavior(name="valid", assumes=[]),
            None,
            None,
            None,
            ["limit = 1;"],
            None,
            True,
            False,
            _ops(),
            witness_setup=["int out_limit_nonzero = (limit != 0);"],
            extra_outputs=["out_limit_nonzero"],
        )

        self.assertIn("int limit = 0;", lines)
        self.assertIn("limit = 1;", lines)
        self.assertIn("run(limit);", lines)
        self.assertIn("int out_limit_nonzero = (limit != 0);", lines)

    def test_valid_setup_body_appends_explicit_witness_outputs(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            ACSLBehavior(name="valid", assumes=[]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            witness_setup=["int out_cb_called = cb_called;"],
            extra_outputs=["out_cb_called"],
        )

        self.assertLess(lines.index("int out_ret = run();"), lines.index("int out_cb_called = cb_called;"))
        self.assertEqual(outputs, ["out_ret", "out_cb_called"])

    def test_source_witness_outputs_skip_array_fields(self):
        func = CFunction(
            name="step",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context", "Device"},
            struct_fields={
                "Context": {
                    "base": CParam("base", "Device", "Device", False, False, False, 0),
                    "count": CParam("count", "int", "int", False, False, False, 0),
                    "handlers": CParam("handlers", "Handler handlers[4]", "Handler", True, False, True, 4),
                },
                "Device": {
                    "name": CParam("name", "char name[32]", "char", True, False, True, 32),
                    "iface_count": CParam("iface_count", "int", "int", False, False, False, 0),
                },
            },
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            "",
            catalog,
            None,
            None,
            None,
            True,
            True,
            _ops(),
        )

        self.assertIn("int out_ctx_count = ctx_obj.count;", lines)
        self.assertIn("int out_ctx_base_iface_count = ctx_obj.base.iface_count;", lines)
        self.assertNotIn("int out_ctx_handlers = ctx_obj.handlers;", lines)
        self.assertNotIn("int out_ctx_base_name = ctx_obj.base.name;", lines)
        self.assertNotIn("out_ctx_handlers", outputs)
        self.assertNotIn("out_ctx_base_name", outputs)

    def test_valid_setup_body_backs_nested_pointer_object_paths(self):
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context", "Connection"},
            struct_fields={
                "Context": {
                    "conn": CParam("conn", "Connection *", "Connection", True, False, False, 0),
                },
                "Connection": {
                    "state": CParam("state", "int", "int", False, False, False, 0),
                },
            },
        )
        behavior = ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"])

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            catalog,
            None,
            ["ctx->conn->state = 1;"],
            None,
            False,
            False,
            _ops(),
            object_paths=[ObjectPathFact("ctx", ("conn", "state"), "Context *", "int")],
        )

        self.assertLess(lines.index("Connection ctx_conn;"), lines.index("ctx->conn->state = 1;"))
        self.assertIn("memset(&ctx_conn, 0, sizeof(ctx_conn));", lines)
        self.assertIn("ctx_obj.conn = &ctx_conn;", lines)

    def test_valid_setup_body_backs_owner_for_nested_function_pointer_path(self):
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context", "Runner"},
            struct_fields={
                "Context": {
                    "runner": CParam("runner", "Runner *", "Runner", True, False, False, 0),
                },
                "Runner": {
                    "handler": CParam("handler", "Handler handler", "Handler", False, False, False, 0),
                },
            },
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            catalog,
            None,
            ["ctx->runner->handler = kleva_stub_Handler;"],
            None,
            False,
            False,
            _ops(),
            object_paths=[ObjectPathFact("ctx", ("runner", "handler"), "Context *", "Handler")],
        )

        self.assertLess(lines.index("Runner ctx_runner;"), lines.index("ctx->runner->handler = kleva_stub_Handler;"))
        self.assertIn("memset(&ctx_runner, 0, sizeof(ctx_runner));", lines)
        self.assertIn("ctx_obj.runner = &ctx_runner;", lines)

    def test_valid_setup_body_uses_required_paths_in_pointer_setup(self):
        ops = BodyGenOps(
            **{
                **_ops().__dict__,
                "pointer_argument_setup": pointer_argument_setup,
                "unique_name": unique_name,
            }
        )
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context", "Connection", "Unused"},
            struct_fields={
                "Context": {
                    "conn": CParam("conn", "Connection *", "Connection", True, False, False, 0),
                    "unused": CParam("unused", "Unused *", "Unused", True, False, False, 0),
                },
                "Connection": {
                    "state": CParam("state", "int", "int", False, False, False, 0),
                },
                "Unused": {},
            },
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            catalog,
            None,
            ["ctx->conn->state = 1;"],
            None,
            False,
            False,
            ops,
            object_paths=[ObjectPathFact("ctx", ("conn", "state"), "Context *", "int")],
        )

        self.assertIn("Connection ctx_conn;", lines)
        self.assertIn("ctx.conn = &ctx_conn;", lines)
        self.assertNotIn("Unused ctx_unused;", lines)
        self.assertEqual(lines.count("Connection ctx_conn;"), 1)

    def test_valid_setup_body_backs_terminal_pointer_array_slot(self):
        func = CFunction(
            name="pop",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("queue", "Queue *", "Queue", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Queue", "Item"},
            struct_fields={
                "Queue": {
                    "items": CParam("items", "Item **items", "Item", True, False, False, 0, pointer_depth=2),
                },
                "Item": {},
            },
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["queue"],
            ACSLBehavior(name="valid", assumes=[r"\valid(queue)"]),
            None,
            catalog,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            object_paths=[ObjectPathFact("queue", ("items",), "Queue *", "Item **")],
        )

        self.assertIn("Item queue_items_0;", lines)
        self.assertIn("memset(&queue_items_0, 0, sizeof(queue_items_0));", lines)
        self.assertIn("queue_obj.items[0] = &queue_items_0;", lines)

    def test_failure_behavior_does_not_force_constructor_for_owned_pointer(self):
        captured = {}
        ops = BodyGenOps(
            scalar_bounds={"int": (0, 10)},
            default_shaping_features=frozenset(),
            scalar_values_from_assumptions=lambda _assumes: {},
            extract_result_value=lambda ensures: -1 if any("-1" in e for e in ensures) else None,
            extract_non_null_params=lambda _assumes: [],
            extract_nonzero_params=lambda _assumes: [],
            extract_null_params=lambda _assumes: [],
            extract_valid_params=lambda _assumes: [],
            is_void_star=lambda p: False,
            pointer_argument_setup=lambda p, *_args, **kwargs: (
                captured.update(kwargs) or [f"{p.base_type} {p.name}_obj;"],
                f"&{p.name}_obj",
                [],
            ),
            needs_len_data_shape=lambda *_args: False,
            append_len_data_shape=lambda _lines, _arg: None,
            param_ref_from_arg=param_ref_from_arg,
            function_frees_param=lambda *_args: False,
            function_takes_param_ownership=lambda *_args: True,
            function_accepts_null_param=lambda *_args: False,
            function_returns_owned_pointer=lambda _func: False,
            lookup_free_fn=lambda *_args: None,
            assumption_setup_lines=lambda *_args: [],
            source_for_branch_shaping=lambda source_text, _func_name: source_text or "",
            void_param_cast_types=lambda *_args: {},
            unique_name=lambda base, _used: base,
            function_pointer_stub_preamble=lambda _fp_decl: [],
            function_pointer_stub_name=lambda name: f"{name}_stub",
            rewrite_setup_with_param_args=lambda lines, _param_args: lines,
            safe_c_name=lambda value: value,
        )
        func = CFunction(
            name="insert",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("item", "Item *", "Item", True, False, False, 0)],
        )
        behavior = ACSLBehavior(name="full", assumes=[r"\valid(item)"], ensures=[r"\result == -1"])

        gen_valid_setup_body(
            func,
            ["item"],
            behavior,
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
        )

        self.assertFalse(captured["prefer_constructor"])

    def test_direct_free_parameter_prefers_raw_heap(self):
        captured = {}
        ops = BodyGenOps(
            scalar_bounds={"int": (0, 10)},
            default_shaping_features=frozenset(),
            scalar_values_from_assumptions=lambda _assumes: {},
            extract_result_value=lambda _ensures: None,
            extract_non_null_params=lambda _assumes: [],
            extract_nonzero_params=lambda _assumes: [],
            extract_null_params=lambda _assumes: [],
            extract_valid_params=lambda _assumes: [],
            is_void_star=lambda p: False,
            pointer_argument_setup=lambda p, *_args, **kwargs: (
                captured.update(kwargs) or [f"{p.base_type} *{p.name} = malloc(sizeof({p.base_type}));"],
                p.name,
                [],
            ),
            needs_len_data_shape=lambda *_args: False,
            append_len_data_shape=lambda _lines, _arg: None,
            param_ref_from_arg=param_ref_from_arg,
            function_frees_param=lambda *_args: True,
            function_takes_param_ownership=lambda *_args: False,
            function_accepts_null_param=lambda *_args: False,
            function_returns_owned_pointer=lambda _func: False,
            lookup_free_fn=lambda *_args: None,
            assumption_setup_lines=lambda *_args: [],
            source_for_branch_shaping=lambda source_text, _func_name: source_text or "",
            void_param_cast_types=lambda *_args: {},
            unique_name=lambda base, _used: base,
            function_pointer_stub_preamble=lambda _fp_decl: [],
            function_pointer_stub_name=lambda name: f"{name}_stub",
            rewrite_setup_with_param_args=lambda lines, _param_args: lines,
            safe_c_name=lambda value: value,
        )
        func = CFunction(
            name="consume",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("item", "Item *", "Item", True, False, False, 0)],
        )

        gen_valid_setup_body(
            func,
            ["item"],
            ACSLBehavior(name="valid", assumes=[r"\valid(item)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
        )

        self.assertTrue(captured["prefer_raw_heap"])

    def test_ir_consumed_parameter_prefers_raw_heap(self):
        captured = {}
        regex_free_calls = []
        regex_transfer_calls = []
        regex_null_calls = []
        ops = _ops()
        ops = BodyGenOps(
            **{
                **ops.__dict__,
                "function_frees_param": lambda *_args: regex_free_calls.append(_args) or False,
                "function_takes_param_ownership": lambda *_args: regex_transfer_calls.append(_args) or False,
                "function_accepts_null_param": lambda *_args: regex_null_calls.append(_args) or False,
                "pointer_argument_setup": lambda p, *_args, **kwargs: (
                    captured.update(kwargs) or [f"{p.base_type} *{p.name} = malloc(sizeof({p.base_type}));"],
                    p.name,
                    [f"free({p.name});"],
                ),
            }
        )
        func = CFunction(
            name="consume",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("item", "Item *", "Item", True, False, False, 0)],
        )

        _lines, _outputs, cleanup, _preamble = gen_valid_setup_body(
            func,
            ["item"],
            ACSLBehavior(name="valid", assumes=[r"\valid(item)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
            OwnershipSummary({"item": CONSUMED}, False, {"item"}),
        )

        self.assertTrue(captured["prefer_raw_heap"])
        self.assertTrue(captured["suppress_constructor_guard"])
        self.assertEqual(regex_free_calls, [])
        self.assertEqual(regex_transfer_calls, [])
        self.assertEqual(regex_null_calls, [])
        self.assertEqual(cleanup, [])

    def test_ir_transferred_parameter_suppresses_cleanup(self):
        regex_free_calls = []
        regex_transfer_calls = []
        ops = _ops()
        ops = BodyGenOps(
            **{
                **ops.__dict__,
                "function_frees_param": lambda *_args: regex_free_calls.append(_args) or False,
                "function_takes_param_ownership": lambda *_args: regex_transfer_calls.append(_args) or False,
                "pointer_argument_setup": lambda p, *_args, **_kwargs: (
                    [f"{p.base_type} {p.name}_obj;"],
                    f"&{p.name}_obj",
                    [f"cleanup_{p.name}();"],
                ),
            }
        )
        func = CFunction(
            name="store",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("item", "Item *", "Item", True, False, False, 0)],
        )

        _lines, _outputs, cleanup, _preamble = gen_valid_setup_body(
            func,
            ["item"],
            ACSLBehavior(name="valid", assumes=[r"\valid(item)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
            OwnershipSummary({"item": TRANSFERRED}, False),
        )

        self.assertEqual(regex_free_calls, [])
        self.assertEqual(regex_transfer_calls, [])
        self.assertEqual(cleanup, [])

    def test_ir_returned_owned_pointer_adds_cleanup(self):
        regex_return_calls = []
        ops = _ops()
        ops = BodyGenOps(
            **{
                **ops.__dict__,
                "lookup_free_fn": lambda type_name, _source, _decls: f"{type_name.lower()}_free",
                "function_returns_owned_pointer": lambda _func: regex_return_calls.append(_func) or False,
            }
        )
        func = CFunction(
            name="make_anything",
            return_type="Item *",
            return_base="Item",
            return_is_pointer=True,
            params=[],
        )

        _lines, _outputs, cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            ACSLBehavior(name="valid", assumes=[]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
            OwnershipSummary({}, True),
        )

        self.assertEqual(regex_return_calls, [])
        self.assertEqual(cleanup, ["if (out_ret) item_free(out_ret);"])

    def test_ir_buffer_param_adds_len_data_shape_without_source_detector(self):
        source_detector_calls = []
        appended = []
        ops = _ops()
        ops = BodyGenOps(
            **{
                **ops.__dict__,
                "needs_len_data_shape": lambda *args: source_detector_calls.append(args) or False,
                "append_len_data_shape": lambda lines, arg: appended.append(arg) or lines.append(f"shape({arg});"),
            }
        )
        func = CFunction(
            name="consume",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("buf", "Buffer *", "Buffer", True, False, False, 0)],
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["buf"],
            ACSLBehavior(name="valid", assumes=[r"\valid(buf)"]),
            "int consume(Buffer *buf) { return buf->len; }",
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
            OwnershipSummary({"buf": "borrowed"}, False, set(), {"buf"}),
        )

        self.assertIn("shape(&buf_obj);", lines)
        self.assertEqual(appended, ["&buf_obj"])
        self.assertEqual(source_detector_calls, [])

    def test_ir_void_cast_type_shapes_void_param_without_source_detector(self):
        regex_cast_calls = []
        func = CFunction(
            name="dispatch",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "void *", "void", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context"},
            struct_fields={"Context": {"ready": CParam("ready", "int", "int", False, False, False, 0)}},
        )
        ops = _ops()
        ops = BodyGenOps(
            **{
                **ops.__dict__,
                "void_param_cast_types": lambda *_args: regex_cast_calls.append(_args) or {},
            }
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"ctx != \null"]),
            "int dispatch(void *ctx) { Context *typed = (Context *)ctx; return typed->ready; }",
            catalog,
            None,
            None,
            None,
            False,
            False,
            ops,
            OwnershipSummary({"ctx": "borrowed"}, False, set(), set(), {"ctx": "Context"}),
        )

        self.assertIn("Context ctx_Context;", lines)
        self.assertIn("memset(&ctx_Context, 0, sizeof(ctx_Context));", lines)
        self.assertIn("int out_ret = dispatch(&ctx_Context);", lines)
        self.assertEqual(regex_cast_calls, [])


if __name__ == "__main__":
    unittest.main()
