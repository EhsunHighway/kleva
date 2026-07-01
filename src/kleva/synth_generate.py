from __future__ import annotations

import re
import sys
from pathlib import Path

from .acsl import ACSLBehavior, AcslParser, ScannerAcslParser
from .config import resolve_klee_clang, resolve_klee_include, resolve_llvm_link
from .ir.diagnostics import write_ir_diagnostics as _write_ir_diagnostics
from .ir.serialize import write_ir_json as _write_ir_json
from .kernel import ProgramInput, ProgramModel, build_program_model
from .shaping.diversity import curated_diversity_candidates
from .shaping.ir_parsers import HelperCallRule
from .shaping.reducer import reduce_branch_candidates
from .synth_config import SCALAR_BOUNDS, normalize_shaping_features
from .synth_ops import (
    _extract_non_null_params,
    _extract_null_params,
    _extract_result_value,
    _extract_valid_params,
    _gen_mixed_test,
    _gen_null_setup_body,
    _gen_valid_setup_body,
    _source_branch_candidates,
)
from .yaml_emit import emit_yaml_function as _emit_yaml_function


# ── Main generator ────────────────────────────────────────────────────────────

def _has_exact_scalar_assumption(assumes: list[str]) -> bool:
    for expr in assumes:
        for part in re.split(r"\|\||&&", expr):
            part = part.strip()
            if re.fullmatch(r"[A-Za-z_]\w*\s*==\s*(?:0x[0-9a-fA-F]+|-?\d+)", part):
                return True
            if re.fullmatch(r"(?:0x[0-9a-fA-F]+|-?\d+)\s*==\s*[A-Za-z_]\w*", part):
                return True
    return False


def _looks_like_failure_behavior(behavior: ACSLBehavior) -> bool:
    name = behavior.name.lower()
    if any(token in name for token in ("bad", "null", "error", "fail", "invalid")):
        return True
    return any(r"\result == \null" in ensure or r"\result == -1" in ensure for ensure in behavior.ensures)


def branch_seed_score(behavior: ACSLBehavior) -> tuple[int, int, int, int]:
    """
    Rank ACSL behaviors for source/IR branch shaping.

    Branch shaping needs a permissive starting state. A behavior that already
    forces a guard, such as `capacity == 0`, is useful as its own ACSL test but
    a poor seed for exploring the opposite branch.
    """
    return (
        0 if _looks_like_failure_behavior(behavior) else 1,
        0 if _has_exact_scalar_assumption(behavior.assumes) else 1,
        1 if _extract_result_value(behavior.ensures) is None else 0,
        len(behavior.assumes),
    )

def generate_yaml_from_header(
    header_path: str,
    source_path: str | None = None,
    include_dir: str | None = None,
    extra_includes: list[str] | None = None,
    extra_sources: list[str] | None = None,
    output_path: str | None = None,
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
    ir_backend: str = "clang-json",
    preprocess_ir: bool = False,
    emit_ir_path: str | None = None,
    ir_diagnostics_path: str | None = None,
    helper_call_rules: tuple[HelperCallRule, ...] = (),
    acsl_parser: AcslParser | None = None,
    program_model: ProgramModel | None = None,
    include_static_functions: bool = False,
) -> str:
    """
    Generate a complete kleva YAML config from a C header with ACSL annotations.

    Unlike `kleva init`, this:
      - Reads ACSL contracts to produce complete body/cleanup/outputs
      - No TODOs — output is ready for `kleva all`
    """
    header_path_obj = Path(header_path)
    module_name = header_path_obj.stem
    src_path = source_path or f"../src/{module_name}.c"
    inc_dir = include_dir or str(header_path_obj.parent)
    out_path = output_path or f"kleva/{module_name}.yaml"
    extra_includes = extra_includes or []
    extra_sources = extra_sources or []
    try:
        shaping_features = normalize_shaping_features(shaping, no_shaping)
    except ValueError as exc:
        print(f"kleva synth: {exc}", file=sys.stderr)
        sys.exit(1)

    parser = acsl_parser or ScannerAcslParser()
    program = program_model
    if program is None:
        program = build_program_model(
            ProgramInput(
                header_path=header_path_obj,
                source_path=src_path,
                include_dir=inc_dir,
                extra_includes=tuple(extra_includes),
                extra_sources=tuple(extra_sources),
                ir_backend=ir_backend,
                preprocess_ir=preprocess_ir,
                include_static_functions=include_static_functions,
                acsl_parser=parser,
                shaping_features=frozenset(shaping_features),
            )
        )
        program.print_fallback_warnings()
    funcs = program.functions
    acsl_specs = program.acsl_specs
    function_irs = program.function_irs
    function_decls = program.function_decls
    type_catalog = program.type_catalog
    source_text_for_fallbacks = program.source_text_for_fallbacks()
    source_include_names = program.source_include_names
    extra_sources = program.extra_sources
    fallback_notes = program.fallback_notes
    source_included = include_static_functions
    module_header = str(Path(src_path).resolve()) if source_included else header_path_obj.name
    if emit_ir_path:
        _write_ir_json(function_irs, emit_ir_path)
    if ir_diagnostics_path:
        _write_ir_diagnostics(program.diagnostics, ir_diagnostics_path)
    helper_params = {
        name: tuple(p.name for p in decl.params)
        for name, decl in function_decls.items()
    }

    def fallback_facts_for(
        function_name: str,
        candidate_name: str | None = None,
        candidate_origin: str | None = None,
    ) -> list[dict[str, str]]:
        return program.fallback_fact_dicts(function_name, candidate_name, candidate_origin)

    klee_clang = resolve_klee_clang()
    llvm_link = resolve_llvm_link()
    klee_include = resolve_klee_include()

    # Build the YAML
    lines: list[str] = [
        f"# kleva YAML — auto-synthesized by `kleva synth` from ACSL annotations",
        f"# Headers: {header_path_obj.name}",
        f"# Shaping: {', '.join(sorted(shaping_features)) if shaping_features else 'none'}",
        f"# Fallbacks: {'used' if fallback_notes else 'none'}",
    ]
    for note in fallback_notes:
        lines.append(f"# Fallback: {note}")
    lines += [
        f"#",
        f"# Usage (from your tests/ directory):",
        f"#   kleva klee {module_name}.yaml --base-dir .",
        f"#   kleva gen  {module_name}.yaml --base-dir .",
        f"#   kleva all  {module_name}.yaml --base-dir .",
        "",
        "module:",
        f"  name:        {module_name}",
        f"  header:      {module_header}",
        f"  source:      {src_path}",
        f"  include_dir: {inc_dir}",
    ]
    if source_included:
        lines.append("  source_included: true")
        lines.append("  # source_included means generated harnesses include the primary .c")
        lines.append("  # directly so static/internal functions are callable.")

    if extra_includes:
        lines.append("  extra_includes:")
        for inc in extra_includes:
            lines.append(f"    - {inc}")

    if extra_sources:
        lines.append("  extra_sources:")
        for src in extra_sources:
            lines.append(f"    - {src}")

    lines += [
        "",
        "tools:",
        "  ktest_tool:   ktest-tool",
        "  klee:         klee",
        f"  klee_clang:   {klee_clang}",
        f"  llvm_link:    {llvm_link}",
        f"  klee_include: {klee_include}",
        "  framac:       frama-c",
        "",
        "eva:",
        "  precision: 7",
        "  extra_flags:",
        "    - -eva-no-alloc-returns-null",
        "    - -eva-auto-loop-unroll",
        "    - \"20\"",
        "",
        "klee:",
        "  output_base: klee_build",
        "  max_time:    60",
        "  macros:",
        '    - "__assert_fail(e,f,l,fn)=__assert_rtn(fn,f,l,e)"',
        "",
        "output:",
        f"  probe_file: eva/eva_{module_name}_kleva.c",
        f"  unit_file:  unit/test_{module_name}_kleva.c",
        "",
        "functions:",
    ]

    # For each function, generate tests based on ACSL behaviors
    for func in funcs:
        spec = acsl_specs.get(func.name)
        func_ir = function_irs.get(func.name)

        if spec and spec.behaviors:
            lines.append("")
            lines.append(f"  # {'─' * 74}")
            lines.append(f"  # {func.name} ({len(spec.behaviors)} ACSL behaviors)")
            lines.append(f"  # {'─' * 74}")

            for behavior in spec.behaviors:
                test_suffix = behavior.name  # "null", "valid", etc.
                null_params = _extract_null_params(behavior.assumes)
                valid_params = list(dict.fromkeys([
                    *_extract_valid_params(behavior.assumes),
                    *_extract_non_null_params(behavior.assumes),
                ]))

                # Determine the test case name
                test_name = f"{func.name}_{test_suffix}"
                ktest_dir = f"klee_build/klee_out_{test_name}"

                if null_params and not valid_params:
                    # Pure null-guard: generate null body
                    result = _gen_null_setup_body(
                        func, null_params, behavior, source_text_for_fallbacks, type_catalog, function_decls, shaping_features,
                        function_ir=func_ir,
                        helper_irs=function_irs,
                        helper_params=helper_params,
                    )
                elif not null_params:
                    # Valid/scalar-only path: generate a concrete call using
                    # object constructors and scalar assumptions.
                    result = _gen_valid_setup_body(
                        func, valid_params, behavior, source_text_for_fallbacks, type_catalog, function_decls,
                        shaping_features=shaping_features,
                        function_ir=func_ir,
                        helper_irs=function_irs,
                        helper_params=helper_params,
                    )
                else:
                    # Mixed or unknown: handle gracefully
                    result = _gen_mixed_test(
                        func, behavior, source_text_for_fallbacks, type_catalog, function_decls, shaping_features,
                        function_ir=func_ir,
                        helper_irs=function_irs,
                        helper_params=helper_params,
                    )

                lines.extend(_emit_yaml_function(
                    func, behavior, result.body, result.outputs, result.cleanup, ktest_dir, result.preamble, source_include_names,
                    inputs=result.inputs,
                    candidate_facts=fallback_facts_for(func.name),
                ))

            branch_seed: ACSLBehavior | None = None
            branch_seed_valid_params: list[str] = []
            for behavior in spec.behaviors:
                null_params = _extract_null_params(behavior.assumes)
                valid_params = list(dict.fromkeys([
                    *_extract_valid_params(behavior.assumes),
                    *_extract_non_null_params(behavior.assumes),
                ]))
                if null_params:
                    continue
                if branch_seed is None:
                    branch_seed = behavior
                    branch_seed_valid_params = valid_params
                    continue
                if branch_seed_score(behavior) > branch_seed_score(branch_seed):
                    branch_seed = behavior
                    branch_seed_valid_params = valid_params

            if branch_seed is not None:
                candidates = _source_branch_candidates(
                    func,
                    branch_seed,
                    source_text_for_fallbacks,
                    type_catalog,
                    shaping_features,
                    function_ir=func_ir,
                    helper_call_rules=helper_call_rules,
                    helper_irs=function_irs,
                    helper_params=helper_params,
                )
                if "test-diversity" in shaping_features:
                    candidates.extend(curated_diversity_candidates(func, SCALAR_BOUNDS, branch_seed.assumes))
                reduction = reduce_branch_candidates(candidates)
                candidates = reduction.kept
                if candidates:
                    lines.append("")
                    lines.append(f"  # {func.name} — implementation-shaped branch candidates")
                    if reduction.original_count != len(candidates):
                        lines.append(
                            f"  # Candidate reduction: kept {len(candidates)} of {reduction.original_count} "
                            f"(deduped {reduction.deduped_count}, budget-skipped {reduction.budget_skip_count})"
                        )
                    for candidate in candidates:
                        test_name = f"{func.name}_{candidate.name}"
                        ktest_dir = f"klee_build/klee_out_{test_name}"
                        shaped_behavior = ACSLBehavior(
                            name=candidate.name,
                            assumes=branch_seed.assumes,
                            ensures=branch_seed.ensures,
                            assigns=branch_seed.assigns,
                        )
                        result = _gen_valid_setup_body(
                            func,
                            branch_seed_valid_params,
                            shaped_behavior,
                            source_text_for_fallbacks,
                            type_catalog,
                            function_decls,
                            extra_setup=candidate.setup,
                            shaping_features=shaping_features,
                            source_shape_oracle=candidate.oracle,
                            source_shape_witnesses=candidate.witness_outputs,
                            function_ir=func_ir,
                            helper_irs=function_irs,
                            helper_params=helper_params,
                            object_paths=candidate.object_paths,
                            call_arg_overrides=candidate.call_arg_overrides,
                            witness_setup=candidate.witness_setup,
                            extra_outputs=candidate.extra_outputs,
                            post_state_facts=candidate.post_state_facts,
                            fixture_requirements=candidate.fixture_requirements,
                            branch_facts=candidate.branch_facts,
                        )
                        preamble = [*result.preamble, *candidate.preamble]
                        lines.extend(_emit_yaml_function(
                            func,
                            shaped_behavior,
                            result.body,
                            result.outputs,
                            result.cleanup,
                            ktest_dir,
                            preamble,
                            source_include_names,
                            candidate=True,
                            source_location=candidate.source_location,
                            target_branch=candidate.target_branch,
                            candidate_origin=candidate.origin,
                            candidate_facts=[
                                *candidate.semantic_fact_dicts(),
                                *fallback_facts_for(func.name, candidate.name, candidate.origin),
                            ],
                            inputs=result.inputs,
                        ))
        else:
            # No ACSL spec: emit a basic test with just function call
            lines.append("")
            lines.append(f"  # {'─' * 74}")
            lines.append(f"  # {func.name} (no ACSL — basic stub)")
            lines.append(f"  # {'─' * 74}")

            # Generate a simple null-guard test only when the source has a
            # recognizable null guard. A pointer parameter alone is not a
            # promise that NULL is a valid input.
            pointer_params = [p for p in func.params if p.is_pointer]
            nullable_params = [
                p for p in pointer_params
                if program.accepts_null_param(func.name, p.name)
            ]
            if nullable_params:
                # Null test for first pointer
                np = nullable_params[0]
                result = _gen_null_setup_body(
                    func, [np.name],
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    source_text_for_fallbacks,
                    type_catalog,
                    function_decls,
                    shaping_features,
                    function_ir=func_ir,
                    helper_irs=function_irs,
                    helper_params=helper_params,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    result.body, result.outputs, result.cleanup,
                    f"klee_build/klee_out_{func.name}_null",
                    result.preamble,
                    source_include_names,
                    inputs=result.inputs,
                    candidate_facts=fallback_facts_for(func.name),
                ))

            # Valid test with constructors for all pointer params
            if func.params:
                valid_names = [p.name for p in func.params if p.is_pointer and p.base_type != "char"]
                result = _gen_valid_setup_body(
                    func, valid_names or ([] if not pointer_params else [pointer_params[0].name]),
                    ACSLBehavior(name="valid", assumes=[]),
                    source_text_for_fallbacks,
                    type_catalog,
                    function_decls,
                    shaping_features=shaping_features,
                    function_ir=func_ir,
                    helper_irs=function_irs,
                    helper_params=helper_params,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="valid", assumes=[]),
                    result.body, result.outputs, result.cleanup,
                    f"klee_build/klee_out_{func.name}_valid",
                    result.preamble,
                    source_include_names,
                    inputs=result.inputs,
                    candidate_facts=fallback_facts_for(func.name),
                ))

    return "\n".join(lines) + "\n"
