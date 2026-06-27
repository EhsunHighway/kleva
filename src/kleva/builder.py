"""
builder.py — Convert a FunctionSpec + ktest objects → a Recipe.

Responsibilities
────────────────
1. For every .ktest file in the function's ktest_dir:
     a. Parse the symbolic objects with ktest-tool.
     b. Map each InputSpec to a C variable declaration.
     c. Evaluate skip_if expressions against the scalar value.
     d. Return a Recipe (or None if skipped).

2. Collect all recipes for a function via build_recipes_for_function().

C declaration rules
───────────────────
  Scalar (c_type does NOT end in "[]"):
      size_t cap = (size_t)64ULL;

  Array (c_type ends in "[]"), fixed length:
      uint8_t data[4] = {0x00, 0x01, 0x02, 0x03};

  Array, length from another ktest scalar:
      uint8_t hdr[N] = {...N bytes...};
      where N = min(scalar_value, max_length)

skip_if expressions (advanced)
───────────────────
  Each entry is a Python expression string.  {val} is replaced with
  the actual unsigned integer value before eval().
  Example: "{val} == 0"  or  "{val} > 268435455"
  A recipe is skipped (returns None) if ANY expression evaluates to True.

bounds (preferred over skip_if)
───────────────────
  bounds: {min: 1, max: 268435455}
  Skips any vector where the scalar value is outside [min, max].
  Also auto-generates klee_assume() calls in the KLEE harness.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import FunctionSpec, InputSpec
from .ktest import KTestObject, find_obj, parse_ktest
from .recipe import Recipe


# ── skip condition evaluator ──────────────────────────────────────────────────

def _should_skip(inp: InputSpec, val: int) -> bool:
    # bounds check (preferred: simple min/max declared in YAML)
    if inp.bounds is not None:
        if inp.bounds.min is not None and val < inp.bounds.min:
            return True
        if inp.bounds.max is not None and val > inp.bounds.max:
            return True
    # skip_if (advanced: arbitrary Python expressions)
    for expr in inp.skip_if:
        try:
            if eval(expr.replace("{val}", str(val))):   # noqa: S307 (local dev tool)
                return True
        except Exception:
            pass
    return False


# ── C declaration generators ──────────────────────────────────────────────────

def _scalar_decl(inp: InputSpec, val: int) -> str:
    return f"{inp.c_type} {inp.c_var} = ({inp.c_type}){val}ULL;"


def _array_decl(inp: InputSpec, data: bytes, n: int) -> str:
    n    = min(n, len(data), inp.max_length)
    vals = ", ".join(f"0x{b:02X}" for b in data[:n])
    return f"uint8_t {inp.c_var}[{n}] = {{{vals}}};"


# ── recipe builder ─────────────────────────────────────────────────────────────

def build_recipe(
    spec:       FunctionSpec,
    ktest_objs: list[KTestObject],
    idx:        int,
    ktest_path:  str | None = None,
) -> Optional[Recipe]:
    """
    Produce one Recipe from a FunctionSpec + the ktest objects for one test vector.
    Returns None if any skip_if condition fires or a required ktest object is missing.
    """
    # fn_id: strip the module prefix ("buffer_create" -> "create_tv001")
    parts     = spec.name.split("_", 1)
    fn_suffix = parts[1] if len(parts) == 2 else spec.name
    fn_id     = f"{fn_suffix}_tv{idx:03d}"

    # Collect scalars first; array length_from references need them.
    scalars: dict[str, int] = {}
    for inp in spec.inputs:
        if not inp.c_type.endswith("[]"):
            obj = find_obj(ktest_objs, inp.ktest_name)
            if obj:
                scalars[inp.ktest_name] = obj.uint

    # Build declarations, honouring skip_if.
    decl_lines: list[str] = []
    for inp in spec.inputs:
        obj = find_obj(ktest_objs, inp.ktest_name)
        if obj is None:
            return None     # required symbolic variable not in this ktest

        if inp.c_type.endswith("[]"):
            # Resolve array length
            if inp.length is not None:
                n = inp.length
            elif inp.length_from is not None:
                n = scalars.get(inp.length_from, 0)
                if n == 0:
                    return None
            else:
                n = obj.size
            decl_lines.append(_array_decl(inp, obj.data, n))

        else:
            val = obj.uint
            if _should_skip(inp, val):
                return None
            decl_lines.append(_scalar_decl(inp, val))

    return Recipe(
        fn_id      = fn_id,
        decl_lines = decl_lines,
        body_lines = spec.body,
        cleanup    = spec.cleanup,
        outputs    = spec.outputs,
        preamble   = spec.preamble,
        candidate  = spec.candidate,
        ktest_path = ktest_path,
        source_location = spec.source_location,
        target_branch   = spec.target_branch,
        candidate_origin = spec.candidate_origin,
        candidate_facts = spec.candidate_facts,
    )


# ── collect all recipes for one function ─────────────────────────────────────

def build_recipes_for_function(
    spec:       FunctionSpec,
    ktest_tool: str,
    base_dir:   str = ".",
) -> list[Recipe]:
    """
    Parse every .ktest file in spec.ktest_dir and return a Recipe per file.
    Missing ktest dirs are silently treated as zero recipes.
    """
    ktest_path = Path(spec.ktest_dir)
    if not ktest_path.is_absolute():
        ktest_path = Path(base_dir) / ktest_path

    if not ktest_path.is_dir():
        return []

    recipes: list[Recipe] = []
    for i, kf in enumerate(sorted(ktest_path.glob("*.ktest")), 1):
        try:
            objs = parse_ktest(ktest_tool, str(kf))
        except Exception as exc:
            import sys
            print(f"    [warn] {kf.name}: {exc}", file=sys.stderr)
            continue
        r = build_recipe(spec, objs, i, str(kf))
        if r:
            recipes.append(r)

    return recipes
