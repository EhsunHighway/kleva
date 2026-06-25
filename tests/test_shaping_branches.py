import re
import unittest

from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.fixtures.construction import safe_c_name
from kleva.shaping.branches import BranchShapeOps, source_branch_candidates
from kleva.shaping.candidates import BranchCandidate


def _param(name, raw_type, base_type, is_pointer=True):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


def _ops(body, delegates=None):
    delegates = delegates or {}
    return BranchShapeOps(
        lambda _source, _name: body,
        lambda _body, _params: {"hdr": ("Header", "pkt->data")},
        lambda _body: {},
        lambda _body: {},
        lambda _body: {},
        lambda _body, _aliases: ["fix_checksum();"],
        lambda *_args: ["guard_pointer();"],
        lambda *_args: ["backing_storage();"],
        lambda cast_type, expr, field: f"(({cast_type} *){expr})->{field}",
        lambda fn: fn.replace("ntoh", "hton"),
        lambda value: "1" if value == "0" else "0",
        lambda value: bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Z][A-Z0-9_]*", value)),
        safe_c_name,
        lambda param: param.base_type == "void" and param.is_pointer,
        delegates.get("loop", lambda *_args: []),
        delegates.get("state", lambda *_args: []),
        delegates.get("fallback", lambda *_args: []),
        delegates.get("callee", lambda *_args: []),
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
            {"casted-fields"},
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
            set(),
            _ops(body, delegates),
        )

        self.assertEqual([candidate.name for candidate in candidates], ["same", "loop_only", "state_only"])


if __name__ == "__main__":
    unittest.main()
