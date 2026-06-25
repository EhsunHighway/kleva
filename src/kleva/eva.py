"""
eva.py — Run Frama-C EVA and parse singleton final-state values.

EVA (Value Analysis) abstract-interprets C code and, when the abstract value
of a variable at the end of a function is a singleton set {N}, that N is a
formally-proven concrete value — it can be used directly as an assert() oracle.

EVA log format (relevant lines):
    [eva:final-states] Values at end of function probe_create_tv001:
      out_len ∈ {0}          ← singleton   → use as oracle
      out_cap ∈ {255}        ← singleton
      p ∈ {{ &__malloc_42 }} ← heap address → not a singleton int, skip
      __fc_heap_status ∈ [--..--]  ← not singleton, skip
"""
from __future__ import annotations

import re
import subprocess


def run_eva(
    framac:        str,
    probe_file:    str,
    src_file:      str,
    src_inc:       str,
    precision:     int = 7,
    max_time:      int = 120,
    extra_flags:   list[str] | None = None,
    extra_sources: list[str] | None = None,
    extra_includes: list[str] | None = None,
) -> str:
    """
    Invoke frama-c EVA on (probe_file, src_file, *extra_sources).
    Returns the combined stdout+stderr log string.
    """
    all_incs = [src_inc] + (extra_includes or [])
    inc_args = " ".join(f"-I{d}" for d in all_incs)
    cmd = [
        framac,
        "-eva",
        "-eva-precision", str(precision),
        f"-cpp-extra-args={inc_args}",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd += [probe_file, src_file]
    if extra_sources:
        cmd.extend(extra_sources)

    timeout = None if max_time <= 0 else max_time
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout + result.stderr


# U+2208 ∈ appears literally in Frama-C output (not an ASCII equals)
# EVA emits two formats for final-state blocks:
#   one-line: [eva:final-states] Values at end of function XYZ:
#   two-line:  [eva:final-states]\n  Values at end of function XYZ:
_FN_RE      = re.compile(r'\[eva:final-states\] Values at end of function (\w+):')
_FN_BARE_RE = re.compile(r'^\[eva:final-states\]\s*$')   # bare tag, name on next line
_FN_CONT_RE = re.compile(r'^\s+Values at end of function (\w+):\s*$')  # continuation
_SING_RE    = re.compile(r'^\s+(\w+)\s+\u2208\s+\{(-?\d+)\}\s*$')
_SECT_RE    = re.compile(r'^\[')    # any new top-level Frama-C section marker


def parse_singletons(log: str) -> dict[str, dict[str, int]]:
    """
    Scan an EVA log and extract singleton integer values.

    Returns:
        { function_name: { variable_name: int_value } }

    Only variables whose abstract value is exactly {N} for some integer N are
    included.  Heap addresses, intervals, and multi-element sets are ignored.
    """
    result: dict[str, dict[str, int]] = {}
    cur_fn: str | None = None
    after_bare: bool = False  # saw bare [eva:final-states] — name on next line

    for line in log.splitlines():
        # Standard one-line format: [eva:final-states] Values at end of function XYZ:
        m = _FN_RE.search(line)
        if m:
            cur_fn = m.group(1)
            result[cur_fn] = {}
            after_bare = False
            continue

        # Bare tag — function name appears on the following line
        if _FN_BARE_RE.match(line):
            after_bare = True
            cur_fn = None
            continue

        # Two-line continuation: "  Values at end of function XYZ:"
        if after_bare:
            m = _FN_CONT_RE.match(line)
            if m:
                cur_fn = m.group(1)
                result[cur_fn] = {}
            after_bare = False
            continue

        if cur_fn:
            m = _SING_RE.match(line)
            if m:
                result[cur_fn][m.group(1)] = int(m.group(2))
            elif _SECT_RE.match(line):   # only bare [tag] lines, not indented [N]
                cur_fn = None   # end of this function's block

    return result
