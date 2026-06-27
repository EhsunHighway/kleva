from __future__ import annotations

import sys
from pathlib import Path

from .acsl import ACSLBehavior, AcslParser, ScannerAcslParser
from .ast.model import CTypeCatalog
from .compat.source_fallbacks import fallback_build_type_catalog as _fallback_build_type_catalog
from .compat.source_fallbacks import fallback_function_accepts_null_param as _fallback_function_accepts_null_param
from .compat.source_fallbacks import fallback_function_decl_map as _fallback_function_decl_map
from .compat.source_fallbacks import fallback_parse_header as _fallback_parse_header
from .config import resolve_klee_clang, resolve_klee_include, resolve_llvm_link
from .ir.clang_json import parse_header_function_decls as _parse_header_function_decls
from .ir.clang_json import parse_translation_unit_with_decls_and_types as _parse_ir_translation_unit_with_decls_and_types
from .ir.diagnostics import IrDiagnostic, write_ir_diagnostics as _write_ir_diagnostics
from .ir.serialize import write_ir_json as _write_ir_json
from .shaping.ir_nullability import accepts_null_param_from_ir as _ir_accepts_null_param
from .source_discovery import (
    collect_source_include_headers as _collect_source_include_headers,
    collect_visible_headers as _collect_visible_headers,
    dedupe_paths as _dedupe_paths,
    source_include_names as _source_include_names,
    suggest_extra_sources as _suggest_extra_sources,
)
from .shaping.ir_parsers import HelperCallRule
from .synth_config import normalize_shaping_features
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


def _merge_type_catalogs(fallback: CTypeCatalog, preferred: CTypeCatalog) -> CTypeCatalog:
    merged = CTypeCatalog(
        complete_structs=set(fallback.complete_structs),
        opaque_structs=set(fallback.opaque_structs),
        function_pointers=dict(fallback.function_pointers),
        struct_fields={name: dict(fields) for name, fields in fallback.struct_fields.items()},
    )
    merged.complete_structs.update(preferred.complete_structs)
    merged.opaque_structs.update(preferred.opaque_structs)
    merged.function_pointers.update(preferred.function_pointers)
    for name, fields in preferred.struct_fields.items():
        merged.struct_fields[name] = dict(fields)
    merged.opaque_structs.difference_update(merged.complete_structs)
    return merged

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
    emit_ir_path: str | None = None,
    ir_diagnostics_path: str | None = None,
    helper_call_rules: tuple[HelperCallRule, ...] = (),
    acsl_parser: AcslParser | None = None,
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

    header_text = header_path_obj.read_text()

    # Parse ACSL annotations
    parser = acsl_parser or ScannerAcslParser()
    acsl_specs = parser.parse_file(header_path)

    # Read visible declarations/definitions for type and helper-function detection.
    include_roots = [Path(inc_dir), *(Path(p) for p in extra_includes)]
    fallback_notes: list[str] = []

    try:
        funcs = (
            _parse_header_function_decls(header_path_obj, [str(p) for p in include_roots])
            if ir_backend == "clang-json"
            else _fallback_parse_header(header_path_obj)
        )
        if ir_backend == "off":
            fallback_notes.append("header declarations parsed with source-text fallback because IR backend is off")
    except Exception as exc:
        fallback_notes.append(
            f"header declarations parsed with source-text fallback after clang-json failure: {type(exc).__name__}: {exc}"
        )
        funcs = _fallback_parse_header(header_path_obj)

    for suggested in _suggest_extra_sources(header_path_obj, include_roots, src_path):
        if suggested not in extra_sources:
            extra_sources.append(suggested)
    extra_sources = _dedupe_paths(extra_sources)

    visible_text_parts = _collect_visible_headers(header_path_obj, include_roots)
    visible_text_parts.extend(_collect_source_include_headers(src_path, include_roots))
    if not visible_text_parts:
        visible_text_parts = [header_text]
    for candidate in [src_path, *extra_sources]:
        try:
            visible_text_parts.append(Path(candidate).read_text())
        except FileNotFoundError:
            pass
    source_text = "\n".join(visible_text_parts)
    source_include_names = _source_include_names(src_path, include_roots)
    type_catalog = CTypeCatalog()
    function_decls = {}
    function_irs = {}
    ir_diagnostics: list[IrDiagnostic] = []
    needs_source_metadata_fallback = ir_backend == "off"
    if needs_source_metadata_fallback:
        fallback_notes.append("function/type metadata parsed with source-text fallback because IR backend is off")
    if ir_backend == "clang-json":
        try:
            function_irs, clang_function_decls, clang_type_catalog = _parse_ir_translation_unit_with_decls_and_types(src_path, [str(p) for p in include_roots])
            function_decls.update(clang_function_decls)
            type_catalog = clang_type_catalog
            for extra_source in extra_sources:
                try:
                    _extra_irs, extra_decls, extra_catalog = _parse_ir_translation_unit_with_decls_and_types(extra_source, [str(p) for p in include_roots])
                    function_decls.update(extra_decls)
                    type_catalog = _merge_type_catalogs(type_catalog, extra_catalog)
                except Exception as exc:
                    fallback_notes.append(
                        f"extra source skipped after clang-json failure: {extra_source}: {type(exc).__name__}: {exc}"
                    )
                    pass
            ir_diagnostics.append(IrDiagnostic("clang-json", src_path, "ok"))
        except Exception as exc:
            ir_diagnostics.append(IrDiagnostic("clang-json", src_path, "failed", error=f"{type(exc).__name__}: {exc}"))
            function_irs = {}
            needs_source_metadata_fallback = True
            fallback_notes.append(
                f"function/type metadata parsed with source-text fallback after clang-json failure: {type(exc).__name__}: {exc}"
            )
    elif ir_backend != "off":
        raise ValueError(f"unknown IR backend: {ir_backend}")
    else:
        ir_diagnostics.append(IrDiagnostic("off", src_path, "disabled"))
    if needs_source_metadata_fallback:
        type_catalog = _merge_type_catalogs(type_catalog, _fallback_build_type_catalog(source_text))
        function_decls.update(_fallback_function_decl_map(source_text))
    source_text_for_fallbacks = (
        source_text
        if needs_source_metadata_fallback or "regex-fallbacks" in shaping_features
        else None
    )
    if "regex-fallbacks" in shaping_features:
        fallback_notes.append("source-text branch shapers enabled by regex-fallbacks feature")
    if fallback_notes:
        seen_notes: set[str] = set()
        fallback_notes = [note for note in fallback_notes if not (note in seen_notes or seen_notes.add(note))]
        for note in fallback_notes:
            print(f"kleva synth warning: {note}", file=sys.stderr)
        ir_diagnostics.extend(
            IrDiagnostic("source-fallback", src_path, "used", error=note)
            for note in fallback_notes
        )
    if emit_ir_path:
        _write_ir_json(function_irs, emit_ir_path)
    if ir_diagnostics_path:
        _write_ir_diagnostics(ir_diagnostics, ir_diagnostics_path)
    helper_params = {
        name: tuple(p.name for p in decl.params)
        for name, decl in function_decls.items()
    }

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
        f"  header:      {header_path_obj.name}",
        f"  source:      {src_path}",
        f"  include_dir: {inc_dir}",
    ]

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
                    body, outputs, cleanup, preamble = _gen_null_setup_body(
                        func, null_params, behavior, source_text_for_fallbacks, type_catalog, function_decls, shaping_features,
                        function_ir=func_ir,
                    )
                elif not null_params:
                    # Valid/scalar-only path: generate a concrete call using
                    # object constructors and scalar assumptions.
                    body, outputs, cleanup, preamble = _gen_valid_setup_body(
                        func, valid_params, behavior, source_text_for_fallbacks, type_catalog, function_decls,
                        shaping_features=shaping_features,
                        function_ir=func_ir,
                    )
                else:
                    # Mixed or unknown: handle gracefully
                    body, outputs, cleanup, preamble = _gen_mixed_test(
                        func, behavior, source_text_for_fallbacks, type_catalog, function_decls, shaping_features,
                        function_ir=func_ir,
                    )

                lines.extend(_emit_yaml_function(
                    func, behavior, body, outputs, cleanup, ktest_dir, preamble, source_include_names
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
                current_score = (
                    _extract_result_value(behavior.ensures) is None,
                    len(behavior.assumes),
                )
                best_score = (
                    _extract_result_value(branch_seed.ensures) is None,
                    len(branch_seed.assumes),
                )
                if current_score > best_score:
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
                if candidates:
                    lines.append("")
                    lines.append(f"  # {func.name} — implementation-shaped branch candidates")
                    for candidate in candidates:
                        test_name = f"{func.name}_{candidate.name}"
                        ktest_dir = f"klee_build/klee_out_{test_name}"
                        shaped_behavior = ACSLBehavior(
                            name=candidate.name,
                            assumes=branch_seed.assumes,
                            ensures=branch_seed.ensures,
                            assigns=branch_seed.assigns,
                        )
                        body, outputs, cleanup, preamble = _gen_valid_setup_body(
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
                            object_paths=candidate.object_paths,
                            call_arg_overrides=candidate.call_arg_overrides,
                            witness_setup=candidate.witness_setup,
                            extra_outputs=candidate.extra_outputs,
                        )
                        preamble = [*preamble, *candidate.preamble]
                        lines.extend(_emit_yaml_function(
                            func,
                            shaped_behavior,
                            body,
                            outputs,
                            cleanup,
                            ktest_dir,
                            preamble,
                            source_include_names,
                            candidate=True,
                            source_location=candidate.source_location,
                            target_branch=candidate.target_branch,
                            candidate_origin=candidate.origin,
                            candidate_facts=candidate.semantic_fact_dicts(),
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
                if (
                    _ir_accepts_null_param(func_ir, p.name)
                    if func_ir is not None
                    else _fallback_function_accepts_null_param(source_text_for_fallbacks, func.name, p.name)
                )
            ]
            if nullable_params:
                # Null test for first pointer
                np = nullable_params[0]
                body, outputs, cleanup, preamble = _gen_null_setup_body(
                    func, [np.name],
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    source_text_for_fallbacks,
                    type_catalog,
                    function_decls,
                    shaping_features,
                    function_ir=func_ir,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="null", assumes=[f"{np.name} == \\null"]),
                    body, outputs, cleanup,
                    f"klee_build/klee_out_{func.name}_null",
                    preamble,
                    source_include_names,
                ))

            # Valid test with constructors for all pointer params
            if func.params:
                valid_names = [p.name for p in func.params if p.is_pointer and p.base_type != "char"]
                body, outputs, cleanup, preamble = _gen_valid_setup_body(
                    func, valid_names or ([] if not pointer_params else [pointer_params[0].name]),
                    ACSLBehavior(name="valid", assumes=[]),
                    source_text_for_fallbacks,
                    type_catalog,
                    function_decls,
                    shaping_features=shaping_features,
                    function_ir=func_ir,
                )
                lines.extend(_emit_yaml_function(
                    func,
                    ACSLBehavior(name="valid", assumes=[]),
                    body, outputs, cleanup,
                    f"klee_build/klee_out_{func.name}_valid",
                    preamble,
                    source_include_names,
                ))

    return "\n".join(lines) + "\n"
