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

import sys
import subprocess
from datetime import datetime
from pathlib import Path

from .builder import build_recipes_for_function
from .codegen import write_klee_harness, write_probe_driver, write_probe_standalone, write_unit_tests
from .config import ModuleConfig
from .eva import parse_singletons, run_eva
from .klee import run_klee_for_function
from .recipe import Recipe


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
    for spec in ordered:
        recipes = build_recipes_for_function(spec, ktest_tool, base_dir)
        log(f"  {spec.name}: {len(recipes)} recipe(s)")
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
    src_path = _resolve(base_dir, cfg.module_source)
    inc_path = _resolve(base_dir, cfg.include_dir)
    probe_dir = Path(probe_path).parent / "probes"
    probe_dir.mkdir(parents=True, exist_ok=True)

    probe_singletons: dict[str, dict[str, int]] = {}
    timed_out: list[str] = []
    for idx, r in enumerate(all_recipes, start=1):
        standalone_path = str(probe_dir / f"probe_{r.fn_id}.c")
        write_probe_standalone(r, standalone_path, cfg.module_header, ts)
        fn = "probe_" + r.fn_id
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
            )
        except subprocess.TimeoutExpired:
            timed_out.append(r.fn_id)
            log(f"    timeout after {cfg.eva_max_time}s")
            continue
        fn_singletons = parse_singletons(eva_log).get(fn, {})
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
    )
    log(
        f"  wrote: {unit_path} "
        f"({proven} EVA-proven assertions, {unproven} unproven, "
        f"{skipped_candidates} skipped candidate recipes)"
    )

    return {
        "recipes":    len(all_recipes),
        "proven":     proven,
        "unproven":   unproven,
        "skipped_candidates": skipped_candidates,
        "probe_file": probe_path,
        "unit_file":  unit_path,
        "emit_unproved": emit_unproved,
        "unproved_unit_file": str(Path(unit_path).with_name(f"{Path(unit_path).stem}_unproved{Path(unit_path).suffix}")),
        "unproved_report_file": str(Path(unit_path).with_name(f"{Path(unit_path).stem}_unproved_report.md")),
    }


def _resolve(base_dir: str, path: str) -> str:
    """Resolve path relative to base_dir unless it is already absolute."""
    p = Path(path)
    return str(p if p.is_absolute() else Path(base_dir) / p)
