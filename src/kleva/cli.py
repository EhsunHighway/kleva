"""
cli.py — `kleva` command.

    kleva synth  <header.h>     [--source FILE] [--include DIR] [--out FILE]
                                Generate a complete YAML from ACSL annotations
    kleva klee   <module.yaml>  [--base-dir DIR]   Phase 0: harnesses + KLEE
    kleva gen    <module.yaml>  [--base-dir DIR]   Phases 1-5: ktests → EVA → unit tests
    kleva all    <module.yaml>  [--base-dir DIR]   klee + gen (full pipeline)
    kleva run    <header.h>     [--mode all|klee|gen] [--base-dir DIR]
                                Synthesize in memory and run without YAML
    kleva quality-report <path> --out report.md
                                Summarize generated KLEVA unit-test quality
    kleva coverage-report <facts.yaml> --out report.md
                                Render report-only candidate coverage facts
    kleva augment <module.yaml> [--source FILE] [--rules FILE]
                                Add source-derived edge cases
    kleva refine <module.yaml>  [--base-dir DIR]   Refine YAML from pipeline output
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config, load_config_text
from .pipeline import run_klee_phase, run_pipeline
from .synth import generate_yaml_from_header, run_synth
from .synth_config import SHAPING_FEATURES, load_helper_call_rules
from .augment import augment_yaml_text, run_augment
from .coverage_report import write_coverage_report
from .quality_report import write_quality_report
from .refiner import run_refine


def _add_help_alias(p: argparse.ArgumentParser) -> None:
    p.add_argument("-help", action="help", help=argparse.SUPPRESS)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("config", help="path to module YAML config file")
    p.add_argument(
        "--base-dir", default=".",
        help="working directory (ktest dirs and output paths resolved from here)",
    )
    p.add_argument("--quiet", "-q", action="store_true")


def _add_emit_unproved_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--emit-unproved",
        choices=("off", "report", "tests", "all"),
        default="off",
        help="emit EVA-unproved candidate diagnostics separately instead of only skipping them",
    )


def _add_synth_input_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("header", help="path to the .h file")
    p.add_argument("--source",  "-s", default=None,
                   help="path to the .c source file (default: guessed from header)")
    p.add_argument("--include", "-I", default=None,
                   help="include_dir for the module (default: header's directory)")
    p.add_argument("--extra-include", dest="extra_includes", action="append",
                   default=[], metavar="DIR",
                   help="additional -I dirs (repeat for multiple)")
    p.add_argument("--extra-source", dest="extra_sources", action="append",
                   default=[], metavar="FILE",
                   help="additional .c files to link (repeat for multiple)")
    shaping_names = ", ".join(["all", "none", *sorted(SHAPING_FEATURES)])
    p.add_argument("--shaping", action="append", default=None, metavar="NAME[,NAME...]",
                   help=f"enable only selected synth shapers; choices: {shaping_names}")
    p.add_argument("--no-shaping", action="append", default=None, metavar="NAME[,NAME...]",
                   help=f"disable selected synth shapers from the default set; choices: {shaping_names}")
    p.add_argument("--ir-backend", choices=("clang-json", "off"), default="clang-json",
                   help="source IR backend for synthesis shaping (default: clang-json)")
    p.add_argument("--preprocess-ir", action="store_true",
                   help="run clang -E first and extract IR from the preprocessed translation unit")
    p.add_argument("--emit-ir", default=None, metavar="FILE",
                   help="write extracted typed IR JSON for inspection")
    p.add_argument("--ir-diagnostics", default=None, metavar="FILE",
                   help="write controlled IR extraction diagnostics JSON")
    p.add_argument("--helper-rules", action="append", default=None, metavar="FILE",
                   help="YAML helper-call repair rules for IR parser shaping (repeat for multiple)")
    p.add_argument("--include-static-functions", action="store_true",
                   help="also target static/internal functions defined in the primary source")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kleva",
        description=(
            "kleva (KLEE + EVA) — KLEE symbolic tests + Frama-C EVA\n"
            "→ formally-proven unit assertions for any C module."
        ),
    )
    _add_help_alias(parser)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── kleva klee ─────────────────────────────────────────────────────────────
    p_klee = sub.add_parser(
        "klee",
        help="Phase 0: generate KLEE harnesses and run KLEE (produces .ktest files)",
    )
    _add_help_alias(p_klee)
    _add_common_args(p_klee)

    # ── kleva gen ──────────────────────────────────────────────────────────────
    p_gen = sub.add_parser(
        "gen",
        help="Phases 1-5: parse .ktest files → EVA probe → EVA singletons → unit tests",
    )
    _add_help_alias(p_gen)
    _add_common_args(p_gen)
    p_gen.add_argument("--ktest-tool", default=None, help="override ktest-tool path")
    p_gen.add_argument("--framac",     default=None, help="override frama-c path")
    p_gen.add_argument("--eva-timeout", type=int, default=None,
                       help="seconds per EVA probe (0 = unlimited)")
    _add_emit_unproved_arg(p_gen)

    # ── kleva all ──────────────────────────────────────────────────────────────
    p_all = sub.add_parser(
        "all",
        help="Full pipeline: klee + gen",
    )
    _add_help_alias(p_all)
    _add_common_args(p_all)
    p_all.add_argument("--ktest-tool", default=None, help="override ktest-tool path")
    p_all.add_argument("--framac",     default=None, help="override frama-c path")
    p_all.add_argument("--eva-timeout", type=int, default=None,
                       help="seconds per EVA probe (0 = unlimited)")
    _add_emit_unproved_arg(p_all)

    # ── kleva synth ────────────────────────────────────────────────────────────
    p_synth = sub.add_parser(
        "synth",
        help="Generate a complete YAML from ACSL annotations (no TODOs)",
    )
    _add_help_alias(p_synth)
    _add_synth_input_args(p_synth)
    p_synth.add_argument("--out", "-o", default=None,
                         help="output YAML path (default: kleva/<module>.yaml)")

    # ── kleva run ─────────────────────────────────────────────────────────────
    p_run = sub.add_parser(
        "run",
        help="Synthesize in memory and run KLEE/EVA without a YAML file",
    )
    _add_help_alias(p_run)
    _add_synth_input_args(p_run)
    p_run.add_argument("--mode", choices=("all", "klee", "gen"), default="all",
                       help="pipeline phase to run (default: all)")
    p_run.add_argument("--base-dir", default=".",
                       help="working directory (ktest dirs and output paths resolved from here)")
    p_run.add_argument("--emit-yaml", default=None,
                       help="optional path to write the synthesized YAML for inspection")
    p_run.add_argument("--rules", "-r", default=None,
                       help="optional augment rules applied in memory before running")
    p_run.add_argument("--ktest-tool", default=None, help="override ktest-tool path")
    p_run.add_argument("--framac",     default=None, help="override frama-c path")
    p_run.add_argument("--eva-timeout", type=int, default=None,
                       help="seconds per EVA probe (0 = unlimited)")
    _add_emit_unproved_arg(p_run)
    p_run.add_argument("--quiet", "-q", action="store_true")

    # ── kleva coverage-report ────────────────────────────────────────────────
    p_coverage = sub.add_parser(
        "coverage-report",
        help="Render external coverage facts as a report-only candidate mapping",
    )
    _add_help_alias(p_coverage)
    p_coverage.add_argument("facts", help="YAML file containing candidates and branches")
    p_coverage.add_argument("--out", "-o", required=True, help="output Markdown report path")

    # ── kleva quality-report ────────────────────────────────────────────────
    p_quality = sub.add_parser(
        "quality-report",
        help="Summarize generated KLEVA unit-test quality",
    )
    _add_help_alias(p_quality)
    p_quality.add_argument("path", help="generated unit test file or directory to scan")
    p_quality.add_argument("--out", "-o", required=True, help="output Markdown report path")

    # ── kleva augment ─────────────────────────────────────────────────────────
    p_augment = sub.add_parser(
        "augment",
        help="Add source-derived edge-case tests to a YAML config",
    )
    _add_help_alias(p_augment)
    p_augment.add_argument("config", help="path to module YAML config file")
    p_augment.add_argument("--source", "-s", default=None,
                           help="path to implementation .c file (default: module.source)")
    p_augment.add_argument("--rules", "-r", default=None,
                           help="YAML file containing augment.rules or a rules list")
    p_augment.add_argument("--out", "-o", default=None,
                           help="output YAML path (default: overwrite config)")

    # ── kleva refine ───────────────────────────────────────────────────────────
    p_refine = sub.add_parser(
        "refine",
        help="Refine YAML from pipeline output (read unit tests, write improved YAML)",
    )
    _add_help_alias(p_refine)
    p_refine.add_argument("config", help="path to module YAML config file")
    p_refine.add_argument("--base-dir", default=".",
                          help="working directory (resolves ktest dirs and unit tests)")
    p_refine.add_argument("--out", "-o", default=None,
                          help="output YAML path (default: overwrite existing config)")

    args = parser.parse_args()

    # ── handle synth before loading a YAML config ─────────────────────────────
    if args.cmd == "synth":
        run_synth(
            header         = args.header,
            source         = args.source,
            include_dir    = args.include,
            out            = args.out,
            extra_includes = args.extra_includes,
            extra_sources  = args.extra_sources,
            shaping        = args.shaping,
            no_shaping     = args.no_shaping,
            ir_backend     = args.ir_backend,
            preprocess_ir  = args.preprocess_ir,
            include_static_functions = args.include_static_functions,
            emit_ir        = args.emit_ir,
            ir_diagnostics = args.ir_diagnostics,
            helper_rules   = args.helper_rules,
        )
        return

    if args.cmd == "run":
        yaml_text = generate_yaml_from_header(
            header_path    = args.header,
            source_path    = args.source,
            include_dir    = args.include,
            extra_includes = args.extra_includes,
            extra_sources  = args.extra_sources,
            shaping        = args.shaping,
            no_shaping     = args.no_shaping,
            ir_backend     = args.ir_backend,
            preprocess_ir  = args.preprocess_ir,
            include_static_functions = args.include_static_functions,
            emit_ir_path   = args.emit_ir,
            ir_diagnostics_path = args.ir_diagnostics,
            helper_call_rules = load_helper_call_rules(args.helper_rules),
        )
        if args.rules:
            yaml_text = augment_yaml_text(
                yaml_text,
                source_path = args.source,
                rules_path  = args.rules,
                base_dir    = args.base_dir,
            )
        if args.emit_yaml:
            emit_path = Path(args.emit_yaml)
            emit_path.parent.mkdir(parents=True, exist_ok=True)
            emit_path.write_text(yaml_text)

        cfg     = load_config_text(yaml_text)
        verbose = not args.quiet
        if args.eva_timeout is not None:
            cfg.eva_max_time = args.eva_timeout

        if args.mode in ("all", "klee"):
            run_klee_phase(cfg, base_dir=args.base_dir, verbose=verbose)

        if args.mode in ("all", "gen"):
            try:
                result = run_pipeline(
                    cfg,
                    ktest_tool = getattr(args, "ktest_tool", None),
                    framac     = getattr(args, "framac",     None),
                    base_dir   = args.base_dir,
                    verbose    = verbose,
                    emit_unproved = args.emit_unproved,
                )
            except ValueError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)
            _print_result(result)
        return

    if args.cmd == "augment":
        run_augment(
            config = args.config,
            source = args.source,
            out    = args.out,
            rules  = args.rules,
        )
        return

    if args.cmd == "coverage-report":
        write_coverage_report(args.facts, args.out)
        print(f"kleva coverage-report: wrote {args.out}", file=sys.stderr)
        return

    if args.cmd == "quality-report":
        write_quality_report(args.path, args.out)
        print(f"kleva quality-report: wrote {args.out}", file=sys.stderr)
        return

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"kleva: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    cfg     = load_config(config_path)
    verbose = not args.quiet
    if getattr(args, "eva_timeout", None) is not None:
        cfg.eva_max_time = args.eva_timeout

    if args.cmd == "klee":
        run_klee_phase(cfg, base_dir=args.base_dir, verbose=verbose)

    elif args.cmd == "gen":
        try:
            result = run_pipeline(
                cfg,
                ktest_tool = getattr(args, "ktest_tool", None),
                framac     = getattr(args, "framac",     None),
                base_dir   = args.base_dir,
                verbose    = verbose,
                emit_unproved = args.emit_unproved,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        _print_result(result)

    elif args.cmd == "all":
        run_klee_phase(cfg, base_dir=args.base_dir, verbose=verbose)
        try:
            result = run_pipeline(
                cfg,
                ktest_tool = getattr(args, "ktest_tool", None),
                framac     = getattr(args, "framac",     None),
                base_dir   = args.base_dir,
                verbose    = verbose,
                emit_unproved = args.emit_unproved,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        _print_result(result)

    elif args.cmd == "refine":
        from .refiner import run_refine
        run_refine(
            config   = args.config,
            base_dir = args.base_dir,
        )


def _print_result(result: dict) -> None:
    text = (
        f"\nkleva done: {result['recipes']} test vectors  |  "
        f"{result['proven']} EVA-proven assertions  |  "
        f"{result['unproven']} unproven  |  "
        f"{result.get('skipped_candidates', 0)} skipped candidates\n"
        f"  probe → {result['probe_file']}\n"
        f"  tests → {result['unit_file']}"
    )
    mode = result.get("emit_unproved", "off")
    if mode in {"tests", "all"}:
        text += f"\n  unproved tests → {result['unproved_unit_file']}"
    if mode in {"report", "all"}:
        text += f"\n  unproved report → {result['unproved_report_file']}"
    print(text)


if __name__ == "__main__":
    main()
