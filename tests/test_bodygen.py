import unittest
from dataclasses import replace

from kleva.acsl import ACSLBehavior
from kleva.acsl_contract import scalar_values_from_assumptions
from kleva.ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from kleva.bodygen import (
    BodyGenOps,
    gen_null_setup_body,
    gen_valid_setup_body,
    param_ref_from_arg,
)
from kleva.fixtures.construction import pointer_argument_setup, unique_name
from kleva.fixtures.requirements import object_path_value
from kleva.ir.model import AssignmentStmt, BinaryOp, DeclarationStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, VarRef
from kleva.shaping.candidates import BranchFact, ObjectPathFact, PostStateFact
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

        result = gen_null_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            None,
            None,
            None,
            _ops(),
        )
        lines, outputs, cleanup, preamble = result

        self.assertIn("int out_ret = run(NULL, 0);", lines)
        self.assertEqual(result.inputs, [])
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

    def test_valid_setup_body_uses_assignable_local_for_function_pointer_param(self):
        func = CFunction(
            name="register_handler",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("handler", "Handler", "Handler", False, False, False, 0)],
        )
        type_catalog = CTypeCatalog(
            function_pointers={
                "Handler": CFunctionPointerTypedef(
                    "Handler",
                    "int",
                    [CParam("value", "int value", "int", False, False, False, 0)],
                )
            }
        )
        behavior = ACSLBehavior(name="null_handler_branch", assumes=[])

        result = gen_valid_setup_body(
            func,
            [],
            behavior,
            None,
            type_catalog,
            None,
            ["handler = 0;"],
            {"function-pointers"},
            False,
            False,
            _ops(),
        )

        body = "\n".join(result.body)
        self.assertIn("Handler handler = Handler_stub;", body)
        self.assertIn("handler = 0;", body)
        self.assertIn("int out_ret = register_handler(handler);", body)
        self.assertNotIn("Handler_stub = 0;", body)

    def test_valid_setup_body_keeps_exact_scalar_assumption_concrete(self):
        func = CFunction(
            name="make",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("capacity", "size_t capacity", "size_t", False, False, False, 0)],
        )
        behavior = ACSLBehavior(
            name="zero",
            assumes=["capacity == 0"],
            ensures=[r"\result == -1"],
        )
        ops = replace(
            _ops(),
            scalar_bounds={"size_t": (1, 268435455)},
            scalar_values_from_assumptions=scalar_values_from_assumptions,
        )

        result = gen_valid_setup_body(
            func,
            [],
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

        self.assertIn("int out_ret = make(0);", result.body)
        self.assertEqual(result.inputs, [])

    def test_valid_setup_body_does_not_rewrite_literal_call_arg_as_lvalue(self):
        from kleva.synth_ops import _rewrite_setup_with_param_args

        rewritten = _rewrite_setup_with_param_args(
            ["capacity = 0;", "other = capacity;"],
            {"capacity": "0"},
        )

        self.assertEqual(rewritten, ["other = 0;"])

    def test_valid_setup_body_suppresses_destructor_after_nested_pointer_shaping(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *ctx", "Context", True, False, False, 0)],
        )
        behavior = ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"])
        ops = replace(
            _ops(),
            pointer_argument_setup=lambda p, *_args, **_kwargs: (
                [f"{p.base_type} *{p.name} = make_{p.name}();"],
                p.name,
                [f"free_{p.name}({p.name});"],
            ),
        )

        result = gen_valid_setup_body(
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
            ops,
            object_paths=[ObjectPathFact("ctx", ("queue",))],
        )

        self.assertEqual(result.cleanup, [])

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

        result = gen_valid_setup_body(
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
        lines, _outputs, _cleanup, _preamble = result

        self.assertIn("int out_ret = run(NULL, 0);", lines)
        self.assertEqual(result.inputs, [])

    def test_valid_setup_body_uses_mutable_scalar_when_candidate_assigns_it(self):
        func = CFunction(
            name="run",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("limit", "int limit", "int", False, False, False, 0)],
        )

        result = gen_valid_setup_body(
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
        lines, _outputs, _cleanup, _preamble = result

        self.assertEqual(result.inputs[0]["ktest_name"], "limit")
        self.assertEqual(result.inputs[0]["bounds"], (0, 10))
        self.assertNotIn("int limit = 0;", lines)
        self.assertIn("limit = 1;", lines)
        self.assertIn("int out_ret = run(limit);", lines)
        self.assertIn("int out_limit_nonzero = (limit != 0);", lines)

    def test_source_shape_oracle_for_nonvoid_uses_return_value(self):
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
            True,
            False,
            _ops(),
        )

        self.assertIn("int out_ret = run();", lines)
        self.assertNotIn("int out_ok = 1;", lines)
        self.assertEqual(outputs, ["out_ret"])

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

    def test_valid_setup_body_uses_completion_witness_for_void_without_observable(self):
        func = CFunction(
            name="run",
            return_type="void",
            return_base="void",
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
        )

        self.assertIn("run();", lines)
        self.assertIn("int out_call_completed = 1;", lines)
        self.assertNotIn("out_missing_oracle", outputs)
        self.assertEqual(outputs, ["out_call_completed"])

    def test_valid_setup_body_avoids_weak_oracle_when_post_state_witness_exists(self):
        func = CFunction(
            name="run",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            post_state_facts=[PostStateFact("ctx->ready", "==", "1")],
        )

        self.assertIn("run(&ctx_obj);", lines)
        self.assertIn("/* oracle-deferred: post-call witness will be emitted below */", lines)
        self.assertNotIn("out_missing_oracle", outputs)
        self.assertTrue(any(output.startswith("out_post_state_") for output in outputs))

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

    def test_void_struct_param_adds_generic_field_witnesses(self):
        func = CFunction(
            name="touch",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context"},
            struct_fields={
                "Context": {
                    "count": CParam("count", "int", "int", False, False, False, 0),
                    "items": CParam("items", "Item items[4]", "Item", True, False, True, 4),
                },
            },
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            catalog,
            None,
            None,
            None,
            False,
            False,
            _ops(),
        )

        self.assertLess(lines.index("touch(&ctx_obj);"), lines.index("int out_ctx_count = ctx_obj.count;"))
        self.assertIn("out_ctx_count", outputs)
        self.assertNotIn("out_ctx_items", outputs)
        self.assertNotIn("out_missing_oracle", outputs)

    def test_valid_setup_body_adds_old_state_delta_postcondition_witness(self):
        func = CFunction(
            name="advance",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("ctx", "Context *", "Context", True, False, False, 0),
                CParam("delta", "int delta", "int", False, False, False, 0),
            ],
        )
        behavior = ACSLBehavior(
            name="ok",
            assumes=[r"\valid(ctx)", "delta == 2"],
            ensures=[r"ctx->count == \old(ctx->count) + delta"],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
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

        self.assertLess(
            lines.index("uintptr_t kleva_old_ctx_count = (uintptr_t)(ctx_obj.count);"),
            lines.index("int out_ret = advance(&ctx_obj, 0);"),
        )
        self.assertIn(
            "int out_post_ctx_count_delta = ((uintptr_t)(ctx_obj.count) == (kleva_old_ctx_count + (uintptr_t)(0)));",
            lines,
        )
        self.assertIn("out_post_ctx_count_delta", outputs)

    def test_valid_setup_body_adds_old_state_pointer_postcondition_witness(self):
        func = CFunction(
            name="retreat",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("ctx", "Context *", "Context", True, False, False, 0),
                CParam("amount", "size_t amount", "size_t", False, False, False, 0),
            ],
        )
        behavior = ACSLBehavior(
            name="ok",
            assumes=[r"\valid(ctx)", "amount == 4"],
            ensures=[r"ctx->data == \old(ctx->data) - amount"],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
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

        self.assertLess(
            lines.index("uintptr_t kleva_old_ctx_data = (uintptr_t)(ctx_obj.data);"),
            lines.index("int out_ret = retreat(&ctx_obj, 0);"),
        )
        self.assertIn(
            "int out_post_ctx_data_amount = ((uintptr_t)(ctx_obj.data) == (kleva_old_ctx_data - (uintptr_t)(0)));",
            lines,
        )
        self.assertIn("out_post_ctx_data_amount", outputs)

    def test_valid_setup_body_skips_old_state_witness_for_consumed_param(self):
        func = CFunction(
            name="destroy",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        ownership = OwnershipSummary(param_behavior={"ctx": CONSUMED}, returns_owned_pointer=False)
        behavior = ACSLBehavior(
            name="valid",
            assumes=[r"\valid(ctx)"],
            ensures=[r"ctx->count == \old(ctx->count)"],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
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
            ownership=ownership,
        )

        self.assertNotIn("kleva_old_ctx_count", "\n".join(lines))
        self.assertNotIn("out_post_ctx_count", outputs)

    def test_valid_setup_body_skips_old_state_witness_for_null_branch_root(self):
        func = CFunction(
            name="maybe_destroy",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        behavior = ACSLBehavior(
            name="null_branch",
            assumes=[r"\valid(ctx)"],
            ensures=[r"ctx->count == \old(ctx->count)"],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            None,
            None,
            ["ctx = 0;"],
            None,
            False,
            False,
            _ops(),
            branch_facts=[BranchFact("ctx", "==", "0")],
        )

        self.assertNotIn("kleva_old_ctx_count", "\n".join(lines))
        self.assertNotIn("out_post_ctx_count", outputs)

    def test_valid_setup_body_adds_ir_direct_assignment_witness(self):
        func = CFunction(
            name="mark_ready",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        function_ir = FunctionIR(
            "mark_ready",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "ready", "int"),
                    IntLiteral(1),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertLess(
            lines.index("int out_ret = mark_ready(&ctx_obj);"),
            lines.index("int out_ir_post_ctx_ready = ((uintptr_t)(ctx_obj.ready) == (uintptr_t)(1));"),
        )
        self.assertIn("out_ir_post_ctx_ready", outputs)

    def test_valid_setup_body_does_not_add_ir_witness_for_branch_assignment(self):
        func = CFunction(
            name="maybe_mark",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        function_ir = FunctionIR(
            "maybe_mark",
            [
                IfStmt(
                    VarRef("condition"),
                    [
                        AssignmentStmt(
                            FieldAccess(VarRef("ctx", "Context *"), "ready", "int"),
                            IntLiteral(1),
                        )
                    ],
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertNotIn("out_ir_post_ctx_ready", "\n".join(lines))
        self.assertNotIn("out_ir_post_ctx_ready", outputs)

    def test_valid_setup_body_uses_old_target_for_ir_compound_assignment_witness(self):
        func = CFunction(
            name="add_total",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("ctx", "Context *", "Context", True, False, False, 0),
                CParam("amount", "int", "int", False, False, False, 0),
            ],
        )
        target = FieldAccess(VarRef("ctx", "Context *"), "total", "int")
        function_ir = FunctionIR(
            "add_total",
            [
                AssignmentStmt(
                    target,
                    BinaryOp("+", target, IntLiteral(1, "int"), "int"),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx", "amount"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertLess(
            lines.index("uintptr_t kleva_old_ir_ctx_total = (uintptr_t)(ctx_obj.total);"),
            lines.index("int out_ret = add_total(&ctx_obj, 0);"),
        )
        self.assertIn(
            "int out_ir_post_ctx_total = ((uintptr_t)(ctx_obj.total) == (uintptr_t)((kleva_old_ir_ctx_total + 1)));",
            lines,
        )
        self.assertIn("out_ir_post_ctx_total", outputs)

    def test_valid_setup_body_skips_ir_witness_value_that_mentions_consumed_param(self):
        func = CFunction(
            name="receive",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("iface", "Interface *", "Interface", True, False, False, 0),
                CParam("frame", "Packet *", "Packet", True, False, False, 0),
            ],
        )
        function_ir = FunctionIR(
            "receive",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("iface", "Interface *"), "rx_bytes", "size_t"),
                    BinaryOp(
                        "+",
                        FieldAccess(VarRef("iface", "Interface *"), "rx_bytes", "size_t"),
                        FieldAccess(VarRef("frame", "Packet *"), "len", "size_t"),
                        "size_t",
                    ),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["iface", "frame"],
            ACSLBehavior(name="valid", assumes=[r"\valid(iface)", r"\valid(frame)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            ownership=OwnershipSummary({"frame": CONSUMED}, False),
            function_ir=function_ir,
        )

        rendered = "\n".join(lines)
        self.assertNotIn("kleva_old_ir_iface_rx_bytes", rendered)
        self.assertNotIn("out_ir_post_iface_rx_bytes", rendered)
        self.assertNotIn("out_ir_post_iface_rx_bytes", outputs)

    def test_valid_setup_body_skips_ir_witness_with_callee_local_rhs(self):
        func = CFunction(
            name="grow_queue",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("eq", "EventQueue *", "EventQueue", True, False, False, 0)],
        )
        function_ir = FunctionIR(
            "grow_queue",
            [
                DeclarationStmt("new_events", "Event **", None),
                AssignmentStmt(
                    FieldAccess(VarRef("eq", "EventQueue *"), "events", "Event **"),
                    VarRef("new_events"),
                ),
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["eq"],
            ACSLBehavior(name="valid", assumes=[r"\valid(eq)"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertNotIn("new_events", "\n".join(lines))
        self.assertNotIn("out_ir_post_eq_events", outputs)

    def test_valid_setup_body_skips_ir_assignment_witness_for_failure_result(self):
        func = CFunction(
            name="fail_before_store",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        function_ir = FunctionIR(
            "fail_before_store",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "ready", "int"),
                    IntLiteral(1),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="failure", assumes=[r"\valid(ctx)"], ensures=[r"\result == -1"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertNotIn("out_ir_post_ctx_ready", "\n".join(lines))
        self.assertNotIn("out_ir_post_ctx_ready", outputs)

    def test_valid_setup_body_skips_ir_assignment_witness_for_source_branch_candidate(self):
        func = CFunction(
            name="branch_before_store",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        function_ir = FunctionIR(
            "branch_before_store",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "ready", "int"),
                    IntLiteral(1),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="branch_candidate", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            _ops(),
            function_ir=function_ir,
        )

        self.assertNotIn("out_ir_post_ctx_ready", "\n".join(lines))
        self.assertNotIn("out_ir_post_ctx_ready", outputs)

    def test_valid_setup_body_renders_typed_post_state_fact_witness(self):
        func = CFunction(
            name="mark_ready",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="branch", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            _ops(),
            post_state_facts=[PostStateFact("ctx->ready", "==", "1")],
        )

        self.assertIn(
            "int out_post_state_ctx_ready_1 = ((uintptr_t)(ctx_obj.ready) == (uintptr_t)(1));",
            lines,
        )
        self.assertIn("out_post_state_ctx_ready_1", outputs)

    def test_valid_setup_body_skips_post_state_witness_with_unresolved_loop_index(self):
        func = CFunction(
            name="fill_slot",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("table", "Table *", "Table", True, False, False, 0)],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["table"],
            ACSLBehavior(name="valid", assumes=[r"\valid(table)"]),
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            _ops(),
            post_state_facts=[PostStateFact("table->items[i].valid", "==", "1")],
        )

        rendered = "\n".join(lines)
        self.assertNotIn("table_obj.items[i].valid", rendered)
        self.assertNotIn("out_post_state_table_items_i_valid_1", outputs)

    def test_valid_setup_body_skips_post_state_witness_for_null_branch_root(self):
        func = CFunction(
            name="mark_ready",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="branch", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            ["ctx = 0;"],
            None,
            True,
            False,
            _ops(),
            post_state_facts=[PostStateFact("ctx->ready", "==", "1")],
            branch_facts=[BranchFact("ctx", "==", "0")],
        )

        self.assertNotIn("out_post_state_ctx_ready_1", "\n".join(lines))
        self.assertNotIn("out_post_state_ctx_ready_1", outputs)

    def test_valid_setup_body_uses_old_target_for_typed_post_state_fact_witness(self):
        func = CFunction(
            name="add_count",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[
                CParam("ctx", "Context *", "Context", True, False, False, 0),
                CParam("n", "int", "int", False, False, False, 0),
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx", "n"],
            ACSLBehavior(name="branch", assumes=[r"\valid(ctx)"]),
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            _ops(),
            post_state_facts=[PostStateFact("ctx->count", "==", "(ctx->count + n)")],
        )

        self.assertLess(
            lines.index("uintptr_t kleva_old_post_state_ctx_count = (uintptr_t)(ctx_obj.count);"),
            lines.index("add_count(&ctx_obj, 0);"),
        )
        self.assertIn(
            "int out_post_state_ctx_count_ctx_count_n = ((uintptr_t)(ctx_obj.count) == (uintptr_t)((kleva_old_post_state_ctx_count + 0)));",
            lines,
        )
        self.assertIn("out_post_state_ctx_count_ctx_count_n", outputs)

    def test_valid_setup_body_skips_post_state_witness_value_that_mentions_consumed_param(self):
        func = CFunction(
            name="receive",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[
                CParam("iface", "Interface *", "Interface", True, False, False, 0),
                CParam("frame", "Packet *", "Packet", True, False, False, 0),
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["iface", "frame"],
            ACSLBehavior(name="valid", assumes=[r"\valid(iface)", r"\valid(frame)"]),
            None,
            None,
            None,
            None,
            None,
            True,
            False,
            _ops(),
            ownership=OwnershipSummary({"frame": CONSUMED}, False),
            post_state_facts=[PostStateFact("iface->rx_bytes", "==", "(iface->rx_bytes + frame->len)")],
        )

        rendered = "\n".join(lines)
        self.assertNotIn("kleva_old_post_state_iface_rx_bytes", rendered)
        self.assertNotIn("out_post_state_iface_rx_bytes_iface_rx_bytes_frame_len", rendered)
        self.assertNotIn("out_post_state_iface_rx_bytes_iface_rx_bytes_frame_len", outputs)

    def test_valid_setup_body_does_not_replace_field_name_with_scalar_arg(self):
        func = CFunction(
            name="set_up",
            return_type="void",
            return_base="void",
            return_is_pointer=False,
            params=[
                CParam("iface", "Interface *", "Interface", True, False, False, 0),
                CParam("up", "int", "int", False, False, False, 0),
            ],
        )
        function_ir = FunctionIR(
            "set_up",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("iface", "Interface *"), "up", "int"),
                    VarRef("up", "int"),
                )
            ],
        )

        lines, outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["iface", "up"],
            ACSLBehavior(name="valid", assumes=[r"iface != \null"]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            _ops(),
            function_ir=function_ir,
        )

        rendered = "\n".join(lines)
        self.assertIn("iface_obj.up", rendered)
        self.assertNotIn("iface_obj.0", rendered)
        self.assertIn("out_ir_post_iface_up", outputs)

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

        self.assertLess(
            lines.index("Connection *ctx_conn = malloc(sizeof(*ctx_conn));"),
            lines.index("ctx->conn->state = 1;"),
        )
        self.assertIn("if (!ctx_conn) return 0;", lines)
        self.assertIn("memset(ctx_conn, 0, sizeof(*ctx_conn));", lines)
        self.assertIn("ctx_obj.conn = ctx_conn;", lines)

    def test_valid_setup_body_does_not_overwrite_constructor_backed_object_path(self):
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context", "Queue"},
            struct_fields={
                "Context": {
                    "queue": CParam("queue", "Queue *", "Queue", True, False, False, 0),
                },
                "Queue": {
                    "count": CParam("count", "int", "int", False, False, False, 0),
                },
            },
        )
        behavior = ACSLBehavior(name="valid", assumes=[r"\valid(ctx)"])
        ops = replace(
            _ops(),
            pointer_argument_setup=lambda p, *_args, **_kwargs: (
                [
                    "Context * context_create(void);",
                    "Context *ctx = context_create();",
                    "__GUARD__(ctx)",
                ],
                "ctx",
                [],
            ),
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            catalog,
            {"context_create": CFunction("context_create", "Context *", "Context", True, [])},
            None,
            None,
            False,
            False,
            ops,
            object_paths=[ObjectPathFact("ctx", ("queue", "count"), "Context *", "int")],
        )

        rendered = "\n".join(lines)
        self.assertIn("Context *ctx = context_create();", rendered)
        self.assertNotIn("Queue ctx_queue;", rendered)
        self.assertNotIn("ctx->queue = &ctx_queue;", rendered)

    def test_valid_setup_body_uses_typed_object_path_requirements(self):
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

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(name="valid", assumes=[r"\valid(ctx)", r"ctx->conn->state >= 2"]),
            None,
            catalog,
            None,
            None,
            None,
            False,
            False,
            _ops(),
        )

        self.assertLess(
            lines.index("Connection *ctx_conn = malloc(sizeof(*ctx_conn));"),
            lines.index("ctx_obj.conn->state = 2;"),
        )
        self.assertIn("ctx_obj.conn = ctx_conn;", lines)
        self.assertIn("ctx_obj.conn->state = 2;", lines)

    def test_valid_setup_body_reports_conflicting_fixture_requirements(self):
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context"},
            struct_fields={
                "Context": {
                    "state": CParam("state", "int", "int", False, False, False, 0),
                },
            },
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["ctx"],
            ACSLBehavior(
                name="valid",
                assumes=[
                    r"\valid(ctx)",
                    r"ctx == \null",
                    r"ctx->state == 1",
                    r"ctx->state == 2",
                ],
            ),
            None,
            catalog,
            None,
            None,
            None,
            False,
            False,
            _ops(),
        )

        self.assertIn(
            "/* fixture-failed: conflicting constraints: ctx is both null and valid */",
            lines,
        )
        self.assertIn(
            "/* fixture-failed: conflicting constraints: ctx->state == 1 and ctx->state == 2 */",
            lines,
        )
        self.assertNotIn("ctx_obj.state = 1;", lines)
        self.assertNotIn("ctx_obj.state = 2;", lines)

    def test_valid_setup_body_reports_unsupported_fixture_relation(self):
        func = CFunction(
            name="step",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("ctx", "Context *", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Context"},
            struct_fields={
                "Context": {
                    "state": CParam("state", "int", "int", False, False, False, 0),
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
            None,
            None,
            False,
            False,
            _ops(),
            fixture_requirements=[object_path_value("ctx->state", "==", "bad\\value")],
        )

        self.assertIn(
            "/* fixture-failed: unsupported pointer relation: ctx->state == bad\\value */",
            lines,
        )

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

        self.assertLess(
            lines.index("Runner *ctx_runner = malloc(sizeof(*ctx_runner));"),
            lines.index("ctx->runner->handler = kleva_stub_Handler;"),
        )
        self.assertIn("memset(ctx_runner, 0, sizeof(*ctx_runner));", lines)
        self.assertIn("ctx_obj.runner = ctx_runner;", lines)

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

    def test_valid_setup_body_materializes_pointer_param_mentioned_by_candidate(self):
        ops = BodyGenOps(
            **{
                **_ops().__dict__,
                "pointer_argument_setup": pointer_argument_setup,
                "unique_name": unique_name,
            }
        )
        func = CFunction(
            name="check",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("data", "uint8_t *", "uint8_t", True, False, False, 0)],
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            ACSLBehavior(name="candidate", assumes=[]),
            None,
            None,
            None,
            ["data = 0;"],
            None,
            False,
            False,
            ops,
        )

        self.assertIn("uint8_t data_buf[64];", lines)
        self.assertIn("uint8_t * data = data_buf;", lines)
        self.assertIn("data = 0;", lines)
        self.assertIn("int out_ret = check(data);", lines)

    def test_valid_setup_body_materializes_void_pointer_param_mentioned_by_candidate(self):
        ops = BodyGenOps(
            **{
                **_ops().__dict__,
                "is_void_star": lambda p: p.base_type == "void" and p.is_pointer,
            }
        )
        func = CFunction(
            name="check",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("data", "const void *data", "void", True, False, False, 0)],
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            [],
            ACSLBehavior(name="candidate", assumes=[]),
            None,
            None,
            None,
            ["data = 0;"],
            None,
            False,
            False,
            ops,
        )

        self.assertIn("uint8_t data_buf[256];", lines)
        self.assertIn("const void *data = data_buf;", lines)
        self.assertIn("data = 0;", lines)
        self.assertIn("int out_ret = check(data);", lines)

    def test_valid_setup_body_materializes_valid_void_pointer_param(self):
        ops = BodyGenOps(
            **{
                **_ops().__dict__,
                "is_void_star": lambda p: p.base_type == "void" and p.is_pointer,
            }
        )
        func = CFunction(
            name="check",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("data", "const void *data", "void", True, False, False, 0)],
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["data"],
            ACSLBehavior(name="valid", assumes=[]),
            None,
            None,
            None,
            None,
            None,
            False,
            False,
            ops,
        )

        self.assertIn("uint8_t data_buf[256];", lines)
        self.assertIn("const void *data = data_buf;", lines)
        self.assertIn("int out_ret = check(data);", lines)

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

        self.assertIn("void *malloc(size_t size);", lines)
        self.assertIn("Item **queue_items_slots = malloc(sizeof(*queue_items_slots));", lines)
        self.assertIn("if (!queue_items_slots) return 0;", lines)
        self.assertIn("memset(queue_items_slots, 0, sizeof(*queue_items_slots));", lines)
        self.assertIn("queue_obj.items = queue_items_slots;", lines)
        self.assertIn("Item *queue_items_0 = malloc(sizeof(*queue_items_0));", lines)
        self.assertIn("if (!queue_items_0) return 0;", lines)
        self.assertIn("memset(queue_items_0, 0, sizeof(*queue_items_0));", lines)
        self.assertIn("queue_obj.items[0] = queue_items_0;", lines)

    def test_valid_setup_body_backs_nested_pointer_array_slot(self):
        from kleva.synth_ops import _rewrite_setup_with_param_args

        func = CFunction(
            name="lookup",
            return_type="int",
            return_base="int",
            return_is_pointer=False,
            params=[CParam("owner", "Owner *", "Owner", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            complete_structs={"Owner", "Item"},
            struct_fields={
                "Owner": {
                    "items": CParam("items", "Item **items", "Item", True, False, False, 0, pointer_depth=2),
                },
                "Item": {
                    "value": CParam("value", "int value", "int", False, False, False, 0),
                },
            },
        )

        lines, _outputs, _cleanup, _preamble = gen_valid_setup_body(
            func,
            ["owner"],
            ACSLBehavior(name="valid", assumes=[r"\valid(owner)"]),
            None,
            catalog,
            None,
            ["owner->items[0]->value = 7;"],
            None,
            False,
            False,
            replace(_ops(), rewrite_setup_with_param_args=_rewrite_setup_with_param_args),
            object_paths=[ObjectPathFact("owner", ("items", "value"), "Owner *", "int")],
        )

        self.assertIn("Item **owner_items_slots = malloc(sizeof(*owner_items_slots));", lines)
        self.assertIn("if (!owner_items_slots) return 0;", lines)
        self.assertIn("memset(owner_items_slots, 0, sizeof(*owner_items_slots));", lines)
        self.assertIn("owner_obj.items = owner_items_slots;", lines)
        self.assertIn("Item *owner_items = malloc(sizeof(*owner_items));", lines)
        self.assertIn("owner_obj.items[0] = owner_items;", lines)
        self.assertIn("owner_obj.items[0]->value = 7;", lines)

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
