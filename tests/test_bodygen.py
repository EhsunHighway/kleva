import unittest

from kleva.acsl import ACSLBehavior
from kleva.ast.model import CFunction, CParam
from kleva.bodygen import (
    BodyGenOps,
    gen_null_setup_body,
    gen_valid_setup_body,
    param_ref_from_arg,
)


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
        pointer_argument_setup=lambda p, *_args: (
            [f"{p.base_type} {p.name}_obj;"],
            f"&{p.name}_obj",
            [],
        ),
        needs_len_data_shape=lambda *_args: False,
        append_len_data_shape=lambda _lines, _arg: None,
        param_ref_from_arg=param_ref_from_arg,
        function_frees_param=lambda *_args: False,
        function_takes_param_ownership=lambda *_args: False,
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

        lines, outputs, cleanup, preamble = gen_null_setup_body(
            func,
            ["ctx"],
            behavior,
            None,
            None,
            None,
            None,
            _ops(),
        )

        self.assertIn("int out_ret = run(NULL, 0);", lines)
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


if __name__ == "__main__":
    unittest.main()
