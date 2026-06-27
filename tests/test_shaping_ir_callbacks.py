from __future__ import annotations

import unittest

from kleva.ast.model import CFunction, CFunctionPointerTypedef, CParam, CTypeCatalog
from kleva.fixtures.construction import function_pointer_stub_name, function_pointer_stub_preamble
from kleva.ir.model import CallExpr, ExprStmt, FieldAccess, FunctionIR, IfStmt, SourceLocation, SwitchStmt, VarRef
from kleva.shaping.candidates import BranchFact, ObjectPathFact
from kleva.shaping.ir_callbacks import (
    callback_calls_from_ir,
    callback_candidates_from_ir,
    callback_field_exprs_from_ir,
    callback_guard_exprs_from_ir,
)


class IrCallbackShapingTests(unittest.TestCase):
    def test_detects_callback_field_call(self):
        func = FunctionIR(
            "run",
            [ExprStmt(CallExpr("ctx->handler", []))],
        )

        calls = callback_calls_from_ir(func)

        self.assertEqual([call.target_expr for call in calls], ["ctx->handler"])
        self.assertEqual([call.target_expr for call in callback_field_exprs_from_ir(func)], ["ctx->handler"])

    def test_dedupes_repeated_callback_calls(self):
        func = FunctionIR(
            "run",
            [
                ExprStmt(CallExpr("ctx->handler", [])),
                ExprStmt(CallExpr("ctx->handler", [])),
            ],
        )

        self.assertEqual(
            [call.target_expr for call in callback_calls_from_ir(func)],
            ["ctx->handler"],
        )

    def test_detects_nested_callback_field_call(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    VarRef("ready", "int"),
                    [ExprStmt(CallExpr("ctx->handler", []), SourceLocation("sample.c", 12, 13))],
                )
            ],
        )

        calls = callback_field_exprs_from_ir(func)

        self.assertEqual([call.target_expr for call in calls], ["ctx->handler"])
        self.assertEqual(calls[0].loc, SourceLocation("sample.c", 12, 13))

    def test_detects_switch_body_callback_guard(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    VarRef("state", "int"),
                    body=[
                        IfStmt(
                            FieldAccess(VarRef("ctx", "Context *"), "handler", "Handler"),
                            loc=SourceLocation("sample.c", 15, 17),
                        )
                    ],
                )
            ],
        )

        guards = callback_guard_exprs_from_ir(func)

        self.assertEqual([guard.target_expr for guard in guards], ["ctx->handler"])
        self.assertEqual(guards[0].loc, SourceLocation("sample.c", 15, 17))

    def test_detects_callback_guard_expression(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "handler", "Handler"),
                    loc=SourceLocation("sample.c", 9, 5),
                )
            ],
        )

        guards = callback_guard_exprs_from_ir(func)

        self.assertEqual([guard.target_expr for guard in guards], ["ctx->handler"])
        self.assertEqual(guards[0].loc, SourceLocation("sample.c", 9, 5))

    def test_generates_callback_null_and_present_candidates(self):
        func_ir = FunctionIR(
            "run",
            [ExprStmt(CallExpr("ctx->handler", []), SourceLocation("sample.c", 7, 9))],
        )
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [CParam("ctx", "Context *ctx", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            function_pointers={
                "Handler": CFunctionPointerTypedef(
                    "Handler",
                    "void",
                    [CParam("arg", "void *arg", "void", True, False, False, 0)],
                )
            },
            struct_fields={
                "Context": {
                    "handler": CParam("handler", "Handler handler", "Handler", False, False, False, 0)
                }
            },
        )

        candidates = callback_candidates_from_ir(
            func_ir,
            func,
            catalog,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_callback_ctx_handler_null", ["ctx->handler = NULL;"]),
                ("ir_callback_ctx_handler_present", ["ctx->handler = kleva_stub_Handler;"]),
            ],
        )
        self.assertEqual(candidates[1].preamble[0], "static int kleva_stub_Handler_called;")
        self.assertTrue(candidates[1].preamble[1].startswith("static void kleva_stub_Handler"))
        self.assertEqual(candidates[1].witness_setup, ["int out_ctx_handler_called = kleva_stub_Handler_called;"])
        self.assertEqual(candidates[1].extra_outputs, ["out_ctx_handler_called"])
        self.assertEqual(candidates[0].source_location, "sample.c:7:9")
        self.assertEqual(candidates[0].target_branch, "callback ctx->handler null")
        self.assertEqual(candidates[0].branch_facts, [
            BranchFact("ctx->handler", "==", "NULL"),
        ])
        self.assertEqual(candidates[1].branch_facts, [
            BranchFact("ctx->handler", "!=", "NULL"),
        ])

    def test_generates_callback_guard_candidates(self):
        func_ir = FunctionIR(
            "run",
            [
                IfStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "handler", "Handler"),
                    loc=SourceLocation("sample.c", 9, 5),
                )
            ],
        )
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [CParam("ctx", "Context *ctx", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            function_pointers={
                "Handler": CFunctionPointerTypedef("Handler", "void", [])
            },
            struct_fields={
                "Context": {
                    "handler": CParam("handler", "Handler handler", "Handler", False, False, False, 0)
                }
            },
        )

        candidates = callback_candidates_from_ir(
            func_ir,
            func,
            catalog,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_callback_ctx_handler_null", ["ctx->handler = NULL;"]),
                ("ir_callback_ctx_handler_present", ["ctx->handler = kleva_stub_Handler;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:9:5")
        self.assertEqual(candidates[1].extra_outputs, ["out_ctx_handler_called"])

    def test_generates_nested_callback_field_candidates_with_object_path(self):
        func_ir = FunctionIR(
            "run",
            [
                IfStmt(
                    FieldAccess(
                        FieldAccess(VarRef("ctx", "Context *"), "runner", "Runner *"),
                        "handler",
                        "Handler",
                    ),
                    loc=SourceLocation("sample.c", 14, 9),
                )
            ],
        )
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [CParam("ctx", "Context *ctx", "Context", True, False, False, 0)],
        )
        catalog = CTypeCatalog(
            function_pointers={
                "Handler": CFunctionPointerTypedef("Handler", "void", [])
            },
            struct_fields={
                "Context": {
                    "runner": CParam("runner", "Runner *runner", "Runner", True, False, False, 0)
                },
                "Runner": {
                    "handler": CParam("handler", "Handler handler", "Handler", False, False, False, 0)
                },
            },
        )

        candidates = callback_candidates_from_ir(
            func_ir,
            func,
            catalog,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_callback_ctx_runner_handler_null", ["ctx->runner->handler = NULL;"]),
                (
                    "ir_callback_ctx_runner_handler_present",
                    ["ctx->runner->handler = kleva_stub_Handler;"],
                ),
            ],
        )
        self.assertEqual(candidates[1].object_paths, [
            ObjectPathFact("ctx", ("runner", "handler"), "Context *", "Handler")
        ])
        self.assertEqual(candidates[1].extra_outputs, ["out_ctx_runner_handler_called"])
        self.assertEqual(candidates[1].branch_facts, [
            BranchFact("ctx->runner->handler", "!=", "NULL"),
        ])

    def test_generates_direct_function_pointer_parameter_candidates(self):
        func_ir = FunctionIR(
            "run",
            [ExprStmt(CallExpr("handler", []), SourceLocation("sample.c", 11, 5))],
        )
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [CParam("handler", "Handler handler", "Handler", False, False, False, 0)],
        )
        catalog = CTypeCatalog(
            function_pointers={
                "Handler": CFunctionPointerTypedef("Handler", "void", [])
            },
        )

        candidates = callback_candidates_from_ir(
            func_ir,
            func,
            catalog,
            function_pointer_stub_preamble,
            function_pointer_stub_name,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.call_arg_overrides) for candidate in candidates],
            [
                ("ir_callback_handler_null", [], {"handler": "NULL"}),
                ("ir_callback_handler_present", [], {"handler": "kleva_stub_Handler"}),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:11:5")
        self.assertEqual(candidates[0].target_branch, "callback handler null")
        self.assertEqual(candidates[0].branch_facts, [
            BranchFact("handler", "==", "NULL"),
        ])
        self.assertEqual(candidates[1].preamble[0], "static int kleva_stub_Handler_called;")
        self.assertTrue(candidates[1].preamble[1].startswith("static void kleva_stub_Handler"))
        self.assertEqual(candidates[1].witness_setup, ["int out_handler_called = kleva_stub_Handler_called;"])
        self.assertEqual(candidates[1].extra_outputs, ["out_handler_called"])
        self.assertEqual(candidates[1].branch_facts, [
            BranchFact("handler", "!=", "NULL"),
        ])


if __name__ == "__main__":
    unittest.main()
