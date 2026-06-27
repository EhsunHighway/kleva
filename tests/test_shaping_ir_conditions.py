from __future__ import annotations

import unittest

from kleva.fixtures.construction import safe_c_name
from kleva.ir.model import ArraySubscript, AssignmentStmt, BinaryOp, CallExpr, CastExpr, DeclarationStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, LoopStmt, SourceLocation, SwitchStmt, UnaryOp, VarRef
from kleva.shaping.candidates import ObjectPathFact
from kleva.shaping.ir_byte_order import decoded_field_aliases_from_ir
from kleva.shaping.ir_conditions import IrConditionOps, condition_candidates_from_ir


def _ops():
    return IrConditionOps(
        safe_c_name,
        lambda value: "1" if value == "0" else "0",
    )


def _ops_with_byte_order(func):
    return IrConditionOps(
        safe_c_name,
        lambda value: "1" if value == "0" else "0",
        decoded_field_aliases_from_ir(func),
        lambda fn: fn.replace("ntoh", "hton", 1) if "ntoh" in fn else "",
    )


class IrConditionShapingTests(unittest.TestCase):
    def test_generates_comparison_condition_candidate(self):
        func = FunctionIR(
            "step",
            [IfStmt(
                BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(3)),
                loc=SourceLocation("sample.c", 12, 5),
            )],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_state_eq_3", ["ctx->state = 3;"]),
                ("ir_if_0_false_ctx_state_ne_3", ["ctx->state = 0;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:12:5")
        self.assertEqual(candidates[0].target_branch, "if ctx->state == 3")

    def test_generates_condition_candidate_inside_nested_body(self):
        func = FunctionIR(
            "step",
            [
                SwitchStmt(
                    VarRef("state", "int"),
                    body=[
                        IfStmt(
                            BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(3)),
                            loc=SourceLocation("sample.c", 18, 9),
                        )
                    ],
                )
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_state_eq_3", ["ctx->state = 3;"]),
                ("ir_if_0_false_ctx_state_ne_3", ["ctx->state = 0;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:18:9")

    def test_resolves_declaration_alias_before_condition(self):
        func = FunctionIR(
            "step",
            [
                DeclarationStmt(
                    "state",
                    "int",
                    FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
                ),
                IfStmt(BinaryOp("==", VarRef("state", "int"), IntLiteral(3, "int"))),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_ctx_state_eq_3",
                ["ctx->state = 3;"],
                "if ctx->state == 3",
            ), (
                "ir_if_0_false_ctx_state_ne_3",
                ["ctx->state = 0;"],
                "if ctx->state != 3",
            )],
        )
        self.assertEqual(candidates[0].object_paths, [
            ObjectPathFact("ctx", ("state",), "Context *", "int"),
        ])

    def test_skips_unreachable_local_variable_condition(self):
        func = FunctionIR(
            "make",
            [
                DeclarationStmt(
                    "item",
                    "Item *",
                    CallExpr("malloc", []),
                ),
                IfStmt(UnaryOp("!", VarRef("item", "Item *"))),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(candidates, [])

    def test_skips_condition_setup_that_mentions_local_root_in_index(self):
        func = FunctionIR(
            "step",
            [
                DeclarationStmt("e", "Event *", CallExpr("event_queue_pop", [])),
                IfStmt(FieldAccess(
                    ArraySubscript(
                        FieldAccess(VarRef("s", "Scheduler *"), "handlers", "Handler **"),
                        FieldAccess(VarRef("e", "Event *"), "type", "int"),
                        "Handler *",
                    ),
                    "fn",
                    "HandlerFn",
                )),
            ],
        )

        self.assertEqual(condition_candidates_from_ir(func, _ops()), [])

    def test_keeps_reachable_alias_from_local_variable(self):
        func = FunctionIR(
            "step",
            [
                DeclarationStmt(
                    "state",
                    "int",
                    FieldAccess(VarRef("ctx", "Context *"), "state", "int"),
                ),
                IfStmt(BinaryOp("==", VarRef("state", "int"), IntLiteral(3, "int"))),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_state_eq_3", ["ctx->state = 3;"]),
                ("ir_if_0_false_ctx_state_ne_3", ["ctx->state = 0;"]),
            ],
        )

    def test_resolves_assignment_alias_inside_nested_body(self):
        func = FunctionIR(
            "step",
            [
                LoopStmt(
                    "while",
                    VarRef("running", "int"),
                    body=[
                        AssignmentStmt(
                            VarRef("kind", "int"),
                            FieldAccess(VarRef("item", "Item *"), "kind", "int"),
                        ),
                        IfStmt(BinaryOp("!=", VarRef("kind", "int"), IntLiteral(0, "int"))),
                    ],
                )
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_item_kind_ne_0",
                ["item->kind = 1;"],
                "if item->kind != 0",
            ), (
                "ir_if_0_false_item_kind_eq_0",
                ["item->kind = 0;"],
                "if item->kind == 0",
            )],
        )

    def test_splits_boolean_or_into_separate_candidates(self):
        func = FunctionIR(
            "step",
            [
                IfStmt(BinaryOp(
                    "||",
                    BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(1)),
                    BinaryOp("==", FieldAccess(VarRef("ctx"), "mode"), IntLiteral(2)),
                )),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_left_ctx_state_eq_1", ["ctx->state = 1;"]),
                ("ir_if_0_right_ctx_mode_eq_2", ["ctx->mode = 2;"]),
                (
                    "ir_if_0_false_false_ctx_state_ne_1_and_false_ctx_mode_ne_2",
                    ["ctx->state = 0;", "ctx->mode = 0;"],
                ),
            ],
        )

    def test_combines_boolean_and_requirements(self):
        func = FunctionIR(
            "step",
            [
                IfStmt(BinaryOp(
                    "&&",
                    BinaryOp(">", FieldAccess(VarRef("ctx"), "len"), IntLiteral(4)),
                    BinaryOp("!=", FieldAccess(VarRef("ctx"), "closed"), IntLiteral(0)),
                )),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                (
                    "ir_if_0_ctx_len_gt_4_and_ctx_closed_ne_0",
                    ["ctx->len = ((4) + 1);", "ctx->closed = 1;"],
                ),
                ("ir_if_0_false_left_false_ctx_len_le_4", ["ctx->len = 4;"]),
                ("ir_if_0_false_right_false_ctx_closed_eq_0", ["ctx->closed = 0;"]),
            ],
        )

    def test_shapes_field_to_field_comparison_with_small_correlated_values(self):
        func = FunctionIR(
            "push",
            [
                IfStmt(BinaryOp(
                    ">=",
                    FieldAccess(VarRef("queue", "Queue *"), "count", "size_t"),
                    FieldAccess(VarRef("queue", "Queue *"), "capacity", "size_t"),
                )),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                (
                    "ir_if_0_queue_count_ge_queue_capacity",
                    ["queue->capacity = 1;", "queue->count = queue->capacity;"],
                ),
                (
                    "ir_if_0_false_queue_count_lt_queue_capacity",
                    ["queue->capacity = 2;", "queue->count = ((queue->capacity) > 0 ? (queue->capacity) - 1 : 0);"],
                ),
            ],
        )

    def test_adds_continuation_object_paths_after_guard(self):
        func = FunctionIR(
            "pop",
            [
                IfStmt(BinaryOp(
                    "==",
                    FieldAccess(VarRef("queue", "Queue *"), "count", "size_t"),
                    IntLiteral(0, "int"),
                )),
                DeclarationStmt(
                    "item",
                    "Item *",
                    ArraySubscript(
                        FieldAccess(VarRef("queue", "Queue *"), "items", "Item **"),
                        IntLiteral(0, "int"),
                        "Item *",
                    ),
                ),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertIn(
            ObjectPathFact("queue", ("items",), "Queue *", "Item **"),
            candidates[0].object_paths,
        )

    def test_shapes_unary_not_condition(self):
        func = FunctionIR(
            "step",
            [IfStmt(UnaryOp("!", FieldAccess(VarRef("ctx"), "handler")))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_not_ctx_handler", ["ctx->handler = 0;"]),
                ("ir_if_0_false_not_ctx_handler", ["ctx->handler = 1;"]),
            ],
        )

    def test_shapes_unary_not_pointer_condition_with_typed_non_null_value(self):
        func = FunctionIR(
            "step",
            [IfStmt(UnaryOp("!", VarRef("scheduler", "Scheduler *")))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_not_scheduler", ["scheduler = 0;"]),
                ("ir_if_0_false_not_scheduler", ["if (!scheduler) return 0;"]),
            ],
        )

    def test_shapes_truthy_pointer_condition_with_typed_non_null_value(self):
        func = FunctionIR(
            "step",
            [IfStmt(VarRef("event", "Event *"))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_truthy_event", ["if (!event) return 0;"]),
                ("ir_if_0_false_truthy_event", ["event = 0;"]),
            ],
        )

    def test_shapes_bitwise_flag_condition(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp("&", VarRef("flags", "unsigned"), VarRef("READY", "unsigned")))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_flags_has_READY",
                ["flags |= READY;"],
                "if flags & READY",
            ), (
                "ir_if_0_false_flags_has_READY",
                ["flags = 0;"],
                "if !(flags & READY)",
            )],
        )

    def test_shapes_negated_bitwise_flag_condition(self):
        func = FunctionIR(
            "step",
            [IfStmt(UnaryOp(
                "!",
                BinaryOp("&", FieldAccess(VarRef("ctx", "Context *"), "flags", "unsigned"), IntLiteral(4, "int")),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_not_ctx_flags_has_4",
                ["ctx->flags = 0;"],
                "if !(ctx->flags & 4)",
            ), (
                "ir_if_0_false_not_ctx_flags_has_4",
                ["ctx->flags |= 4;"],
                "if ctx->flags & 4",
            )],
        )
        self.assertEqual(candidates[0].object_paths, [
            ObjectPathFact("ctx", ("flags",), "Context *", "unsigned"),
        ])

    def test_preserves_typed_object_path_facts_for_nested_field_condition(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "==",
                FieldAccess(
                    FieldAccess(VarRef("ctx", "Context *"), "conn", "Connection *"),
                    "state",
                    "int",
                ),
                IntLiteral(1, "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(candidates[0].object_paths, [
            ObjectPathFact("ctx", ("conn", "state"), "Context *", "int"),
        ])

    def test_pointer_equality_uses_null_assignment_instead_of_scalar_zero(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "==",
                FieldAccess(VarRef("ctx", "Context *"), "next", "Node *"),
                IntLiteral(0, "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_next_eq_0", ["ctx->next = NULL;"]),
                ("ir_if_0_false_ctx_next_ne_0", ["ctx->next = ((Node *)1);"]),
            ],
        )

    def test_pointer_inequality_uses_typed_non_null_assignment(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "!=",
                FieldAccess(VarRef("ctx", "Context *"), "next", "Node *"),
                IntLiteral(0, "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_next_ne_0", ["ctx->next = ((Node *)1);"]),
                ("ir_if_0_false_ctx_next_eq_0", ["ctx->next = NULL;"]),
            ],
        )

    def test_skips_numeric_boundary_shaping_for_pointer_expressions(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                ">",
                FieldAccess(VarRef("ctx", "Context *"), "next", "Node *"),
                IntLiteral(4, "int"),
            ))],
        )

        self.assertEqual(condition_candidates_from_ir(func, _ops()), [])

    def test_shapes_casted_field_condition_operand(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "==",
                FieldAccess(
                    CastExpr("Header *", VarRef("raw", "void *"), "BitCast", "Header *"),
                    "type",
                    "int",
                ),
                IntLiteral(4, "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_Header_raw_type_eq_4",
                ["((Header *)raw)->type = 4;"],
                "if ((Header *)raw)->type == 4",
            ), (
                "ir_if_0_false_Header_raw_type_ne_4",
                ["((Header *)raw)->type = 0;"],
                "if ((Header *)raw)->type != 4",
            )],
        )

    def test_shapes_flipped_equality_condition_operand(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "==",
                IntLiteral(7, "int"),
                FieldAccess(VarRef("ctx", "Context *"), "kind", "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_ctx_kind_eq_7",
                ["ctx->kind = 7;"],
                "if ctx->kind == 7",
            ), (
                "ir_if_0_false_ctx_kind_ne_7",
                ["ctx->kind = 0;"],
                "if ctx->kind != 7",
            )],
        )

    def test_shapes_flipped_ordering_condition_operand(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "<",
                IntLiteral(10, "int"),
                FieldAccess(VarRef("ctx", "Context *"), "len", "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_ctx_len_gt_10",
                ["ctx->len = ((10) + 1);"],
                "if ctx->len > 10",
            ), (
                "ir_if_0_false_ctx_len_le_10",
                ["ctx->len = 10;"],
                "if ctx->len <= 10",
            )],
        )

    def test_shapes_decoded_byte_order_equality_condition(self):
        func = FunctionIR(
            "step",
            [
                DeclarationStmt(
                    "port",
                    "uint16_t",
                    CallExpr("ns_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "port", "uint16_t")]),
                ),
                IfStmt(BinaryOp("==", VarRef("port", "uint16_t"), IntLiteral(80, "int"))),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops_with_byte_order(func))

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_port_eq_80",
                ["hdr->port = ns_htons(80);"],
                "if port == 80",
            ), (
                "ir_if_0_false_port_ne_80",
                ["hdr->port = ns_htons(0);"],
                "if port != 80",
            )],
        )

    def test_shapes_copied_decoded_byte_order_ordering_condition(self):
        func = FunctionIR(
            "step",
            [
                DeclarationStmt(
                    "length",
                    "uint16_t",
                    CallExpr("custom_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "length", "uint16_t")]),
                ),
                DeclarationStmt("alias", "uint16_t", VarRef("length", "uint16_t")),
                IfStmt(BinaryOp(">", VarRef("alias", "uint16_t"), IntLiteral(8, "int"))),
            ],
        )

        candidates = condition_candidates_from_ir(func, _ops_with_byte_order(func))

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_length_gt_8",
                ["hdr->length = custom_htons(((8) + 1));"],
                "if length > 8",
            ), (
                "ir_if_0_false_length_le_8",
                ["hdr->length = custom_htons(8);"],
                "if length <= 8",
            )],
        )

    def test_shapes_array_subscript_condition_operand(self):
        func = FunctionIR(
            "step",
            [IfStmt(BinaryOp(
                "<",
                ArraySubscript(VarRef("items", "int *"), IntLiteral(2, "int"), "int"),
                IntLiteral(8, "int"),
            ))],
        )

        candidates = condition_candidates_from_ir(func, _ops())

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.target_branch) for candidate in candidates],
            [(
                "ir_if_0_items_2_lt_8",
                ["items[2] = ((8) > 0 ? (8) - 1 : 0);"],
                "if items[2] < 8",
            ), (
                "ir_if_0_false_items_2_ge_8",
                ["items[2] = 8;"],
                "if items[2] >= 8",
            )],
        )


if __name__ == "__main__":
    unittest.main()
