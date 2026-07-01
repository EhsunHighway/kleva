"""
klee.py — Compile KLEE harnesses to LLVM bitcode and run KLEE.

Compilation pipeline per function:
    harness.c  ──clang──►  harness.bc  ─┐
    source.c   ──clang──►  source.bc   ─┴── llvm-link ──► linked.bc ──klee──► ktest files

Why LLVM bitcode?
    KLEE is an interpreter that runs on LLVM IR (bitcode), not native machine
    code.  That's what lets it fork execution at every branch and explore all
    paths symbolically.  The bitcode clang (llvm@16) produces -emit-llvm output;
    the system clang used for unit tests produces native code instead.

Why llvm-link?
    The harness calls functions defined in source.c.  We need to link both files
    into one bitcode module before KLEE can see all the code it needs to interpret.
"""
from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path

from .config import FunctionSpec, KleeSettings
from .recipe import allocator_redirect_macros_for_lines


# ── bitcode compilation ───────────────────────────────────────────────────────

def _bitcode_flags(klee_include: str, src_inc: str, macros: list[str], extra_includes: list[str] | None = None) -> list[str]:
    """Common clang flags for producing KLEE-compatible LLVM bitcode."""
    flags = [
        "-emit-llvm", "-c", "-g", "-O0",
        "-Xclang", "-disable-O0-optnone",   # keep functions uninlined for KLEE
        f"-I{klee_include}",
        f"-I{src_inc}",
        "-include", "assert.h",
    ]
    for inc in (extra_includes or []):
        flags.append(f"-I{inc}")
    for macro in macros:
        flags.append(f"-D{macro}")
    return flags


def compile_to_bc(
    clang:          str,
    src_c:          str,
    out_bc:         str,
    klee_include:   str,
    src_inc:        str,
    macros:         list[str],
    verbose:        bool = True,
    extra_includes: list[str] | None = None,
) -> None:
    """Compile one C file to LLVM bitcode."""
    cmd = [clang] + _bitcode_flags(klee_include, src_inc, macros, extra_includes) + [src_c, "-o", out_bc]
    if verbose:
        print(f"  clang: {Path(src_c).name} → {Path(out_bc).name}", file=sys.stderr)
    subprocess.run(cmd, check=True)


def link_bitcode(
    llvm_link: str,
    *bc_files:  str,
    out:        str,
    verbose:    bool = True,
) -> None:
    """Link multiple bitcode files into one module with llvm-link."""
    cmd = [llvm_link, *bc_files, "-o", out]
    if verbose:
        names = " + ".join(Path(f).name for f in bc_files)
        print(f"  link: {names} → {Path(out).name}", file=sys.stderr)
    subprocess.run(cmd, check=True)


# ── KLEE runner ───────────────────────────────────────────────────────────────

def run_klee(
    klee:        str,
    linked_bc:   str,
    output_dir:  str,
    max_time:    int = 60,
    extra_flags: list[str] | None = None,
    verbose:     bool = True,
) -> None:
    """
    Run KLEE on a linked bitcode module.

    KLEE explores all symbolic paths and writes one .ktest file per
    distinct execution path it discovers.  It also reports any errors
    (memory access violations, assertion failures, divisions by zero, etc.)
    as .err files in the output directory.
    """
    cmd = [klee, f"--output-dir={output_dir}"]
    if max_time > 0:
        cmd.append(f"--max-time={max_time}")
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(linked_bc)

    shutil.rmtree(output_dir, ignore_errors=True)

    if verbose:
        print(f"  klee → {output_dir}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)

    # KLEE writes stats to stderr; show it so the user can see path counts.
    if verbose and result.stderr:
        for line in result.stderr.splitlines():
            if any(k in line for k in ("KLEE:", "generated", "completed", "error")):
                print(f"    {line}", file=sys.stderr)


# ── high-level per-function runner ────────────────────────────────────────────

def run_klee_for_function(
    spec:            FunctionSpec,
    ks:              KleeSettings,
    src_c:           str,
    src_inc:         str,
    extra_sources:   list[str] | None = None,
    extra_includes:  list[str] | None = None,
    source_included: bool = False,
    base_dir:        str = ".",
    verbose:         bool = True,
) -> Path:
    """
    Compile + link + run KLEE for one function.
    Returns the Path to the ktest output directory.

    Directory layout (all under base_dir / ks.output_base):
        harnesses/klee_{name}.c        ← generated harness source
        klee_{name}.bc                 ← harness bitcode
        {stem}_source.bc               ← primary source bitcode (reused across functions)
        {dep}_source.bc                ← each extra_source bitcode (reused across functions)
        klee_{name}_linked.bc          ← linked harness + all sources
        klee_out_{name}/               ← KLEE output (.ktest files live here)
    """
    out_base  = Path(base_dir) / ks.output_base
    out_base.mkdir(parents=True, exist_ok=True)

    harness_c  = str(out_base / "harnesses" / f"klee_{spec.name}.c")
    harness_bc = str(out_base / f"klee_{spec.name}.bc")
    linked_bc  = str(out_base / f"klee_{spec.name}_linked.bc")
    ktest_dir  = str(out_base / f"klee_out_{spec.name}")

    # Rebuild source bitcode for each run. Reusing stale bitcode is unsafe
    # when the selected clang/LLVM version or include path changes.
    macros = [*ks.macros, *_allocator_macros_for_spec(spec)]

    source_bcs: list[str] = []
    if not source_included:
        src_bc = str(out_base / f"{Path(src_c).stem}_source.bc")
        compile_to_bc(
            ks.klee_clang, src_c, src_bc,
            ks.klee_include, src_inc, macros, verbose,
            extra_includes=extra_includes,
        )
        source_bcs.append(src_bc)

    # Extra dependency bitcode (e.g. event.c when testing scheduler.c).
    extra_bcs: list[str] = []
    for dep_c in (extra_sources or []):
        dep_bc = str(out_base / f"{Path(dep_c).stem}_source.bc")
        compile_to_bc(
            ks.klee_clang, dep_c, dep_bc,
            ks.klee_include, src_inc, macros, verbose,
            extra_includes=extra_includes,
        )
        extra_bcs.append(dep_bc)

    compile_to_bc(
        ks.klee_clang, harness_c, harness_bc,
        ks.klee_include, src_inc, macros, verbose,
        extra_includes=extra_includes,
    )

    link_bitcode(ks.llvm_link, harness_bc, *source_bcs, *extra_bcs, out=linked_bc, verbose=verbose)

    run_klee(
        ks.klee, linked_bc, ktest_dir,
        max_time=ks.max_time, extra_flags=ks.extra_flags, verbose=verbose,
    )

    return Path(ktest_dir)


def _allocator_macros_for_spec(spec: FunctionSpec) -> list[str]:
    return allocator_redirect_macros_for_lines(spec.preamble, spec.body)
