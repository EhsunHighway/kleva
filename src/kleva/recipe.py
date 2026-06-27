"""
recipe.py — Recipe dataclass and guard-marker expansion.

A Recipe is the unit of work for one test vector of one C function.
It carries everything the code generator needs to emit both a probe
function (for EVA) and a unit test function.

Guard markers in body_lines
───────────────────────────
Two special pseudo-statements let one list of body lines serve both
the probe driver and the unit test file:

    __GUARD__(expr)
        probe : Frama_C_assume(ptr != 0);
        unit  : assert(ptr != NULL);

    For a non-variable expression such as __GUARD__(ret == 0), the expression is
    used directly:
        probe : Frama_C_assume(ret == 0);
        unit  : assert(ret == 0);

    __GUARD_WITH_CLEANUP__(ptr, cleanup_stmt)
        probe : Frama_C_assume(ptr != 0);
        unit  : assert(ptr != NULL);       (cleanup_stmt is not emitted in unit)

This avoids duplicating the setup logic between probe and unit functions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Recipe:
    fn_id:      str          # unique suffix, e.g. "create_tv001"
    decl_lines: list[str]    # C declarations generated from ktest input mapping
    body_lines: list[str]    # C statements from config (may contain __GUARD__ markers)
    cleanup:    list[str]    # statements appended after body (free calls, etc.)
    outputs:    list[str]    # local variable names EVA should prove as singletons
    preamble:   list[str] = field(default_factory=list)  # top-level C before the test fn
    candidate:  bool = False  # optional generated recipe; skip if EVA cannot prove all outputs
    ktest_path: str | None = None  # concrete KLEE artifact used to build this recipe
    source_location: str | None = None
    target_branch:   str | None = None
    candidate_origin: str | None = None
    candidate_facts: list[dict[str, str]] = field(default_factory=list)


# ── guard marker regex patterns ───────────────────────────────────────────────

_GUARD_RE         = re.compile(r'^__GUARD__\((.+)\)$')
_GUARD_CLEANUP_RE = re.compile(r'^__GUARD_WITH_CLEANUP__\((\w+),\s*(.+)\)$')


def _is_identifier(expr: str) -> bool:
    return re.match(r'^[A-Za-z_]\w*$', expr) is not None


def expand_guard(line: str, *, is_probe: bool, is_klee: bool = False) -> str:
    """
    Expand one __GUARD__ or __GUARD_WITH_CLEANUP__ marker.
    Returns the line unchanged if it contains no marker.
    """
    m = _GUARD_RE.match(line)
    if m:
        v = m.group(1).strip()
        if not _is_identifier(v):
            if is_klee:
                return f"if (!({v})) return 0;"
            return f"Frama_C_assume({v});" if is_probe else f"assert({v});"
        if is_klee:
            return f"if (!{v}) return 0;"
        return f"Frama_C_assume({v} != 0);" if is_probe else f"assert({v} != NULL);"

    m = _GUARD_CLEANUP_RE.match(line)
    if m:
        v, cl = m.group(1), m.group(2)
        if is_klee:
            return f"if (!{v}) {{ {cl}; return 0; }}"
        return (
            f"Frama_C_assume({v} != 0);"
            if is_probe
            else f"assert({v} != NULL);"
        )

    return line
