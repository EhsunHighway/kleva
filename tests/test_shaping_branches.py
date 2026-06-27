import re
import unittest

from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.fixtures.construction import safe_c_name
from kleva.ir.model import BinaryOp, CallExpr, CastExpr, DeclarationStmt, FieldAccess, FunctionIR, IfStmt, IntLiteral, SwitchCase, SwitchStmt, VarRef
from kleva.shaping.branches import BranchShapeOps, source_branch_candidates
from kleva.shaping.candidates import BranchCandidate, BranchFact, CallOutcomeFact
from kleva.shaping.ir_byte_order import decoded_field_aliases_from_ir
from kleva.shaping.ir_conditions import IrConditionOps, condition_candidates_from_ir
from kleva.shaping.ir_switches import state_switch_candidates_from_ir


def _param(name, raw_type, base_type, is_pointer=True):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


def _ops(body, delegates=None):
    delegates = delegates or {}
    return BranchShapeOps(
        lambda _source, _name: body,
        lambda _body, _params: {"hdr": ("Header", "pkt->data")},
        delegates.get("decoded", lambda _body: {}),
        lambda _body: {},
        lambda _body: {},
        delegates.get("checksum", lambda _body, _aliases: ["fix_checksum();"]),
        lambda *_args: ["guard_pointer();"],
        delegates.get("backing", lambda *_args: ["backing_storage();"]),
        lambda cast_type, expr, field: f"(({cast_type} *){expr})->{field}",
        lambda fn: fn.replace("ntoh", "hton"),
        lambda value: "1" if value == "0" else "0",
        lambda value: bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Z][A-Z0-9_]*", value)),
        safe_c_name,
        lambda param: param.base_type == "void" and param.is_pointer,
        delegates.get("ir_condition", lambda *_args: []),
        delegates.get("ir_callback", lambda *_args: []),
        delegates.get("ir_callee", lambda *_args: []),
        delegates.get("ir_parser", lambda *_args: []),
        delegates.get("ir_table", lambda *_args: []),
        delegates.get("loop", lambda *_args: []),
        delegates.get("state", lambda *_args: []),
        delegates.get("ir_state", lambda *_args: []),
        delegates.get("fallback", lambda *_args: []),
        delegates.get("callee", lambda *_args: []),
        delegates.get("ir_lookup", lambda *_args: []),
    )


class BranchShapingTests(unittest.TestCase):
    def test_generates_casted_field_and_pointer_field_candidates(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            switch (hdr->type) {
                case OPEN:
                    break;
            }
            if (hdr->code == 0) return -1;
            if (hdr->flag != 0) return -1;
            if (pkt->ready != 0) return -1;
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"casted-fields", "regex-fallbacks"},
            _ops(body),
        )
        names = {candidate.name for candidate in candidates}

        self.assertIn("source_case_OPEN", names)
        self.assertIn("source_default_type", names)
        self.assertIn("source_hdr_code_0", names)
        self.assertIn("source_hdr_flag_eq_0", names)
        self.assertIn("source_hdr_flag_ne_0", names)
        self.assertIn("source_pkt_ready_0", names)
        case = next(candidate for candidate in candidates if candidate.name == "source_case_OPEN")
        self.assertEqual(case.origin, "regex")
        self.assertIn("backing_storage();", case.setup)
        self.assertIn("((Header *)pkt->data)->type = OPEN;", case.setup)
        self.assertIn("fix_checksum();", case.setup)

    def test_dedupes_delegate_candidate_names(self):
        body = "return 0;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        delegates = {
            "loop": lambda *_args: [BranchCandidate("same", ["loop"]), BranchCandidate("loop_only", ["loop"])],
            "state": lambda *_args: [BranchCandidate("same", ["state"]), BranchCandidate("state_only", ["state"])],
            "fallback": lambda *_args: [],
            "callee": lambda *_args: [],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"regex-fallbacks"},
            _ops(body, delegates),
        )

        self.assertEqual([candidate.name for candidate in candidates], ["same", "loop_only", "state_only"])

    def test_adds_ir_state_switch_candidates(self):
        body = "return 0;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        delegates = {
            "ir_state": lambda _ir: [BranchCandidate("ir_case_state_1", ["ctx->state = 1;"])],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"state-switches"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [("ir_case_state_1", ["ctx->state = 1;"])],
        )

    def test_adds_ir_condition_candidates(self):
        body = "return 0;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        delegates = {
            "ir_condition": lambda _ir: [BranchCandidate("ir_if_0_state_eq_1", ["ctx->state = 1;"])],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"branch-conditions"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [("ir_if_0_state_eq_1", ["ctx->state = 1;"])],
        )

    def test_prefers_ir_candidate_when_regex_setup_is_equivalent(self):
        body = "if (ctx->state == 1) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [IfStmt(BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(1)))],
        )
        delegates = {
            "ir_condition": lambda ir: condition_candidates_from_ir(
                ir,
                IrConditionOps(safe_c_name, lambda value: "1" if value == "0" else "0"),
            ),
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"branch-conditions", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_state_eq_1", ["ctx->state = 1;"]),
                ("ir_if_0_false_ctx_state_ne_1", ["ctx->state = 0;"]),
            ],
        )
        self.assertEqual(candidates[0].source_location, "ir:handle:if[0]")
        self.assertEqual(candidates[0].origin, "ir")

    def test_prefers_ir_pointer_field_candidate_when_regex_has_guard_setup(self):
        body = "if (ctx->state == 1) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [IfStmt(BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(1)))],
        )
        delegates = {
            "ir_condition": lambda ir: condition_candidates_from_ir(
                ir,
                IrConditionOps(safe_c_name, lambda value: "1" if value == "0" else "0"),
            ),
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"branch-conditions", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_if_0_ctx_state_eq_1", names)
        self.assertIn("ir_if_0_false_ctx_state_ne_1", names)
        self.assertNotIn("source_ctx_state_eq_1", names)

    def test_prefers_ir_pointer_field_not_equal_candidates_when_regex_has_guard_setup(self):
        body = "if (ctx->state != 0) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [IfStmt(BinaryOp("!=", FieldAccess(VarRef("ctx"), "state"), IntLiteral(0)))],
        )
        delegates = {
            "ir_condition": lambda ir: condition_candidates_from_ir(
                ir,
                IrConditionOps(safe_c_name, lambda value: "1" if value == "0" else "0"),
            ),
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"branch-conditions", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_if_0_ctx_state_ne_0", names)
        self.assertIn("ir_if_0_false_ctx_state_eq_0", names)
        self.assertNotIn("source_ctx_state_0", names)
        self.assertNotIn("source_ctx_state_ne_0", names)

    def test_prefers_ir_byte_order_candidate_when_regex_setup_is_equivalent(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            uint16_t port = ns_ntohs(hdr->port);
            if (port == 80) return -1;
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        function_ir = FunctionIR(
            "handle",
            [
                DeclarationStmt("hdr", "Header *", CastExpr("Header *", FieldAccess(VarRef("pkt"), "data"))),
                DeclarationStmt(
                    "port",
                    "uint16_t",
                    CallExpr("ns_ntohs", [FieldAccess(VarRef("hdr", "Header *"), "port", "uint16_t")]),
                ),
                IfStmt(BinaryOp("==", VarRef("port", "uint16_t"), IntLiteral(80, "int"))),
            ],
        )
        delegates = {
            "decoded": lambda _body: {"port": ("ns_ntohs", "hdr", "port")},
            "ir_condition": lambda ir: condition_candidates_from_ir(
                ir,
                IrConditionOps(
                    safe_c_name,
                    lambda value: "1" if value == "0" else "0",
                    decoded_field_aliases_from_ir(ir),
                    lambda fn: fn.replace("ntoh", "hton", 1),
                ),
            ),
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"branch-conditions", "byte-order", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup, candidate.origin) for candidate in candidates],
            [
                ("ir_if_0_port_eq_80", ["((Header *)pkt->data)->port = ns_htons(80);"], "ir"),
                ("ir_if_0_false_port_ne_80", ["((Header *)pkt->data)->port = ns_htons(0);"], "ir"),
            ],
        )

    def test_ir_byte_order_candidate_suppresses_regex_byte_order_fallback(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            uint16_t port = ns_ntohs(hdr->port);
            if (port == 80) return -1;
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        delegates = {
            "decoded": lambda _body: {"port": ("ns_ntohs", "hdr", "port")},
            "ir_condition": lambda _ir: [
                BranchCandidate(
                    "ir_false_port_ne_80",
                    ["custom_false_setup();"],
                    branch_facts=[BranchFact("((Header *)pkt->data)->port", "!=", "80")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"branch-conditions", "byte-order", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_false_port_ne_80", names)
        self.assertNotIn("source_port_tmp_80", names)
        self.assertNotIn("source_port_not_eq_80", names)

    def test_ir_condition_candidates_disable_regex_byte_order_fallback(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            uint16_t port = ns_ntohs(hdr->port);
            if (port == 0) return -1;
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        delegates = {
            "decoded": lambda _body: {"port": ("ns_ntohs", "hdr", "port")},
            "ir_condition": lambda _ir: [
                BranchCandidate(
                    "ir_if_0_port_eq_0",
                    ["((Header *)pkt->data)->port = ns_htons(0);"],
                    branch_facts=[BranchFact("((Header *)pkt->data)->port", "==", "0")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"branch-conditions", "byte-order", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_if_0_port_eq_0", names)
        self.assertNotIn("source_port_tmp_0", names)
        self.assertNotIn("source_port_not_eq_0", names)

    def test_prefers_ir_casted_switch_candidate_when_regex_setup_is_equivalent(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            switch (hdr->type) {
                case OPEN:
                    break;
            }
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        function_ir = FunctionIR(
            "handle",
            [
                DeclarationStmt("hdr", "Header *", CastExpr("Header *", FieldAccess(VarRef("pkt"), "data"))),
                SwitchStmt(FieldAccess(VarRef("hdr", "Header *"), "type", "int"), [SwitchCase("OPEN")]),
            ],
        )
        delegates = {
            "ir_state": state_switch_candidates_from_ir,
            "checksum": lambda *_args: [],
            "backing": lambda *_args: [],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"state-switches", "casted-fields", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        self.assertIn(
            ("ir_case_type_OPEN", ["((Header *)pkt->data)->type = OPEN;"], "ir"),
            [(candidate.name, candidate.setup, candidate.origin) for candidate in candidates],
        )
        self.assertNotIn("source_case_OPEN", {candidate.name for candidate in candidates})

    def test_prefers_ir_casted_switch_candidate_when_regex_has_extra_harness_setup(self):
        body = """
            Header *hdr = (Header *)pkt->data;
            switch (hdr->type) {
                case OPEN:
                    break;
            }
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        function_ir = FunctionIR(
            "handle",
            [
                DeclarationStmt("hdr", "Header *", CastExpr("Header *", FieldAccess(VarRef("pkt"), "data"))),
                SwitchStmt(FieldAccess(VarRef("hdr", "Header *"), "type", "int"), [SwitchCase("OPEN")]),
            ],
        )
        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"state-switches", "casted-fields", "regex-fallbacks"},
            _ops(body, {"ir_state": state_switch_candidates_from_ir}),
            function_ir=function_ir,
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_case_type_OPEN", names)
        self.assertNotIn("source_case_OPEN", names)
        self.assertIn("source_default_type", names)

    def test_prefers_ir_state_switch_candidate_over_regex_delegate_by_fact(self):
        body = "switch (ctx->state) { case 1: break; }"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [SwitchStmt(FieldAccess(VarRef("ctx", "Context *"), "state", "int"), [SwitchCase(1)])],
        )
        delegates = {
            "ir_state": state_switch_candidates_from_ir,
            "state": lambda *_args: [
                BranchCandidate(
                    "source_state_1",
                    ["guard_pointer();", "ctx->state = 1;"],
                    branch_facts=[BranchFact("ctx->state", "case", "1")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"state-switches", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_case_state_1", names)
        self.assertNotIn("source_state_1", names)

    def test_prefers_ir_fact_when_regex_uses_numeric_macro_spelling(self):
        body = "return 0;"
        source_text = "#define HEADER_LEN 20\nint handle(Packet *pkt) { return 0; }"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("pkt", "Packet *", "Packet")],
        )
        delegates = {
            "ir_condition": lambda _ir: [
                BranchCandidate(
                    "ir_header_field_zero",
                    ["((Header *)(pkt->data - 20))->field = 0;"],
                    branch_facts=[BranchFact("((Header *)(pkt->data - 20))->field", "==", "0")],
                )
            ],
            "fallback": lambda *_args: [
                BranchCandidate(
                    "source_header_field_zero",
                    ["((Header *)(pkt->data - HEADER_LEN))->field = 0;"],
                    branch_facts=[BranchFact("((Header *)(pkt->data - HEADER_LEN))->field", "==", "0")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            source_text,
            CTypeCatalog(),
            {"branch-conditions", "fallback-lookups", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=FunctionIR("handle", []),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_header_field_zero", names)
        self.assertNotIn("source_header_field_zero", names)

    def test_adds_ir_table_candidates(self):
        body = "return 0;"
        func = CFunction(
            "lookup",
            "int",
            "int",
            False,
            [_param("table", "Table *", "Table")],
        )
        delegates = {
            "ir_table": lambda _ir: [BranchCandidate("ir_table_items_key_hit", ["items[0].key = key;"])],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"loop-tables"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [("ir_table_items_key_hit", ["items[0].key = key;"])],
        )

    def test_prefers_ir_table_candidate_over_regex_delegate_by_fact(self):
        body = "for (...) if (items[i].id == wanted) return 1;"
        func = CFunction(
            "lookup",
            "int",
            "int",
            False,
            [_param("items", "Item *", "Item")],
        )
        delegates = {
            "ir_table": lambda _ir: [
                BranchCandidate(
                    "ir_table_items_id_hit",
                    ["items[0].id = wanted;"],
                    branch_facts=[BranchFact("items[0].id", "==", "wanted")],
                )
            ],
            "loop": lambda *_args: [
                BranchCandidate(
                    "source_items_id_match",
                    ["guard_pointer();", "items[0].id = wanted;"],
                    branch_facts=[BranchFact("items[0].id", "==", "wanted")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"loop-tables", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_table_items_id_hit", names)
        self.assertNotIn("source_items_id_match", names)

    def test_prefers_ir_candidate_over_fallback_lookup_delegate_by_fact(self):
        body = "if (!exact && allow) { fallback = find_any(table); } if (fallback) return 1;"
        func = CFunction(
            "lookup",
            "int",
            "int",
            False,
            [_param("table", "Table *", "Table")],
        )
        delegates = {
            "ir_table": lambda _ir: [
                BranchCandidate(
                    "ir_table_items_valid_hit",
                    ["table->items[0].valid = 1;"],
                    branch_facts=[BranchFact("table->items[0].valid", "!=", "0")],
                )
            ],
            "fallback": lambda *_args: [
                BranchCandidate(
                    "source_fallback_lookup_hit",
                    ["allow = 1;", "table->items[0].valid = 1;"],
                    branch_facts=[BranchFact("table->items[0].valid", "!=", "0")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"loop-tables", "fallback-lookups", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_table_items_valid_hit", names)
        self.assertNotIn("source_fallback_lookup_hit", names)

    def test_ir_lookup_candidates_disable_regex_lookup_fallback_delegate(self):
        body = "if (!exact && allow) { fallback = find_any(table); } if (fallback) return 1;"
        func = CFunction(
            "lookup",
            "int",
            "int",
            False,
            [_param("table", "Table *", "Table")],
        )
        delegates = {
            "ir_lookup": lambda _ir: [
                BranchCandidate(
                    "ir_fallback_lookup_fallback_1_1",
                    ["table->items[0].valid = 1;"],
                    branch_facts=[BranchFact("table->items[0].valid", "!=", "0")],
                )
            ],
            "fallback": lambda *_args: [
                BranchCandidate(
                    "source_weaker_lookup_guard",
                    ["allow = 1;"],
                    branch_facts=[BranchFact("allow", "!=", "0")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"fallback-lookups", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_fallback_lookup_fallback_1_1", names)
        self.assertNotIn("source_weaker_lookup_guard", names)

    def test_adds_ir_callback_candidates(self):
        body = "return 0;"
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        delegates = {
            "ir_callback": lambda *_args: [BranchCandidate("ir_callback_ctx_handler_present", ["ctx->handler = stub;"])],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"function-pointers"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [("ir_callback_ctx_handler_present", ["ctx->handler = stub;"])],
        )

    def test_adds_ir_callee_candidates(self):
        body = "return 0;"
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        delegates = {
            "ir_callee": lambda *_args: [BranchCandidate("ir_callee_prepare_nonzero_success", [], witness_outputs=True)],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"callee-success"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual([candidate.name for candidate in candidates], ["ir_callee_prepare_nonzero_success"])
        self.assertTrue(candidates[0].witness_outputs)

    def test_prefers_ir_callee_candidate_over_regex_delegate_by_call_fact(self):
        body = "int res = prepare(ctx); if (res == -1) return -1;"
        func = CFunction(
            "run",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        delegates = {
            "ir_callee": lambda *_args: [
                BranchCandidate(
                    "ir_callee_prepare_equals_1_success",
                    ["ctx->ready = 1;"],
                    witness_outputs=True,
                    call_facts=[CallOutcomeFact("prepare", "equals_-1", "success")],
                )
            ],
            "callee": lambda *_args: [
                BranchCandidate(
                    "source_prepare_success",
                    ["guard_pointer();", "ctx->ready = 1;"],
                    witness_outputs=True,
                    call_facts=[CallOutcomeFact("prepare", "equals_-1", "success")],
                )
            ],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"callee-success", "regex-fallbacks"},
            _ops(body, delegates),
            function_ir=object(),
        )

        names = {candidate.name for candidate in candidates}
        self.assertIn("ir_callee_prepare_equals_1_success", names)
        self.assertNotIn("source_prepare_success", names)

    def test_adds_ir_parser_candidates(self):
        body = "return 0;"
        func = CFunction(
            "parse",
            "int",
            "int",
            False,
            [_param("input", "Input *", "Input")],
        )
        delegates = {
            "ir_parser": lambda _ir: [BranchCandidate("ir_min_guard_0_input_size_lt_8_too_low", ["input->size = 7;"])],
        }

        candidates = source_branch_candidates(
            func,
            "source",
            CTypeCatalog(),
            {"parser-headers"},
            _ops(body, delegates),
            function_ir=object(),
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [("ir_min_guard_0_input_size_lt_8_too_low", ["input->size = 7;"])],
        )

    def test_regex_fallbacks_flag_suppresses_text_candidates(self):
        body = "if (ctx->state == 1) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            set(),
            _ops(body),
        )

        self.assertEqual(candidates, [])

    def test_regex_fallbacks_off_keeps_ir_candidates(self):
        body = "if (ctx->state == 1) return -1;"
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )
        function_ir = FunctionIR(
            "handle",
            [IfStmt(BinaryOp("==", FieldAccess(VarRef("ctx"), "state"), IntLiteral(1)))],
        )
        delegates = {
            "ir_condition": lambda ir: condition_candidates_from_ir(
                ir,
                IrConditionOps(safe_c_name, lambda value: "1" if value == "0" else "0"),
            ),
        }

        candidates = source_branch_candidates(
            func,
            "source",
            None,
            {"branch-conditions"},
            _ops(body, delegates),
            function_ir=function_ir,
        )

        self.assertEqual(
            [(candidate.name, candidate.setup) for candidate in candidates],
            [
                ("ir_if_0_ctx_state_eq_1", ["ctx->state = 1;"]),
                ("ir_if_0_false_ctx_state_ne_1", ["ctx->state = 0;"]),
            ],
        )
        self.assertEqual(candidates[0].origin, "ir")

    def test_branch_shaping_does_not_scan_helper_bodies_as_caller_body(self):
        from kleva.synth_ops import _source_branch_candidates

        source = """
            int helper(Context *ctx) {
                if (ctx->state == 7) return -1;
                return 0;
            }

            int handle(Context *ctx) {
                return helper(ctx);
            }
        """
        func = CFunction(
            "handle",
            "int",
            "int",
            False,
            [_param("ctx", "Context *", "Context")],
        )

        candidates = _source_branch_candidates(
            func,
            None,
            source,
            CTypeCatalog(),
            {"regex-fallbacks"},
        )

        self.assertNotIn("source_ctx_state_eq_7", {candidate.name for candidate in candidates})


if __name__ == "__main__":
    unittest.main()
