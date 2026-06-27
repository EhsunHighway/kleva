from __future__ import annotations

import unittest

from kleva.ir.model import AddressOf, AssignmentStmt, BinaryOp, CallExpr, DeclarationStmt, Dereference, FieldAccess, FunctionIR, IfStmt, IntLiteral, LoopStmt, ReturnStmt, SourceLocation, SwitchCase, SwitchStmt, UnaryOp, VarRef
from kleva.shaping.candidates import CallOutcomeFact, PostStateFact
from kleva.shaping.ir_callees import callee_candidates_from_ir, callee_guards_from_ir


class IrCalleeShapingTests(unittest.TestCase):
    def test_detects_nonzero_return_guard(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                    SourceLocation("sample.c", 9, 5),
                )
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "prepare")
        self.assertEqual(guards[0].args, ["ctx"])
        self.assertEqual(guards[0].failure_when, "nonzero")

    def test_detects_unary_not_guard(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    UnaryOp("!", CallExpr("allocate", [])),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "allocate")
        self.assertEqual(guards[0].args, [])
        self.assertEqual(guards[0].failure_when, "zero")

    def test_detects_error_code_return_guard(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("prepare", []), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "prepare")
        self.assertEqual(guards[0].args, [])
        self.assertEqual(guards[0].failure_when, "equals_-1")

    def test_detects_nested_return_guard(self):
        func = FunctionIR(
            "run",
            [
                LoopStmt(
                    "for",
                    VarRef("keep_going"),
                    [
                        IfStmt(
                            BinaryOp("<", CallExpr("step", [FieldAccess(VarRef("ctx"), "item")]), IntLiteral(0)),
                            [ReturnStmt(IntLiteral(-1))],
                        )
                    ],
                )
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "step")
        self.assertEqual(guards[0].args, ["ctx->item"])
        self.assertEqual(guards[0].failure_when, "negative")

    def test_detects_return_value_guard_from_local_declaration(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt("item", init=CallExpr("lookup", [VarRef("table")])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(-1))],
                    SourceLocation("sample.c", 22, 9),
                ),
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "lookup")
        self.assertEqual(guards[0].args, ["table"])
        self.assertEqual(guards[0].failure_when, "zero")
        self.assertEqual(guards[0].loc, SourceLocation("sample.c", 22, 9))

    def test_detects_return_value_guard_from_local_assignment(self):
        func = FunctionIR(
            "run",
            [
                AssignmentStmt(VarRef("item"), CallExpr("lookup", [VarRef("table")])),
                IfStmt(
                    BinaryOp("==", VarRef("item"), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "lookup")
        self.assertEqual(guards[0].args, ["table"])
        self.assertEqual(guards[0].failure_when, "equals_0")

    def test_generates_success_and_failure_candidates(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                    SourceLocation("sample.c", 9, 5),
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            lambda callee, args: ([f"{args[0]}->ready = 1;"], [f"/* {callee} preamble */"]) if args else ([], []),
        )

        self.assertEqual(
            [candidate.name for candidate in candidates],
            ["ir_callee_prepare_nonzero_failure", "ir_callee_prepare_nonzero_success"],
        )
        self.assertEqual(candidates[1].setup, ["ctx->ready = 1;"])
        self.assertEqual(candidates[1].preamble, ["/* prepare preamble */"])
        self.assertFalse(candidates[0].witness_outputs)
        self.assertTrue(candidates[1].witness_outputs)
        self.assertEqual(
            candidates[1].witness_setup,
            ["int out_ir_callee_prepare_nonzero_success_ctx_ready_nonzero = (ctx->ready != 0);"],
        )
        self.assertEqual(
            candidates[1].extra_outputs,
            ["out_ir_callee_prepare_nonzero_success_ctx_ready_nonzero"],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:9:5")
        self.assertEqual(candidates[0].target_branch, "callee prepare failure nonzero")
        self.assertEqual(candidates[0].call_facts, [
            CallOutcomeFact("prepare", "nonzero", "failure"),
        ])
        self.assertEqual(candidates[1].call_facts, [
            CallOutcomeFact("prepare", "nonzero", "success"),
        ])
        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_inverts_helper_ir_failure_preconditions(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("prepare", [VarRef("ctx")]), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    UnaryOp("!", FieldAccess(VarRef("item"), "ready")),
                    [ReturnStmt(IntLiteral(-1))],
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].setup, ["ctx->ready = 1;"])
        self.assertIn(
            PostStateFact("ctx->ready", "!=", "0"),
            candidates[1].post_state_facts,
        )

    def test_success_candidate_records_post_state_facts_without_domain_names(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("make_ready", [VarRef("thing")]), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            lambda _callee, args: ([f"{args[0]}->available = 1;"], []),
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("thing->available", "!=", "0"),
        ])
        self.assertEqual(candidates[1].semantic_fact_dicts()[-1], {
            "kind": "post_state",
            "target": "thing->available",
            "relation": "!=",
            "value": "0",
        })

    def test_success_candidate_infers_post_state_from_helper_ir(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("item"), "ready"),
                    IntLiteral(1),
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            lambda _callee, _args: ([], []),
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])
        self.assertEqual(candidates[1].witness_setup, [
            "int out_ir_callee_prepare_nonzero_success_ctx_ready_nonzero = (ctx->ready != 0);",
        ])

    def test_success_candidate_maps_helper_ir_names_generically(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("activate", [VarRef("bag")]), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "activate",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("slot"), "available"),
                    IntLiteral(1),
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"activate": helper},
            helper_params={"activate": ("slot",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("bag->available", "!=", "0"),
        ])

    def test_success_candidate_infers_post_state_through_helper_alias(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                DeclarationStmt("alias", init=VarRef("item")),
                AssignmentStmt(
                    FieldAccess(VarRef("alias"), "ready"),
                    IntLiteral(1),
                ),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_does_not_treat_conditional_assignment_as_guaranteed_post_state(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    VarRef("enabled"),
                    [
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        )
                    ],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [])

    def test_success_candidate_keeps_straight_line_assignment_before_return(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("item"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_intersects_conditional_and_fallthrough_success_facts(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    VarRef("enabled"),
                    [
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        ),
                        ReturnStmt(IntLiteral(0)),
                    ],
                ),
                AssignmentStmt(
                    FieldAccess(VarRef("item"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_drops_conditional_facts_not_shared_by_all_success_paths(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    VarRef("enabled"),
                    [
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        ),
                        ReturnStmt(IntLiteral(0)),
                    ],
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [])

    def test_success_candidate_ignores_explicit_failure_return_paths(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("prepare", [VarRef("ctx")]), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    VarRef("bad"),
                    [
                        ReturnStmt(UnaryOp("-", IntLiteral(1))),
                    ],
                ),
                AssignmentStmt(
                    FieldAccess(VarRef("item"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_filters_nonzero_returns_for_nonzero_failure_mode(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                IfStmt(
                    VarRef("other_error"),
                    [
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        ),
                        ReturnStmt(IntLiteral(2)),
                    ],
                ),
                AssignmentStmt(
                    FieldAccess(VarRef("item"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_uses_modeled_default_switch_body_facts(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                SwitchStmt(
                    VarRef("mode"),
                    cases=[SwitchCase(1)],
                    has_default=True,
                    body=[
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        ),
                        ReturnStmt(IntLiteral(0)),
                    ],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_keeps_switch_without_default_conservative(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                SwitchStmt(
                    VarRef("mode"),
                    cases=[SwitchCase(1)],
                    has_default=False,
                    body=[
                        AssignmentStmt(
                            FieldAccess(VarRef("item"), "ready"),
                            IntLiteral(1),
                        ),
                        ReturnStmt(IntLiteral(0)),
                    ],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [])

    def test_success_candidate_intersects_precise_switch_case_bodies(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                SwitchStmt(
                    VarRef("mode"),
                    cases=[
                        SwitchCase(1, [
                            AssignmentStmt(FieldAccess(VarRef("item"), "ready"), IntLiteral(1)),
                            ReturnStmt(IntLiteral(0)),
                        ]),
                        SwitchCase(2, [
                            AssignmentStmt(FieldAccess(VarRef("item"), "ready"), IntLiteral(1)),
                            ReturnStmt(IntLiteral(0)),
                        ]),
                    ],
                    has_default=True,
                    default_body=[
                        AssignmentStmt(FieldAccess(VarRef("item"), "ready"), IntLiteral(1)),
                        ReturnStmt(IntLiteral(0)),
                    ],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->ready", "!=", "0"),
        ])

    def test_success_candidate_drops_switch_case_fact_not_shared_by_default(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("prepare", [VarRef("ctx")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "prepare",
            [
                SwitchStmt(
                    VarRef("mode"),
                    cases=[
                        SwitchCase(1, [
                            AssignmentStmt(FieldAccess(VarRef("item"), "ready"), IntLiteral(1)),
                            ReturnStmt(IntLiteral(0)),
                        ]),
                    ],
                    has_default=True,
                    default_body=[
                        ReturnStmt(IntLiteral(0)),
                    ],
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"prepare": helper},
            helper_params={"prepare": ("item",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [])

    def test_success_candidate_maps_out_parameter_address_argument(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("fill", [AddressOf(VarRef("slot"))]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "fill",
            [
                AssignmentStmt(
                    Dereference(VarRef("out")),
                    IntLiteral(1),
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"fill": helper},
            helper_params={"fill": ("out",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("slot", "!=", "0"),
        ])

    def test_success_candidate_maps_out_parameter_pointer_argument(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("fill", [VarRef("slot_ptr")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "fill",
            [
                AssignmentStmt(
                    Dereference(VarRef("out")),
                    IntLiteral(1),
                )
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"fill": helper},
            helper_params={"fill": ("out",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("*slot_ptr", "!=", "0"),
        ])

    def test_success_candidate_generates_for_return_value_guard(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt("item", init=CallExpr("lookup", [VarRef("table")])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )

        candidates = callee_candidates_from_ir(func)

        self.assertEqual(
            [candidate.name for candidate in candidates],
            ["ir_callee_lookup_zero_failure", "ir_callee_lookup_zero_success"],
        )
        self.assertEqual(candidates[1].call_facts, [
            CallOutcomeFact("lookup", "zero", "success"),
        ])

    def test_success_candidate_maps_returned_parameter_facts_to_result_alias(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt("item", init=CallExpr("lookup", [VarRef("table")])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )
        helper = FunctionIR(
            "lookup",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("owner"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(VarRef("owner")),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"lookup": helper},
            helper_params={"lookup": ("owner",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("table->ready", "!=", "0"),
            PostStateFact("item->ready", "!=", "0"),
        ])

    def test_success_candidate_maps_returned_parameter_alias_to_result_alias(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt("item", init=CallExpr("lookup", [VarRef("table")])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )
        helper = FunctionIR(
            "lookup",
            [
                DeclarationStmt("alias", init=VarRef("owner")),
                AssignmentStmt(
                    FieldAccess(VarRef("alias"), "ready"),
                    IntLiteral(1),
                ),
                ReturnStmt(VarRef("alias")),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"lookup": helper},
            helper_params={"lookup": ("owner",)},
        )

        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("table->ready", "!=", "0"),
            PostStateFact("item->ready", "!=", "0"),
        ])


if __name__ == "__main__":
    unittest.main()
