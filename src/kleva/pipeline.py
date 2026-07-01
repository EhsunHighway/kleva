"""
pipeline.py — The five-phase kleva (KLEE + EVA) pipeline.

Phase 1  Parse .ktest files → Recipe objects
Phase 2  Write EVA probe driver (eva/probe.c)
Phase 3  Run Frama-C EVA on the probe driver
Phase 4  Parse EVA log → extract singleton final-state values
Phase 5  Write unit test file with EVA-proven assert() oracles

Why pure functions first?
    Frama-C EVA analyses a single main() that calls all probe functions
    sequentially.  Heap-heavy functions (malloc/free) can accumulate
    abstract state that makes EVA report "NON TERMINATING" for subsequent
    calls.  Pure functions (no heap) placed first get clean analysis.
"""
from __future__ import annotations

import json
import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .builder import build_recipe_result_for_function
from .codegen import write_klee_harness, write_probe_driver, write_probe_standalone, write_unit_tests
from .config import ModuleConfig
from .eva import EvaReport, parse_eva_report, run_eva
from .klee import run_klee_for_function
from .recipe import Recipe, allocator_redirect_macros_for_lines


def candidate_recipe_hint(spec: object, recipe_count: int) -> str | None:
    """
    Explain why a generated candidate has no recipes.

    Candidate specs created by no-YAML synthesis still need KLEE output before
    Phase 1 can build recipes from their ktest directory.
    """
    if not getattr(spec, "candidate", False) or recipe_count != 0:
        return None
    ktest_dir = getattr(spec, "ktest_dir", "")
    return (
        "candidate has no recipes; run mode all/klee first or provide KLEE "
        f"outputs in {ktest_dir}"
    )


def run_klee_phase(
    cfg:      ModuleConfig,
    base_dir: str = ".",
    verbose:  bool = True,
) -> None:
    """
    Phase 0: generate KLEE harnesses and run KLEE for every function.

    For each function in cfg.functions:
      a. Write klee_build/harnesses/klee_{name}.c
      b. Compile harness + source to LLVM bitcode
      c. Run KLEE → klee_build/klee_out_{name}/*.ktest

    After this phase you have all the .ktest files that run_gen_phase() needs.
    """
    def log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    ks      = cfg.klee_settings
    src_c   = _resolve(base_dir, cfg.module_source)
    src_inc = _resolve(base_dir, cfg.include_dir)
    ts      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log("=== Phase 0: generating KLEE harnesses ===")
    for spec in cfg.functions:
        harness_path = str(
            Path(base_dir) / ks.output_base / "harnesses" / f"klee_{spec.name}.c"
        )
        write_klee_harness(spec, harness_path, cfg.module_header, ts)
        log(f"  wrote: {harness_path}")

    log("=== Phase 0: running KLEE ===")
    for spec in cfg.functions:
        log(f"  {spec.name} ...")
        run_klee_for_function(
            spec, ks, src_c, src_inc,
            extra_sources=cfg.extra_sources,
            extra_includes=cfg.extra_includes,
            source_included=cfg.source_included,
            base_dir=base_dir, verbose=verbose,
        )


def run_pipeline(
    cfg:        ModuleConfig,
    ktest_tool: str | None = None,
    framac:     str | None = None,
    base_dir:   str = ".",
    verbose:    bool = True,
    emit_unproved: str = "off",
) -> dict:
    """
    Phases 1-5 only (assumes .ktest files already exist from run_klee_phase).
    """
    started_at = time.monotonic()
    ktest_tool = ktest_tool or cfg.ktest_tool
    framac     = framac     or cfg.framac
    ts         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    # ── Phase 1: parse ktest files ────────────────────────────────────────────
    log("=== Phase 1: parsing .ktest files ===")

    # Pure (no-heap) functions first so EVA's heap state stays clean for them.
    ordered = (
        [f for f in cfg.functions if f.pure]
        + [f for f in cfg.functions if not f.pure]
    )

    all_recipes: list[Recipe] = []
    total_recipe_original = 0
    total_recipe_deduped = 0
    total_recipe_budget_skipped = 0
    for spec in ordered:
        result = build_recipe_result_for_function(spec, ktest_tool, base_dir)
        recipes = result.recipes
        total_recipe_original += result.original_count
        total_recipe_deduped += result.deduped_count
        total_recipe_budget_skipped += result.budget_skip_count
        reduction_note = ""
        if result.deduped_count or result.budget_skip_count:
            reduction_note = (
                f" (built {result.original_count}, deduped {result.deduped_count}, "
                f"budget-skipped {result.budget_skip_count})"
            )
        log(f"  {spec.name}: {len(recipes)} recipe(s){reduction_note}")
        if hint := candidate_recipe_hint(spec, len(recipes)):
            log(f"    note: {hint}")
        all_recipes.extend(recipes)

    if not all_recipes:
        log("ERROR: no recipes built — run KLEE first, or check ktest_dir paths.")
        sys.exit(1)
    log(f"  total: {len(all_recipes)} recipe(s)")

    # ── Phase 2: write EVA probe driver ───────────────────────────────────────
    log("=== Phase 2: writing EVA probe driver ===")
    probe_path = _resolve(base_dir, cfg.probe_file)
    write_probe_driver(all_recipes, probe_path, cfg.module_header, ts)
    log(f"  wrote: {probe_path}")

    # ── Phase 3: run Frama-C EVA (one run per probe for clean heap state) ────
    log("=== Phase 3: running Frama-C EVA ===")
    src_path = None if cfg.source_included else _resolve(base_dir, cfg.module_source)
    inc_path = _resolve(base_dir, cfg.include_dir)
    probe_dir = Path(probe_path).parent / "probes"
    probe_dir.mkdir(parents=True, exist_ok=True)
    eva_log_dir = Path(probe_path).parent / "logs"
    eva_log_dir.mkdir(parents=True, exist_ok=True)

    probe_singletons: dict[str, dict[str, int]] = {}
    eva_reports: dict[str, EvaReport] = {}
    timed_out: list[str] = []
    for idx, r in enumerate(all_recipes, start=1):
        standalone_path = str(probe_dir / f"probe_{r.fn_id}.c")
        write_probe_standalone(r, standalone_path, cfg.module_header, ts)
        fn = "probe_" + r.fn_id
        eva_log_path = eva_log_dir / f"{fn}.txt"
        log(f"  EVA {idx}/{len(all_recipes)}: {r.fn_id}")
        try:
            eva_log = run_eva(
                framac,
                standalone_path,
                src_path,
                inc_path,
                precision       = cfg.eva_precision,
                max_time        = cfg.eva_max_time,
                extra_flags     = cfg.eva_extra_flags,
                extra_sources   = [_resolve(base_dir, s) for s in cfg.extra_sources]
                                 + [_resolve(base_dir, s) for s in cfg.eva_extra_sources],
                extra_includes  = [_resolve(base_dir, d) for d in cfg.extra_includes],
                cpp_macros      = _cpp_macros_for_recipe(r),
            )
        except subprocess.TimeoutExpired:
            timed_out.append(r.fn_id)
            eva_log_path.write_text(f"EVA timeout after {cfg.eva_max_time}s\n")
            eva_reports[fn] = parse_eva_report(
                eva_log_path.read_text(),
                raw_log_path=eva_log_path,
                timed_out=True,
            )
            log(f"    timeout after {cfg.eva_max_time}s")
            continue
        eva_log_path.write_text(eva_log)
        report = parse_eva_report(eva_log, raw_log_path=eva_log_path)
        eva_reports[fn] = report
        fn_singletons = report.singletons_for(fn)
        if fn_singletons:
            probe_singletons[fn] = fn_singletons

    # ── Phase 4: parse EVA singletons ─────────────────────────────────────────
    log("=== Phase 4: parsing EVA singletons ===")
    log(f"  probe functions with singletons: {len(probe_singletons)}")
    if timed_out:
        log(f"  EVA timeouts: {len(timed_out)}")

    # ── Phase 5: write unit tests ──────────────────────────────────────────────
    log("=== Phase 5: generating unit tests ===")
    unit_path = _resolve(base_dir, cfg.unit_file)
    proven, unproven, skipped_candidates = write_unit_tests(
        all_recipes,
        probe_singletons,
        unit_path,
        cfg.module_header,
        ts,
        emit_unproved = emit_unproved,
        eva_reports_by_probe = eva_reports,
    )
    log(
        f"  wrote: {unit_path} "
        f"({proven} EVA-proven assertions, {unproven} unproven, "
        f"{skipped_candidates} skipped candidate recipes)"
    )
    duration_seconds = time.monotonic() - started_at
    summary = {
        "recipes": len(all_recipes),
        "recipe_candidates_built": total_recipe_original,
        "recipe_deduped": total_recipe_deduped,
        "recipe_budget_skipped": total_recipe_budget_skipped,
        "proven": proven,
        "unproven": unproven,
        "skipped_candidates": skipped_candidates,
        "duration_seconds": round(duration_seconds, 3),
        "probe_file": str(probe_path),
        "unit_file": str(unit_path),
        "emit_unproved": emit_unproved,
        "source_included": cfg.source_included,
        "unproved_unit_file": str(Path(unit_path).with_name(f"{Path(unit_path).stem}_unproved{Path(unit_path).suffix}")),
        "unproved_report_file": str(Path(unit_path).with_name(f"{Path(unit_path).stem}_unproved_report.md")),
        "eva_log_dir": str(eva_log_dir),
    }
    compile_macros = _native_compile_macros_for_recipes(all_recipes)
    if compile_macros:
        summary["native_compile_macros"] = compile_macros
        summary["native_compile_flags"] = [f"-D{macro}" for macro in compile_macros]
    summary_file = str(Path(unit_path).with_name(f"{Path(unit_path).stem}_summary.json"))
    Path(summary_file).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    return {**summary, "summary_file": summary_file}


def _resolve(base_dir: str, path: str) -> str:
    """Resolve path relative to base_dir unless it is already absolute."""
    p = Path(path)
    return str(p if p.is_absolute() else Path(base_dir) / p)


def _cpp_macros_for_recipe(recipe: Recipe) -> list[str]:
    return allocator_redirect_macros_for_lines(recipe.preamble, recipe.body_lines)


def _native_compile_macros_for_recipes(recipes: list[Recipe]) -> list[str]:
    macros: list[str] = []
    seen: set[str] = set()
    for recipe in recipes:
        for macro in _cpp_macros_for_recipe(recipe):
            if macro in seen:
                continue
            seen.add(macro)
            macros.append(macro)
    return macros
