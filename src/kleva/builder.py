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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import FunctionSpec, InputSpec
from .ktest import KTestObject, find_obj, parse_ktest
from .recipe import Recipe

MAX_SCALAR_SWEEP_VALUES = 256
MAX_BOUNDARY_SWEEP_VALUES = 6
DEFAULT_UNBOUNDED_SCALAR_SWEEP = (0, 1, 2)
MAX_CANDIDATE_RECIPES_PER_SPEC = 1


@dataclass(frozen=True)
class RecipeBuildResult:
    recipes: list[Recipe]
    original_count: int
    deduped_count: int
    budget_skip_count: int


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
    scalar_overrides: dict[str, int] | None = None,
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
    scalar_overrides = scalar_overrides or {}
    scalars: dict[str, int] = {}
    for inp in spec.inputs:
        if not inp.c_type.endswith("[]"):
            if inp.ktest_name in scalar_overrides:
                scalars[inp.ktest_name] = scalar_overrides[inp.ktest_name]
            else:
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
            val = scalar_overrides.get(inp.ktest_name, obj.uint)
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


def scalar_sweep_values(spec: FunctionSpec, ktest_objs: list[KTestObject]) -> list[dict[str, int]]:
    """
    Return small, generic scalar override sets for recipe expansion.

    KLEE emits path representatives, not necessarily every useful concrete
    scalar. For small finite scalar spaces, KLEVA can cheaply expand recipes
    before EVA so generated unit tests preserve value-level regression
    coverage. The rule is type/name agnostic:

      - tiny bounded scalar spaces are fully enumerated;
      - larger bounded scalar spaces use boundary values;
      - a function with one unbounded scalar input gets a tiny default sweep;
      - larger or multi-scalar spaces are left to KLEE path representatives.
    """
    scalar_inputs = [inp for inp in spec.inputs if not inp.c_type.endswith("[]")]
    bounded: list[tuple[InputSpec, list[int]]] = []
    for inp in scalar_inputs:
        if inp.bounds is None or inp.bounds.min is None or inp.bounds.max is None:
            continue
        lo, hi = inp.bounds.min, inp.bounds.max
        if hi < lo:
            continue
        span = hi - lo + 1
        if span <= MAX_SCALAR_SWEEP_VALUES:
            bounded.append((inp, list(range(lo, hi + 1))))
        else:
            bounded.append((inp, _boundary_values(lo, hi)))

    if len(bounded) == 1:
        inp, values = bounded[0]
        return [{inp.ktest_name: value} for value in values]

    if len(scalar_inputs) == 1:
        inp = scalar_inputs[0]
        if inp.bounds is None:
            if find_obj(ktest_objs, inp.ktest_name) is None:
                return []
            return [{inp.ktest_name: value} for value in DEFAULT_UNBOUNDED_SCALAR_SWEEP]

    return []


def _boundary_values(lo: int, hi: int) -> list[int]:
    values = [lo, lo + 1, hi - 1, hi]
    mid = lo + ((hi - lo) // 2)
    values.append(mid)
    out: list[int] = []
    for value in values:
        if value < lo or value > hi or value in out:
            continue
        out.append(value)
    return out[:MAX_BOUNDARY_SWEEP_VALUES]


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
    return build_recipe_result_for_function(spec, ktest_tool, base_dir).recipes


def build_recipe_result_for_function(
    spec:       FunctionSpec,
    ktest_tool: str,
    base_dir:   str = ".",
) -> RecipeBuildResult:
    """
    Parse every .ktest file in spec.ktest_dir and return recipes plus reduction
    stats.

    Generated implementation candidates can produce several concrete KLEE
    vectors for the same requested branch/oracle shape. Keep one representative
    by default so EVA validates the scenario without paying for equivalent
    scalar-sweep variants. Hand-written or ACSL behavior recipes are not capped.
    """
    ktest_path = Path(spec.ktest_dir)
    if not ktest_path.is_absolute():
        ktest_path = Path(base_dir) / ktest_path

    if not ktest_path.is_dir():
        return RecipeBuildResult([], 0, 0, 0)

    recipes: list[Recipe] = []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    original_count = 0
    deduped_count = 0

    def append_unique(recipe: Recipe | None) -> None:
        nonlocal original_count, deduped_count
        if recipe is None:
            return
        original_count += 1
        key = (tuple(recipe.decl_lines), tuple(recipe.body_lines))
        if key in seen:
            deduped_count += 1
            return
        recipe.fn_id = _renumber_fn_id(recipe.fn_id, len(recipes) + 1)
        seen.add(key)
        recipes.append(recipe)

    for _i, kf in enumerate(sorted(ktest_path.glob("*.ktest")), 1):
        try:
            objs = parse_ktest(ktest_tool, str(kf))
        except Exception as exc:
            import sys
            print(f"    [warn] {kf.name}: {exc}", file=sys.stderr)
            continue
        expansions = scalar_sweep_values(spec, objs)
        if expansions:
            for overrides in expansions:
                append_unique(build_recipe(spec, objs, len(recipes) + 1, str(kf), overrides))
        else:
            append_unique(build_recipe(spec, objs, len(recipes) + 1, str(kf)))

    recipes, budget_skip_count = reduce_equivalent_candidate_recipes(recipes)
    return RecipeBuildResult(recipes, original_count, deduped_count, budget_skip_count)


def reduce_equivalent_candidate_recipes(
    recipes: list[Recipe],
    *,
    max_candidate_recipes: int = MAX_CANDIDATE_RECIPES_PER_SPEC,
) -> tuple[list[Recipe], int]:
    """
    Cap equivalent generated candidate recipes after KLEE expansion.

    This is intentionally generic: the key is the requested source/branch/oracle
    shape, not any project-specific object name. Direct behavior recipes are
    always preserved.
    """
    if max_candidate_recipes <= 0:
        return recipes, 0

    kept: list[Recipe] = []
    counts: dict[tuple, int] = {}
    skipped = 0
    for recipe in recipes:
        if not recipe.candidate:
            kept.append(recipe)
            continue
        key = _candidate_recipe_shape_key(recipe)
        count = counts.get(key, 0)
        if count >= max_candidate_recipes:
            skipped += 1
            continue
        counts[key] = count + 1
        recipe.fn_id = _renumber_fn_id(recipe.fn_id, len(kept) + 1)
        kept.append(recipe)
    return kept, skipped


def _candidate_recipe_shape_key(recipe: Recipe) -> tuple:
    facts = tuple(sorted(tuple(sorted(fact.items())) for fact in recipe.candidate_facts))
    return (
        recipe.candidate_origin,
        recipe.source_location,
        recipe.target_branch,
        tuple(recipe.body_lines),
        tuple(recipe.outputs),
        facts,
    )


def _renumber_fn_id(fn_id: str, idx: int) -> str:
    return re.sub(r"_tv\d+$", f"_tv{idx:03d}", fn_id)
