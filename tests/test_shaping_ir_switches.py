import unittest

from kleva.fixtures.construction import safe_c_name
from kleva.ir.model import AddressOf, ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, CastExpr, DeclarationStmt, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, ReturnStmt, SourceLocation, SwitchCase, SwitchStmt, VarRef
from kleva.shaping.candidates import BranchFact, ObjectPathFact, PostStateFact, StateTransitionFact
from kleva.shaping.ir_conditions import IrConditionOps
from kleva.shaping.ir_switches import state_switch_candidates_from_ir


def _ops():
    return IrConditionOps(
        safe_c_name,
        lambda value: "1" if value == "0" else "0",
    )


class IrSwitchShapingTests(unittest.TestCase):
    def test_generates_state_switch_candidates_from_typed_ir(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx"), "state"),
                    [SwitchCase(1), SwitchCase(2)],
                    loc=SourceLocation("sample.c", 20, 9),
                )
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [
                ("ir_case_state_1", ["ctx->state = 1;"]),
                ("ir_case_state_2", ["ctx->state = 2;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:20:9")
        self.assertEqual(candidates[0].target_branch, "switch ctx->state case 1")

    def test_switch_case_candidate_records_direct_body_assignment_post_state(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx"), "state"),
                    [
                        SwitchCase(
                            1,
                            [
                                AssignmentStmt(
                                    FieldAccess(VarRef("ctx"), "ready"),
                                    IntLiteral(1),
                                )
                            ],
                        )
                    ],
                )
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(candidates[0].post_state_facts, [
            PostStateFact("ctx->ready", "==", "1"),
        ])

    def test_generates_default_candidate_when_switch_has_default(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx"), "kind"),
                    [SwitchCase(0), SwitchCase(2)],
                    has_default=True,
                )
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [
                ("ir_case_kind_0", ["ctx->kind = 0;"]),
                ("ir_case_kind_2", ["ctx->kind = 2;"]),
                ("ir_default_kind", ["ctx->kind = 1;"]),
            ],
        )
        self.assertEqual(candidates[2].source_location, "ir:run:switch[0]")
        self.assertEqual(candidates[2].target_branch, "switch ctx->kind default")

    def test_generates_candidates_for_nested_state_selector(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(
                        FieldAccess(VarRef("ctx", "Context *"), "conn", "Connection *"),
                        "state",
                        "int",
                    ),
                    [SwitchCase(3)],
                    loc=SourceLocation("sample.c", 31, 13),
                )
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [("ir_case_conn_state_3", ["ctx->conn->state = 3;"])],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:31:13")
        self.assertEqual(candidates[0].target_branch, "switch ctx->conn->state case 3")
        self.assertEqual(candidates[0].object_paths, [
            ObjectPathFact("ctx", ("conn", "state"), "Context *", "int"),
        ])

    def test_generates_candidates_for_selector_reached_through_local_alias(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "conn",
                    "Connection *",
                    FieldAccess(VarRef("ctx", "Context *"), "active", "Connection *"),
                ),
                SwitchStmt(
                    FieldAccess(VarRef("conn", "Connection *"), "state", "int"),
                    [SwitchCase(4)],
                    loc=SourceLocation("sample.c", 44, 9),
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [("ir_case_active_state_4", ["ctx->active->state = 4;"])],
        )
        self.assertEqual(candidates[0].target_branch, "switch ctx->active->state case 4")
        self.assertEqual(candidates[0].object_paths, [
            ObjectPathFact("ctx", ("active", "state"), "Context *", "int"),
        ])

    def test_generates_candidates_for_selector_reached_through_cast_alias(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "ctx",
                    "Context *",
                    CastExpr("Context *", VarRef("raw", "void *"), "BitCast", "Context *"),
                ),
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
                    [SwitchCase(5)],
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [("ir_case_state_5", ["((Context *)raw)->state = 5;"])],
        )

    def test_generates_transition_candidate_from_assignment_to_selector(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
                    [SwitchCase(1)],
                    body=[
                        AssignmentStmt(
                            FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
                            IntLiteral(2, "int"),
                        ),
                    ],
                    loc=SourceLocation("sample.c", 51, 5),
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup, c.target_branch, c.witness_outputs) for c in candidates],
            [
                ("ir_case_state_1", ["ctx->state = 1;"], "switch ctx->state case 1", False),
                ("ir_transition_state_1_to_2", ["ctx->state = 1;"], "transition ctx->state 1 -> 2", True),
            ],
        )
        self.assertEqual(candidates[1].transition_facts, [
            StateTransitionFact("ctx->state", "1", "2"),
        ])
        self.assertIn({
            "kind": "transition",
            "selector": "ctx->state",
            "from": "1",
            "to": "2",
        }, candidates[1].semantic_fact_dicts())

    def test_generates_candidates_for_symbolic_case_names(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                    [SwitchCase("STATE_INIT"), SwitchCase("STATE_DONE")],
                    body=[
                        AssignmentStmt(
                            FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                            VarRef("STATE_DONE", "State"),
                        ),
                    ],
                    loc=SourceLocation("sample.c", 61, 5),
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func)

        self.assertEqual(
            [(c.name, c.setup, c.target_branch) for c in candidates],
            [
                ("ir_case_state_STATE_INIT", ["ctx->state = STATE_INIT;"], "switch ctx->state case STATE_INIT"),
                ("ir_transition_state_STATE_INIT_to_STATE_DONE", ["ctx->state = STATE_INIT;"], "transition ctx->state STATE_INIT -> STATE_DONE"),
                ("ir_case_state_STATE_DONE", ["ctx->state = STATE_DONE;"], "switch ctx->state case STATE_DONE"),
            ],
        )

    def test_generates_guard_candidates_inside_switch_case(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                    [
                        SwitchCase(
                            "STATE_OPEN",
                            body=[
                                IfStmt(
                                    BinaryOp("==", FieldAccess(VarRef("ctx", "Context *"), "ready", "int"), IntLiteral(0)),
                                    [
                                        AssignmentStmt(
                                            FieldAccess(VarRef("ctx", "Context *"), "handled", "int"),
                                            IntLiteral(1),
                                        )
                                    ],
                                    loc=SourceLocation("sample.c", 71, 17),
                                )
                            ],
                        )
                    ],
                    loc=SourceLocation("sample.c", 69, 9),
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(c.name, c.setup, c.target_branch) for c in candidates],
            [
                ("ir_case_state_STATE_OPEN", ["ctx->state = STATE_OPEN;"], "switch ctx->state case STATE_OPEN"),
                (
                    "ir_case_guard_state_STATE_OPEN_1_ctx_ready_eq_0",
                    ["ctx->state = STATE_OPEN;", "ctx->ready = 0;"],
                    "switch ctx->state case STATE_OPEN; if ctx->ready == 0",
                ),
                (
                    "ir_case_guard_state_STATE_OPEN_1_false_ctx_ready_ne_0",
                    ["ctx->state = STATE_OPEN;", "ctx->ready = 1;"],
                    "switch ctx->state case STATE_OPEN; if ctx->ready != 0",
                ),
            ],
        )
        self.assertEqual(candidates[1].source_location, "sample.c:71:17")
        self.assertEqual(
            candidates[1].branch_facts,
            [
                BranchFact("ctx->state", "case", "STATE_OPEN"),
                BranchFact("ctx->ready", "==", "0"),
            ],
        )
        self.assertEqual(candidates[1].post_state_facts, [
            PostStateFact("ctx->handled", "==", "1"),
        ])
        self.assertEqual(candidates[2].post_state_facts, [])

    def test_later_switch_case_guard_inherits_negated_terminating_guard(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                    [
                        SwitchCase(
                            "STATE_OPEN",
                            body=[
                                IfStmt(
                                    BinaryOp("&", VarRef("flags", "unsigned"), VarRef("FLAG_FIN", "unsigned")),
                                    [ReturnStmt(IntLiteral(0))],
                                ),
                                IfStmt(
                                    BinaryOp(">", VarRef("payload_len", "size_t"), IntLiteral(0)),
                                    [
                                        AssignmentStmt(
                                            FieldAccess(VarRef("ctx", "Context *"), "handled", "int"),
                                            IntLiteral(1),
                                        )
                                    ],
                                ),
                            ],
                        )
                    ],
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func, _ops())
        payload_candidate = next(
            c for c in candidates
            if c.target_branch == (
                "switch ctx->state case STATE_OPEN; "
                "if !(flags & FLAG_FIN); payload_len > 0"
            )
        )

        self.assertEqual(payload_candidate.setup, [
            "ctx->state = STATE_OPEN;",
            "flags = 0;",
            "payload_len = ((0) + 1);",
        ])
        self.assertEqual(payload_candidate.branch_facts, [
            BranchFact("ctx->state", "case", "STATE_OPEN"),
            BranchFact("flags", "!&", "FLAG_FIN"),
            BranchFact("payload_len", ">", "0"),
        ])
        self.assertEqual(payload_candidate.post_state_facts, [
            PostStateFact("ctx->handled", "==", "1"),
        ])

    def test_generates_guarded_transition_candidate_inside_switch_case(self):
        func = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                    [
                        SwitchCase(
                            "OPEN",
                            body=[
                                IfStmt(
                                    BinaryOp("==", FieldAccess(VarRef("ctx", "Context *"), "ready", "int"), IntLiteral(1)),
                                    [
                                        AssignmentStmt(
                                            FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                                            VarRef("DONE", "State"),
                                        )
                                    ],
                                )
                            ],
                        )
                    ],
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func, _ops())
        transition = next(c for c in candidates if c.name.startswith("ir_transition_state_OPEN_to_DONE"))

        self.assertEqual(transition.setup, ["ctx->state = OPEN;", "ctx->ready = 1;"])
        self.assertEqual(transition.target_branch, "transition ctx->state OPEN -> DONE when ctx->ready == 1")
        self.assertEqual(transition.transition_facts, [
            StateTransitionFact("ctx->state", "OPEN", "DONE", "ctx->ready == 1"),
        ])

    def test_generates_transition_candidate_across_helper_function(self):
        caller = FunctionIR(
            "run",
            [
                SwitchStmt(
                    FieldAccess(VarRef("ctx", "Context *"), "state", "State"),
                    [
                        SwitchCase(
                            "OPEN",
                            body=[
                                ExprStmt(CallExpr("finish", [VarRef("ctx", "Context *")]))
                            ],
                        )
                    ],
                ),
            ],
        )
        helper = FunctionIR(
            "finish",
            [
                AssignmentStmt(
                    FieldAccess(VarRef("item", "Context *"), "state", "State"),
                    VarRef("DONE", "State"),
                ),
                ReturnStmt(IntLiteral(0)),
            ],
        )

        candidates = state_switch_candidates_from_ir(
            caller,
            _ops(),
            {"finish": helper},
            {"finish": ("item",)},
        )
        transition = next(c for c in candidates if c.name.startswith("ir_transition_state_OPEN_to_DONE"))

        self.assertEqual(transition.setup, ["ctx->state = OPEN;"])
        self.assertEqual(transition.target_branch, "transition ctx->state OPEN -> DONE via helper:finish")
        self.assertEqual(transition.transition_facts, [
            StateTransitionFact("ctx->state", "OPEN", "DONE", None, "helper:finish"),
        ])

    def test_resolves_case_guard_aliases(self):
        func = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "conn",
                    "Connection *",
                    FieldAccess(VarRef("ctx", "Context *"), "active", "Connection *"),
                ),
                SwitchStmt(
                    FieldAccess(VarRef("conn", "Connection *"), "state", "State"),
                    [
                        SwitchCase(
                            "OPEN",
                            body=[
                                IfStmt(
                                    BinaryOp("!=", FieldAccess(VarRef("conn", "Connection *"), "ready", "int"), IntLiteral(1)),
                                )
                            ],
                        )
                    ],
                ),
            ],
        )

        candidates = state_switch_candidates_from_ir(func, _ops())

        self.assertIn(
            (
                "ir_case_guard_active_state_OPEN_1_ctx_active_ready_ne_1",
                ["ctx->active->state = OPEN;", "ctx->active->ready = 0;"],
            ),
            [(c.name, c.setup) for c in candidates],
        )

    def test_rewrites_helper_returned_array_slot_alias(self):
        caller = FunctionIR(
            "run",
            [
                DeclarationStmt(
                    "entry",
                    "Entry *",
                    CallExpr(
                        "find_entry",
                        [FieldAccess(VarRef("ctx", "Context *"), "table", "Table *")],
                        "Entry *",
                    ),
                ),
                SwitchStmt(
                    FieldAccess(VarRef("entry", "Entry *"), "state", "State"),
                    [
                        SwitchCase(
                            "OPEN",
                            body=[
                                IfStmt(
                                    BinaryOp("==", FieldAccess(VarRef("entry", "Entry *"), "ready", "int"), IntLiteral(1)),
                                )
                            ],
                        )
                    ],
                ),
            ],
        )
        helper = FunctionIR(
            "find_entry",
            [
                DeclarationStmt(
                    "entry",
                    "Entry *",
                    AddressOf(
                        ArraySubscript(
                            FieldAccess(VarRef("table", "Table *"), "items", "Entry[]"),
                            VarRef("i", "int"),
                            "Entry",
                        ),
                        "Entry *",
                    ),
                ),
                ReturnStmt(VarRef("entry", "Entry *")),
            ],
        )

        candidates = state_switch_candidates_from_ir(
            caller,
            _ops(),
            {"find_entry": helper},
            {"find_entry": ("table",)},
        )

        self.assertEqual(
            [(c.name, c.setup) for c in candidates],
            [
                ("ir_case_state_OPEN", ["ctx->table->items[0].state = OPEN;"]),
                (
                    "ir_case_guard_state_OPEN_1_entry_ready_eq_1",
                    ["ctx->table->items[0].state = OPEN;", "ctx->table->items[0].ready = 1;"],
                ),
                (
                    "ir_case_guard_state_OPEN_1_false_entry_ready_ne_1",
                    ["ctx->table->items[0].state = OPEN;", "ctx->table->items[0].ready = 0;"],
                ),
            ],
        )
        self.assertEqual(
            candidates[1].branch_facts,
            [
                BranchFact("ctx->table->items[0].state", "case", "OPEN"),
                BranchFact("ctx->table->items[0].ready", "==", "1"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
