"""CLI-facing entry points for `kleva synth`."""
from __future__ import annotations

import sys
from pathlib import Path

from .compat.source_fallbacks import fallback_parse_header
from .ir.clang_json import parse_header_function_decls
from .synth_config import SHAPING_FEATURES, load_helper_call_rules
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

    include_roots = [Path(inc_dir), *(Path(p) for p in (extra_includes or []))]
    try:
        funcs = (
            parse_header_function_decls(header_path, [str(p) for p in include_roots])
            if ir_backend == "clang-json"
            else fallback_parse_header(header_path)
        )
    except Exception:
        funcs = fallback_parse_header(header_path)
    print(f"kleva synth: found {len(funcs)} function(s) in {header_path.name}", file=sys.stderr)
    for f in funcs:
        print(f"  {f.return_type} {f.name}(...)", file=sys.stderr)

    # Parse ACSL
    from .acsl import ScannerAcslParser
    acsl_parser = ScannerAcslParser()
    acsl_specs = acsl_parser.parse_file(header_path)
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
        emit_ir_path=emit_ir,
        ir_diagnostics_path=ir_diagnostics,
        helper_call_rules=load_helper_call_rules(helper_rules),
        acsl_parser=acsl_parser,
    )

    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(yaml_text)
    print(f"kleva synth: wrote {out_file}", file=sys.stderr)
    print(f"Next: kleva all {module_name}.yaml --base-dir .", file=sys.stderr)
