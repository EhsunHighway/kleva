"""CLI-facing entry points for `kleva synth`."""
from __future__ import annotations

import sys
from pathlib import Path

from .acsl import ScannerAcslParser
from .kernel import ProgramInput, build_program_model
from .synth_config import load_helper_call_rules, normalize_shaping_features
from .synth_generate import generate_yaml_from_header


# ── CLI entry point ───────────────────────────────────────────────────────────

def run_synth(
    header: str,
    source: str | None = None,
    include_dir: str | None = None,
    out: str | None = None,
    extra_includes: list[str] | None = None,
    extra_sources: list[str] | None = None,
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
    ir_backend: str = "clang-json",
    preprocess_ir: bool = False,
    include_static_functions: bool = False,
    emit_ir: str | None = None,
    ir_diagnostics: str | None = None,
    helper_rules: list[str] | None = None,
) -> None:
    """
    `kleva synth` entry point: generate YAML from header + ACSL.
    """
    header_path = Path(header)
    if not header_path.exists():
        print(f"kleva synth: header not found: {header_path}", file=sys.stderr)
        sys.exit(1)

    module_name = header_path.stem
    src_path = source or f"../src/{module_name}.c"
    inc_dir = include_dir or str(header_path.parent)
    out_path = out or f"kleva/{module_name}.yaml"

    acsl_parser = ScannerAcslParser()
    try:
        shaping_features = normalize_shaping_features(shaping, no_shaping)
    except ValueError as exc:
        print(f"kleva synth: {exc}", file=sys.stderr)
        sys.exit(1)
    program = build_program_model(
        ProgramInput(
            header_path=header_path,
            source_path=src_path,
            include_dir=inc_dir,
            extra_includes=tuple(extra_includes or []),
            extra_sources=tuple(extra_sources or []),
            ir_backend=ir_backend,
            preprocess_ir=preprocess_ir,
            include_static_functions=include_static_functions,
            acsl_parser=acsl_parser,
            shaping_features=frozenset(shaping_features),
        )
    )
    funcs = program.functions
    program.print_fallback_warnings()
    print(f"kleva synth: found {len(funcs)} function(s) in {header_path.name}", file=sys.stderr)
    for f in funcs:
        print(f"  {f.return_type} {f.name}(...)", file=sys.stderr)

    acsl_specs = program.acsl_specs
    acsl_count = sum(1 for s in acsl_specs.values() if s.behaviors)
    if acsl_count:
        print(f"kleva synth: found ACSL contracts for {acsl_count}/{len(funcs)} function(s)", file=sys.stderr)

    yaml_text = generate_yaml_from_header(
        header_path=str(header_path),
        source_path=src_path,
        include_dir=inc_dir,
        extra_includes=extra_includes or [],
        extra_sources=extra_sources or [],
        output_path=out_path,
        shaping=shaping,
        no_shaping=no_shaping,
        ir_backend=ir_backend,
        preprocess_ir=preprocess_ir,
        include_static_functions=include_static_functions,
        emit_ir_path=emit_ir,
        ir_diagnostics_path=ir_diagnostics,
        helper_call_rules=load_helper_call_rules(helper_rules),
        acsl_parser=acsl_parser,
        program_model=program,
    )

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(yaml_text)
    print(f"kleva synth: wrote {out_file}", file=sys.stderr)
    print(f"Next: kleva all {module_name}.yaml --base-dir .", file=sys.stderr)
