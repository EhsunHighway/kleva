import re
import unittest

from kleva.ast.model import CFunction, CParam, CTypeCatalog
from kleva.fixtures.construction import safe_c_name
from kleva.shaping.candidates import CallOutcomeFact
from kleva.shaping.callees import (
    CalleeSuccessOps,
    callee_success_candidates,
    callee_success_setup_for_call,
    invert_simple_return_guard,
    return_guard_conditions,
)


def _append_unique(lines, line, seen):
    if line not in seen:
        seen.add(line)
        lines.append(line)


def _param(name, raw_type, base_type, is_pointer=False):
    return CParam(name, raw_type, base_type, is_pointer, False, False, 0)


def _ops(source_bodies):
    return CalleeSuccessOps(
        lambda _source: {
            "prepare": CFunction(
                "prepare",
                "int",
                "int",
                False,
                [_param("ctx", "Context *ctx", "Context", True)],
            ),
            "check_size": CFunction(
                "check_size",
                "int",
                "int",
                False,
                [_param("size", "int size", "int")],
            ),
        },
        lambda _source, name: source_bodies.get(name, ""),
        lambda raw: [part.strip() for part in raw.split(",") if part.strip()],
        _append_unique,
        lambda value: "1" if value == "0" else "0",
        lambda value: bool(re.fullmatch(r"0x[0-9a-fA-F]+|\d+|[A-Z][A-Z0-9_]*", value)),
        safe_c_name,
    )


class CalleeShapingTests(unittest.TestCase):
    def test_return_guard_conditions_and_inversion(self):
        body = """
            if (!ctx->ready) return -1;
            if (ctx->state == CLOSED) { return -1; }
            if (ctx->skip) do_work();
        """

        guards = return_guard_conditions(body)
        self.assertEqual(guards, ["!ctx->ready", "ctx->state == CLOSED"])
        self.assertEqual(
            invert_simple_return_guard(guards[0], {"ctx"}, _append_unique, lambda value: "not_" + value),
            ["ctx->ready = 1;"],
        )
        self.assertEqual(
            invert_simple_return_guard(guards[1], {"ctx"}, _append_unique, lambda value: "not_" + value),
            ["ctx->state = not_CLOSED;"],
        )

    def test_inverts_scalar_return_guards(self):
        self.assertEqual(
            invert_simple_return_guard("!enabled", {"enabled"}, _append_unique, lambda value: "not_" + value),
            ["enabled = 1;"],
        )
        self.assertEqual(
            invert_simple_return_guard("size == 0", {"size"}, _append_unique, lambda value: "not_" + value),
            ["size = not_0;"],
        )
        self.assertEqual(
            invert_simple_return_guard("count <= 4", {"count"}, _append_unique, lambda value: "not_" + value),
            ["count = ((4) + 1);"],
        )

    def test_callee_success_setup_for_call_inverts_callee_guards(self):
        ops = _ops({"prepare": "if (!ctx->ready) return -1; return 0;"})

        setup, preamble = callee_success_setup_for_call(
            "prepare",
            ["context"],
            "source",
            CTypeCatalog(),
            ops,
        )

        self.assertEqual(setup, ["context->ready = 1;"])
        self.assertEqual(preamble, [])

    def test_callee_success_setup_for_call_inverts_scalar_callee_guards(self):
        ops = _ops({"check_size": "if (size == 0) return -1; return 0;"})

        setup, preamble = callee_success_setup_for_call(
            "check_size",
            ["length"],
            "source",
            CTypeCatalog(),
            ops,
        )

        self.assertEqual(setup, ["length = 1;"])
        self.assertEqual(preamble, [])

    def test_callee_success_candidates_include_prior_source_guard(self):
        body = """
            if (!ctx->enabled) return -1;
            int res = prepare(ctx);
            if (res == -1) return -1;
        """
        ops = _ops({"prepare": "if (!ctx->ready) return -1; return 0;"})

        candidates = callee_success_candidates(
            body,
            "source",
            CTypeCatalog(),
            {"ctx"},
            {"callee-success"},
            ops,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "source_prepare_success")
        self.assertEqual(candidates[0].setup, ["ctx->enabled = 1;", "ctx->ready = 1;"])
        self.assertTrue(candidates[0].witness_outputs)
        self.assertEqual(candidates[0].call_facts, [
            CallOutcomeFact("prepare", "equals_-1", "success"),
        ])


if __name__ == "__main__":
    unittest.main()
