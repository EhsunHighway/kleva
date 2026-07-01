from __future__ import annotations

import unittest

from kleva.ir.model import AddressOf, ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, CastExpr, DeclarationStmt, Dereference, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, LoopStmt, ReturnStmt, SourceLocation, SwitchCase, SwitchStmt, UnaryOp, UnknownExpr, VarRef
from kleva.shaping.candidates import CallOutcomeFact, HelperSideEffectFact, ObjectPathFact, OwnershipPathFact, PostStateFact
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

    def test_detects_malloc_guard_even_when_sizeof_argument_is_unrendered(self):
        func = FunctionIR(
            "make",
            [
                DeclarationStmt("item", init=CallExpr("malloc", [UnknownExpr("sizeof")])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(0))],
                    SourceLocation("sample.c", 4, 5),
                ),
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(len(guards), 1)
        self.assertEqual(guards[0].callee, "malloc")
        self.assertEqual(guards[0].args, [])
        self.assertEqual(guards[0].failure_when, "zero")
        self.assertEqual(guards[0].result, "item")
        self.assertEqual(guards[0].allocation_index, 0)

    def test_detects_malloc_guard_for_assigned_field(self):
        func = FunctionIR(
            "make",
            [
                DeclarationStmt("item", init=CallExpr("malloc", [])),
                IfStmt(UnaryOp("!", VarRef("item")), [ReturnStmt(IntLiteral(0))]),
                AssignmentStmt(FieldAccess(VarRef("item"), "buf"), CallExpr("malloc", [VarRef("size")])),
                IfStmt(
                    UnaryOp("!", FieldAccess(VarRef("item"), "buf")),
                    [ReturnStmt(IntLiteral(0))],
                ),
            ],
        )

        guards = callee_guards_from_ir(func)

        self.assertEqual(
            [(guard.callee, guard.result, guard.allocation_index) for guard in guards],
            [("malloc", "item", 0), ("malloc", "item->buf", 1)],
        )

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
        self.assertEqual(candidates[1].witness_setup, [])
        self.assertEqual(candidates[1].extra_outputs, [])
        self.assertEqual(candidates[0].source_location, "sample.c:9:5")
        self.assertEqual(candidates[0].target_branch, "callee prepare failure nonzero")
        self.assertEqual(candidates[0].call_facts, [
            CallOutcomeFact("prepare", "nonzero", "failure"),
        ])
        self.assertEqual(candidates[1].call_facts, [
            CallOutcomeFact("prepare", "nonzero", "success"),
        ])
        self.assertEqual(candidates[1].post_state_facts, [])

    def test_callee_guard_resolves_local_alias_arguments(self):
        func = FunctionIR(
            "receive",
            [
                DeclarationStmt(
                    "hdr",
                    init=CastExpr("Header *", FieldAccess(VarRef("pkt", "Packet *"), "data", "uint8_t *")),
                ),
                IfStmt(
                    BinaryOp("!=", CallExpr("checksum", [VarRef("hdr", "Header *")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )
        helper = FunctionIR(
            "checksum",
            [
                IfStmt(
                    UnaryOp("!", VarRef("hdr", "Header *")),
                    [ReturnStmt(IntLiteral(1))],
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"checksum": helper},
            helper_params={"checksum": ("hdr",)},
        )

        self.assertEqual(candidates[0].name, "ir_callee_checksum_nonzero_failure")
        self.assertEqual(candidates[0].setup, ["pkt->data = 0;"])
        self.assertNotIn("hdr = 0;", candidates[0].setup)

    def test_helper_success_non_null_pointer_field_uses_object_path_fact(self):
        func = FunctionIR(
            "receive",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("checksum", [VarRef("frame")]), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "checksum",
            [
                IfStmt(
                    UnaryOp("!", FieldAccess(VarRef("pkt", "Packet *"), "data", "uint8_t *")),
                    [ReturnStmt(IntLiteral(-1))],
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"checksum": helper},
            helper_params={"checksum": ("pkt",)},
        )

        self.assertEqual(candidates[1].setup, [
            "/* kleva: non-null pointer path frame->data backed by fixture */",
        ])
        self.assertIn(ObjectPathFact("frame", ("data",)), candidates[1].object_paths)

    def test_malloc_failure_candidate_uses_allocator_control(self):
        func = FunctionIR(
            "make",
            [
                DeclarationStmt("item", init=CallExpr("malloc", [])),
                IfStmt(
                    UnaryOp("!", VarRef("item")),
                    [ReturnStmt(IntLiteral(0))],
                ),
            ],
        )

        candidates = callee_candidates_from_ir(func)

        self.assertEqual(candidates[0].name, "ir_alloc_malloc_0_zero_failure")
        self.assertIn("__kleva_alloc_fail_on(0);", candidates[0].setup)
        self.assertTrue(any("void *__kleva_malloc(size_t size)" in line for line in candidates[0].preamble))
        self.assertFalse(any("void *malloc(size_t size)" in line for line in candidates[0].preamble))

    def test_allocating_helper_failure_accounts_for_prior_allocations(self):
        func = FunctionIR(
            "make",
            [
                DeclarationStmt("owner", init=CallExpr("malloc", [])),
                DeclarationStmt("queue", init=CallExpr("make_queue", [IntLiteral(1)])),
                IfStmt(
                    UnaryOp("!", VarRef("queue")),
                    [ReturnStmt(IntLiteral(0))],
                ),
            ],
        )
        helper = FunctionIR(
            "make_queue",
            [
                DeclarationStmt("queue", init=CallExpr("malloc", [])),
                IfStmt(UnaryOp("!", VarRef("queue")), [ReturnStmt(IntLiteral(0))]),
                ReturnStmt(VarRef("queue")),
            ],
        )

        candidates = callee_candidates_from_ir(func, helper_irs={"make_queue": helper})

        self.assertEqual(candidates[0].name, "ir_callee_make_queue_zero_failure")
        self.assertIn("__kleva_alloc_fail_on(1);", candidates[0].setup)
        self.assertTrue(any("void *__kleva_malloc(size_t size)" in line for line in candidates[0].preamble))

    def test_helper_success_setup_treats_casted_null_return_as_zero_failure(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt("item", init=CallExpr("pop", [VarRef("queue")])),
                IfStmt(UnaryOp("!", VarRef("item")), [ReturnStmt(IntLiteral(0))]),
            ],
        )
        helper = FunctionIR(
            "pop",
            [
                IfStmt(
                    BinaryOp("==", FieldAccess(VarRef("q"), "count"), IntLiteral(0)),
                    [ReturnStmt(CastExpr("void *", IntLiteral(0), kind="NullToPointer"))],
                ),
                DeclarationStmt(
                    "item",
                    c_type="Item *",
                    init=ArraySubscript(FieldAccess(VarRef("q"), "items", c_type="Item **"), IntLiteral(0), c_type="Item *"),
                ),
                ReturnStmt(VarRef("item")),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"pop": helper},
            helper_params={"pop": ("q",)},
        )

        self.assertIn("#include <stdlib.h>", candidates[1].preamble)
        self.assertIn("queue->count = 1;", candidates[1].setup)
        self.assertIn("Item * kleva_pop_item_0_array[1];", candidates[1].setup)
        self.assertIn("memset(kleva_pop_item_0_array, 0, sizeof(kleva_pop_item_0_array));", candidates[1].setup)
        self.assertIn("queue->items = kleva_pop_item_0_array;", candidates[1].setup)
        self.assertIn("Item *kleva_pop_item_0 = malloc(sizeof(*kleva_pop_item_0));", candidates[1].setup)
        self.assertIn("assert(kleva_pop_item_0 != NULL);", candidates[1].setup)
        self.assertIn("memset(kleva_pop_item_0, 0, sizeof(*kleva_pop_item_0));", candidates[1].setup)
        self.assertIn("queue->items[0] = kleva_pop_item_0;", candidates[1].setup)

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

        self.assertEqual(candidates[0].setup, ["ctx->ready = 0;"])
        self.assertEqual(candidates[1].setup, ["ctx->ready = 1;"])
        self.assertNotIn(
            PostStateFact("ctx->ready", "!=", "0"),
            candidates[1].post_state_facts,
        )
        self.assertNotIn(
            ObjectPathFact("ctx", ("ready",)),
            candidates[1].object_paths,
        )

    def test_success_candidate_propagates_helper_ownership_facts(self):
        func = FunctionIR(
            "run",
            [
                IfStmt(
                    BinaryOp("==", CallExpr("attach", [VarRef("owner"), VarRef("item"), VarRef("tmp")]), UnaryOp("-", IntLiteral(1))),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )
        helper = FunctionIR(
            "attach",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("box"), "slot"),
                    VarRef("value"),
                ),
                ExprStmt(CallExpr("free", [VarRef("scratch")])),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = callee_candidates_from_ir(
            func,
            helper_irs={"attach": helper},
            helper_params={"attach": ("box", "value", "scratch")},
        )

        self.assertIn(
            OwnershipPathFact("item", "transferred", "attach:box->slot"),
            candidates[1].ownership_facts,
        )
        self.assertIn(
            OwnershipPathFact("tmp", "consumed", "attach:free"),
            candidates[1].ownership_facts,
        )
        self.assertIn(
            HelperSideEffectFact("field-changed", "owner->slot", "item", "assignment"),
            candidates[1].side_effect_facts,
        )
        self.assertIn({
            "kind": "ownership",
            "target": "item",
            "action": "transferred",
            "via": "attach:box->slot",
        }, candidates[1].semantic_fact_dicts())

    def test_success_candidate_does_not_treat_fixture_setup_as_post_state(self):
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

        self.assertEqual(candidates[1].setup, ["thing->available = 1;"])
        self.assertEqual(candidates[1].post_state_facts, [])
        self.assertFalse(any(
            fact.get("kind") == "post_state" and fact.get("target") == "thing->available"
            for fact in candidates[1].semantic_fact_dicts()
        ))

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
