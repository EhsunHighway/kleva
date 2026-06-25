"""
config.py - Load a kleva (KLEE + EVA) YAML module config file.

YAML schema:

    module:
      name:        buffer         # used in generated file names
      header:      buffer.h       # #include in generated C files
      source:      buffer.c       # C file passed to frama-c (relative to include_dir)
      include_dir: .              # passed as -I to frama-c / compiler

    tools:                        # optional; override via CLI or leave as defaults
      ktest_tool: ktest-tool
      framac:     frama-c

    eva:
      precision:   7
      max_time:    120
      extra_flags:
        - -eva-no-alloc-returns-null

    output:
      probe_file: eva/probe.c
      unit_file:  unit/test_gen.c

    functions:
      - name: buffer_create       # C function name
        pure: false               # true = no heap; scheduled first in EVA main()
        ktest_dir: klee_build/klee_out_buffer_create
        inputs:                   # ktest symbolic objects -> C declarations
          - ktest_name: capacity  # name passed to klee_make_symbolic()
            c_type: size_t        # C type for the declaration
            c_var:  cap           # variable name used in body lines
            bounds:               # inclusive value range (applied in both KLEE and gen phases)
              min: 1              #   KLEE: klee_assume(cap >= 1)  |  gen: skip if val < 1
              max: 268435455      #   KLEE: klee_assume(cap <= MAX)  |  gen: skip if val > MAX
            # skip_if / assume: advanced overrides — prefer bounds for simple ranges
        body:                     # verbatim C statements; may use __GUARD__ markers
          - "Buffer *buf = buffer_create(cap);"
          - "__GUARD__(buf)"
          - "size_t out_len = buf->len;"
        outputs: [out_len]        # variables EVA should prove as singletons
        cleanup:
          - "buffer_free(buf);"

Array inputs:
    - ktest_name: header
      c_type: uint8_t[]           # any type ending in [] is treated as a byte array
      c_var:  hdr
      length: 4                   # fixed length
        — or —
      length_from: header_len     # use the uint value of another ktest var as length
      max_length: 256             # cap (default 256)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import os
import shutil
import yaml


def _first_existing_executable(*candidates: str | None) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.sep in candidate:
            if Path(candidate).exists():
                return candidate
        elif shutil.which(candidate):
            return candidate
    return next((c for c in candidates if c), "")


def resolve_klee_clang(configured: str | None = None) -> str:
    return _first_existing_executable(
        os.environ.get("KLEE_CLANG"),
        os.environ.get("LLVM_CLANG"),
        "/usr/local/opt/llvm@16/bin/clang",
        "/opt/homebrew/opt/llvm@16/bin/clang",
        configured,
        "clang",
    )


def resolve_llvm_link(configured: str | None = None) -> str:
    return _first_existing_executable(
        os.environ.get("LLVM_LINK"),
        os.environ.get("KLEE_LLVM_LINK"),
        "/usr/local/opt/llvm@16/bin/llvm-link",
        "/opt/homebrew/opt/llvm@16/bin/llvm-link",
        configured,
        "llvm-link",
    )


def _valid_klee_include(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path).expanduser()
    if (p / "klee" / "klee.h").exists():
        return str(p)
    if p.name == "klee" and (p / "klee.h").exists():
        return str(p.parent)
    return None


def resolve_klee_include(configured: str | None = None) -> str:
    candidates = [
        os.environ.get("KLEE_INCLUDE"),
        str(Path(os.environ["KLEE_HOME"]) / "include") if os.environ.get("KLEE_HOME") else None,
        configured,
        "/Users/ehsuntr/klee/include",
        "/usr/local/include",
        "/opt/homebrew/include",
        "/usr/include",
    ]
    for candidate in candidates:
        resolved = _valid_klee_include(candidate)
        if resolved:
            return resolved
    return configured or "/usr/local/include"


@dataclass
class Bounds:
    """
    Inclusive integer bounds for a scalar input.

    Replaces the old `assume` + `skip_if` pair:
      - `assume: ["cap > 0", "cap <= MAX"]`  (KLEE-specific C syntax)
      - `skip_if: ["{val} == 0", "{val} > MAX"]`  (Python eval syntax)

    With a single, language-agnostic declaration:
      bounds:
        min: 1
        max: 268435455

    kleva applies it automatically in both phases:
      KLEE phase : klee_assume(cap >= 1); klee_assume(cap <= 268435455);
      gen phase  : skip any ktest vector where the value is outside [min, max]
    """
    min: int | None = None
    max: int | None = None


@dataclass
class InputSpec:
    ktest_name:    str
    c_type:        str          # e.g. "size_t", "int", "uint8_t[]"
    c_var:         str
    length:        int | None = None        # fixed array length (EVA/unit)
    length_from:   str | None = None        # scalar ktest var → array length (EVA/unit)
    max_length:    int = 256
    bounds:        Bounds | None = None     # min/max for scalar inputs (preferred)
    skip_if:       list[str] = field(default_factory=list)   # advanced: Python eval exprs
    assume:        list[str] = field(default_factory=list)   # advanced: raw klee_assume() C exprs
    symbolic_size: int | None = None   # array: klee_make_symbolic(buf, SIZE, name)


@dataclass
class KleeSettings:
    """Tools and flags needed to compile harnesses and run KLEE."""
    klee:         str = "klee"
    klee_clang:   str = field(default_factory=resolve_klee_clang)
    llvm_link:    str = field(default_factory=resolve_llvm_link)
    klee_include: str = field(default_factory=resolve_klee_include)
    output_base:  str = "klee_build"    # harnesses/ and klee_out_*/ live here
    max_time:     int = 60              # seconds per harness run (0 = no limit)
    extra_flags:  list[str] = field(default_factory=list)
    macros:       list[str] = field(default_factory=list)  # -D flags for clang


@dataclass
class FunctionSpec:
    name:      str
    ktest_dir: str
    inputs:    list[InputSpec]
    body:      list[str]
    outputs:   list[str]
    cleanup:   list[str]
    pure:      bool = False    # no heap → schedule before heap functions in EVA main()
    preamble:  list[str] = field(default_factory=list)  # top-level C declarations before main()
    candidate: bool = False    # optional generated path; only emitted if EVA proves all outputs


@dataclass
class ModuleConfig:
    module_name:      str
    module_header:    str
    module_source:    str
    include_dir:      str
    extra_sources:      list[str] = field(default_factory=list)  # additional .c files to link into KLEE
    extra_includes:     list[str] = field(default_factory=list)  # additional -I dirs for compilation
    ktest_tool:         str = "ktest-tool"
    framac:             str = "frama-c"
    eva_precision:      int = 7
    eva_max_time:       int = 120
    eva_extra_flags:    list[str] = field(default_factory=list)
    eva_extra_sources:  list[str] = field(default_factory=list)  # EVA-only extra .c files (e.g. libc/string.c)
    probe_file:       str = "eva/probe.c"
    unit_file:        str = "unit/test_gen.c"
    klee_settings:    KleeSettings = field(default_factory=KleeSettings)
    functions:        list[FunctionSpec] = field(default_factory=list)


def _config_from_data(data: dict[str, Any]) -> ModuleConfig:
    mod   = data.get("module", {})
    tools = data.get("tools",  {})
    eva   = data.get("eva",    {})
    out   = data.get("output", {})

    klee_sec = data.get("klee", {})
    cfg = ModuleConfig(
        module_name     = mod["name"],
        module_header   = mod["header"],
        module_source   = mod["source"],
        include_dir     = mod.get("include_dir", "."),
        extra_sources   = mod.get("extra_sources", []),
        extra_includes  = mod.get("extra_includes", []),
        ktest_tool      = tools.get("ktest_tool", "ktest-tool"),
        framac          = tools.get("framac",     "frama-c"),
        eva_precision   = eva.get("precision",    7),
        eva_max_time    = eva.get("max_time",     120),
        eva_extra_flags   = eva.get("extra_flags",   []),
        eva_extra_sources = eva.get("extra_sources", []),
        probe_file      = out.get("probe_file",   "eva/probe.c"),
        unit_file       = out.get("unit_file",    "unit/test_gen.c"),
        klee_settings   = KleeSettings(
            klee         = tools.get("klee",         "klee"),
            klee_clang   = resolve_klee_clang(tools.get("klee_clang")),
            llvm_link    = resolve_llvm_link(tools.get("llvm_link")),
            klee_include = resolve_klee_include(tools.get("klee_include")),
            output_base  = klee_sec.get("output_base", "klee_build"),
            max_time     = klee_sec.get("max_time",    60),
            extra_flags  = klee_sec.get("extra_flags", []),
            macros       = klee_sec.get("macros",      []),
        ),
    )

    for fn_data in data.get("functions", []):
        inputs = [
            InputSpec(
                ktest_name    = inp["ktest_name"],
                c_type        = inp["c_type"],
                c_var         = inp["c_var"],
                length        = inp.get("length"),
                length_from   = inp.get("length_from"),
                max_length    = inp.get("max_length", 256),
                bounds        = (
                    Bounds(
                        min = inp["bounds"].get("min"),
                        max = inp["bounds"].get("max"),
                    )
                    if "bounds" in inp else None
                ),
                skip_if       = inp.get("skip_if",       []),
                assume        = inp.get("assume",        []),
                symbolic_size = inp.get("symbolic_size"),
            )
            for inp in fn_data.get("inputs", [])
        ]
        cfg.functions.append(FunctionSpec(
            name      = fn_data["name"],
            ktest_dir = fn_data["ktest_dir"],
            inputs    = inputs,
            body      = fn_data.get("body",     []),
            outputs   = fn_data.get("outputs",  []),
            cleanup   = fn_data.get("cleanup",  []),
            pure      = fn_data.get("pure",     False),
            preamble  = fn_data.get("preamble", []),
            candidate = fn_data.get("candidate", False),
        ))

    return cfg


def load_config(path: str | Path) -> ModuleConfig:
    """Parse a YAML config file and return a validated ModuleConfig."""
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return _config_from_data(data)


def load_config_text(text: str) -> ModuleConfig:
    """Parse YAML config text and return a validated ModuleConfig."""
    data: dict[str, Any] = yaml.safe_load(text)
    return _config_from_data(data)
