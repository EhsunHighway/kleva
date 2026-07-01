from __future__ import annotations

import unittest

from kleva.ir.model import ArraySubscript, BinaryOp, CallExpr, CastExpr, DeclarationStmt, ExprStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, ReturnStmt, SourceLocation, UnaryOp, VarRef
from kleva.fixtures.construction import safe_c_name
from kleva.shaping.candidates import BranchFact, CallOutcomeFact
from kleva.shaping.ir_parsers import HelperCallRule, IrParserOps, parser_candidates_from_ir


class IrParserShapingTests(unittest.TestCase):
    def test_generates_boundaries_for_numeric_early_return_guard(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("<", VarRef("size"), IntLiteral(8)),
                    [ReturnStmt(IntLiteral(-1))],
                    SourceLocation("sample.c", 5, 9),
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_min_guard_0_size_lt_8_too_low", ["size = 7;"]),
                ("ir_min_guard_0_size_lt_8_boundary", ["size = 8;"]),
                ("ir_min_guard_0_size_lt_8_valid_high", ["size = 9;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "sample.c:5:9")
        self.assertEqual(candidates[0].target_branch, "min_guard guard size < 8 too_low")
        self.assertEqual(candidates[0].branch_facts, [
            BranchFact("size", "==", "7"),
        ])
        self.assertEqual(candidates[1].branch_facts, [
            BranchFact("size", "==", "8"),
        ])

    def test_handles_field_guards_and_flipped_comparisons(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp(">", IntLiteral(20), FieldAccess(VarRef("input"), "available")),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_min_guard_0_input_available_lt_20_too_low", ["input->available = 19;"]),
                ("ir_min_guard_0_input_available_lt_20_boundary", ["input->available = 20;"]),
                ("ir_min_guard_0_input_available_lt_20_valid_high", ["input->available = 21;"]),
            ],
        )

    def test_ignores_non_returning_guards(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("<", VarRef("size"), IntLiteral(8)),
                    [],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(candidates, [])

    def test_generates_match_and_mismatch_for_equality_guard(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("!=", FieldAccess(VarRef("input"), "tag"), IntLiteral(7)),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_required_value_0_input_tag_ne_7_required", ["input->tag = 7;"]),
                ("ir_required_value_0_input_tag_ne_7_other", ["input->tag = 8;"]),
            ],
        )

    def test_resolves_cast_alias_before_equality_guard(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt(
                    "hdr",
                    "Header *",
                    CastExpr(
                        "Header *",
                        FieldAccess(VarRef("pkt", "Packet *"), "data", "uint8_t *"),
                        "BitCast",
                        "Header *",
                    ),
                ),
                IfStmt(
                    BinaryOp("==", FieldAccess(VarRef("hdr", "Header *"), "code"), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_forbidden_value_0__Header_pkt_data_code_eq_0_forbidden", ["((Header *)pkt->data)->code = 0;"]),
                ("ir_forbidden_value_0__Header_pkt_data_code_eq_0_allowed", ["((Header *)pkt->data)->code = 1;"]),
            ],
        )

    def test_skips_equality_guard_on_function_local_result(self):
        func = FunctionIR(
            "parse",
            [
                DeclarationStmt(
                    "res",
                    "int",
                    CallExpr("send", [VarRef("ctx", "Context *")]),
                ),
                IfStmt(
                    BinaryOp("==", VarRef("res", "int"), IntLiteral(-1)),
                    [ReturnStmt(IntLiteral(-1))],
                ),
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(candidates, [])

    def test_handles_flipped_equality_guard(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("==", IntLiteral(3), FieldAccess(VarRef("input"), "kind")),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_forbidden_value_0_input_kind_eq_3_forbidden", ["input->kind = 3;"]),
                ("ir_forbidden_value_0_input_kind_eq_3_allowed", ["input->kind = 4;"]),
            ],
        )

    def test_forbidden_allowed_candidate_carries_later_object_paths(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("==", FieldAccess(VarRef("queue"), "count"), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(0))],
                ),
                ExprStmt(ArraySubscript(FieldAccess(VarRef("queue"), "items"), IntLiteral(0))),
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))
        allowed = [candidate for candidate in candidates if candidate.name.endswith("_allowed")][0]

        self.assertEqual(
            [(fact.root, fact.path) for fact in allowed.object_paths],
            [("queue", ("items",))],
        )

    def test_parser_continuation_paths_do_not_use_domain_names(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("==", FieldAccess(VarRef("bag"), "used"), IntLiteral(0)),
                    [ReturnStmt(IntLiteral(0))],
                ),
                ExprStmt(ArraySubscript(FieldAccess(VarRef("bag"), "slots"), IntLiteral(0))),
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))
        allowed = [candidate for candidate in candidates if candidate.name.endswith("_allowed")][0]

        self.assertEqual(
            [(fact.root, fact.path) for fact in allowed.object_paths],
            [("bag", ("slots",))],
        )

    def test_splits_compound_numeric_and_equality_guards(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp(
                        "||",
                        BinaryOp("<", FieldAccess(VarRef("input"), "size"), IntLiteral(8)),
                        BinaryOp("!=", FieldAccess(VarRef("input"), "tag"), IntLiteral(7)),
                    ),
                    [ReturnStmt(IntLiteral(-1))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [candidate.name for candidate in candidates],
            [
                "ir_min_guard_0_input_size_lt_8_too_low",
                "ir_min_guard_0_input_size_lt_8_boundary",
                "ir_min_guard_0_input_size_lt_8_valid_high",
                "ir_required_value_0_input_tag_ne_7_required",
                "ir_required_value_0_input_tag_ne_7_other",
            ],
        )

    def test_detects_call_guard_candidates_without_domain_names(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("validate", [VarRef("data"), VarRef("size")]), IntLiteral(0)),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.witness_outputs) for candidate in candidates],
            [
                ("ir_call_guard_0_validate_ne_0_success", [], True),
                ("ir_call_guard_0_validate_ne_0_failure", [], True),
            ],
        )
        self.assertEqual(candidates[0].call_facts, [
            CallOutcomeFact("validate", "ne_0", "success"),
        ])
        self.assertEqual(candidates[1].call_facts, [
            CallOutcomeFact("validate", "ne_0", "failure"),
        ])

    def test_infers_helper_model_from_boolean_return_ir(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("validate", [VarRef("data")]), IntLiteral(0)),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )
        helper_ir = FunctionIR(
            "validate",
            [
                ReturnStmt(BinaryOp(
                    "==",
                    FieldAccess(VarRef("item"), "status"),
                    IntLiteral(0),
                ))
            ],
        )
        ops = IrParserOps(
            safe_c_name,
            helper_irs={"validate": helper_ir},
            helper_params={"validate": ("item",)},
        )

        candidates = parser_candidates_from_ir(func, ops)

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_call_guard_0_validate_ne_0_success", ["data->status = 1;"]),
                ("ir_call_guard_0_validate_ne_0_failure", ["data->status = 0;"]),
            ],
        )

    def test_applies_explicit_helper_call_repair_rule(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("validate", [VarRef("data"), VarRef("size")]), IntLiteral(0)),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )
        ops = IrParserOps(
            safe_c_name,
            (
                HelperCallRule(
                    "validate",
                    success_setup=("{arg0}->status = 0;",),
                    failure_setup=("{arg0}->status = 1;",),
                ),
            ),
        )

        candidates = parser_candidates_from_ir(func, ops)

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_call_guard_0_validate_ne_0_success", ["data->status = 0;"]),
                ("ir_call_guard_0_validate_ne_0_failure", ["data->status = 1;"]),
            ],
        )

    def test_ignores_helper_rule_templates_with_unknown_arguments(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp("!=", CallExpr("validate", [VarRef("data")]), IntLiteral(0)),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )
        ops = IrParserOps(
            safe_c_name,
            (HelperCallRule("validate", success_setup=("{arg1}->status = 0;",)),),
        )

        candidates = parser_candidates_from_ir(func, ops)

        self.assertEqual(candidates[0].setup, [])

    def test_splits_compound_call_guard(self):
        func = FunctionIR(
            "parse",
            [
                IfStmt(
                    BinaryOp(
                        "||",
                        BinaryOp("<", FieldAccess(VarRef("input"), "size"), IntLiteral(8)),
                        BinaryOp("!=", CallExpr("verify", [VarRef("input")]), IntLiteral(0)),
                    ),
                    [ReturnStmt(UnaryOp("-", IntLiteral(1)))],
                )
            ],
        )

        candidates = parser_candidates_from_ir(func, IrParserOps(safe_c_name))

        self.assertEqual(
            [candidate.name for candidate in candidates],
            [
                "ir_min_guard_0_input_size_lt_8_too_low",
                "ir_min_guard_0_input_size_lt_8_boundary",
                "ir_min_guard_0_input_size_lt_8_valid_high",
                "ir_call_guard_0_verify_ne_0_success",
                "ir_call_guard_0_verify_ne_0_failure",
            ],
        )


if __name__ == "__main__":
    unittest.main()
