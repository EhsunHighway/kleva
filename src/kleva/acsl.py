"""
acsl.py — Parse ACSL (ANSI/ISO C Specification Language) annotations from C headers.

ACSL annotations appear in C comments: /*@ ... @*/
Each function may have a contract with multiple behaviors:

    /*@
        behavior null:
            assumes p == \\null;
            assigns \\nothing;
            ensures \\result == -1;
        behavior valid:
            assumes \\valid(p);
            assigns *p;
        complete behaviors;
        disjoint behaviors;
    */
    int func(Type *p);

Supported clause extraction:
  - behavior <name>:  (section delimiter)
  - assumes <expr>;   (precondition)
  - ensures <expr>;   (postcondition)
  - assigns <expr>;   (write-effect set)
  - \result           (return value placeholder)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ACSLBehavior:
    """One `behavior <name>: ...` section in a function contract."""
    name:     str
    assumes:  list[str] = field(default_factory=list)
    ensures:  list[str] = field(default_factory=list)
    assigns:  str = ""
    complete: bool = False  # was this labeled `complete behaviors;`?


@dataclass
class ACSLSpec:
    """Full ACSL specification for one function."""
    func_name: str
    behaviors: list[ACSLBehavior] = field(default_factory=list)
    complete:  bool = False


def strip_line_comments(text: str) -> str:
    """Remove // line comments but preserve block comments (we need ACSL)."""
    return re.sub(r"//[^\n]*", "", text)


def extract_acsl_blocks(text: str) -> list[str]:
    """
    Extract raw ACSL annotation blocks: /*@ ... */

    Returns a list of strings, each being the content between /*@ and */.
    Both `@*/` and `*/` closing markers are supported.
    """
    blocks: list[str] = []
    for m in re.finditer(r"/\*@(.*?)\*/", text, re.DOTALL):
        content = m.group(1).strip()
        if content and content not in blocks:
            blocks.append(content)
    return blocks


def parse_acsl_block(block: str) -> list[dict]:
    """
    Parse one ACSL annotation block into a list of behavior dicts.

    Returns:
        [
            {
                "name": "null",
                "assumes": ["p == \\null"],
                "ensures": ["\\result == -1"],
                "assigns": "\\nothing",
            },
            ...
        ]
    """
    # Strip C comment artifacts and newlines, unify whitespace
    block = re.sub(r"\*@|@?\*/", "", block)
    block = re.sub(r"\s*\n\s*", "\n", block).strip()

    behaviors: list[dict] = []
    current: dict | None = None
    current_section: str | None = None  # "assumes", "ensures", "assigns"

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Detect section: "complete behaviors;" or "disjoint behaviors;"
        if re.match(r"^\s*(complete|disjoint)\s+behaviors\s*;\s*$", line):
            continue

        # Detect new behavior: "behavior <name>:"
        m = re.match(r"^\s*behavior\s+(\w+)\s*:\s*$", line)
        if m:
            if current:
                behaviors.append(current)
            current = {"name": m.group(1), "assumes": [], "ensures": [], "assigns": ""}
            current_section = None
            continue

        # Plain ACSL contracts do not have named behaviors. Treat their
        # `requires` clauses as the assumptions of a synthetic valid behavior.
        m = re.match(r"^\s*requires\s+(.*)$", line)
        if m:
            if current is None:
                current = {"name": "valid", "assumes": [], "ensures": [], "assigns": ""}
            current_section = "assumes"
            current["assumes"].append(m.group(1).strip().rstrip(";").strip())
            continue

        # Detect section keyword: assumes / ensures / assigns
        m = re.match(r"^\s*(assumes|ensures|assigns)\s+(.*)$", line)
        if m:
            if current is None:
                current = {"name": "valid", "assumes": [], "ensures": [], "assigns": ""}
            current_section = m.group(1)
            clause_text = m.group(2).strip().rstrip(";").strip()
            if current_section == "assigns":
                current["assigns"] = clause_text
            else:
                current[current_section].append(clause_text)
            continue

        # Continuation of previous section (indented or same line)
        if current_section and current is not None:
            clause_text = line.rstrip(";").strip()
            if current_section == "assigns":
                current["assigns"] += " " + clause_text
            else:
                if current[current_section]:
                    current[current_section][-1] += " " + clause_text
                else:
                    current[current_section].append(clause_text)

    if current:
        behaviors.append(current)

    return behaviors


def find_func_decl_line(header_text: str, func_name: str) -> int:
    """
    Find the approximate line number where a function declaration starts.
    Used to associate ACSL blocks (which appear *before* the declaration)
    with the function they annotate.
    """
    for i, line in enumerate(header_text.splitlines()):
        if re.search(rf"\b{func_name}\s*\(", line):
            return i
    return -1


def associate_acsl_to_funcs(
    header_text: str,
    blocks: list[tuple[int, str]],
) -> dict[str, ACSLSpec]:
    """
    Associate ACSL blocks to their corresponding functions.

    All line-number computations use the ORIGINAL header_text so that
    block positions and function positions are on a consistent basis.

    Args:
        header_text: The full header text (may contain ACSL blocks).
        blocks: List of (line_number, block_content) tuples from the
                original header_text (line_number is 0-based).

    Returns:
        { func_name: ACSLSpec }
    """
    # Build a version of the header with ACSL content blanked out (but
    # keeping the newlines so line numbers don't shift).
    blanked = re.sub(r"/\*@.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                     header_text, flags=re.DOTALL)

    # Find function declarations in the blanked text (same line numbers as original).
    # Declarations often span multiple lines:
    #
    #     int foo(int a,
    #             int b);
    #
    # The older line-by-line matcher only saw single-line prototypes, which made
    # ACSL blocks before multi-line declarations invisible to `kleva synth`.
    func_list: list[tuple[str, int]] = []
    pending: list[str] = []
    pending_start = 0
    for i, line in enumerate(blanked.splitlines()):
        stripped = line.strip()
        if not pending:
            pending_start = i
        pending.append(line)
        decl = " ".join(pending)
        if ";" not in line:
            continue

        # Match: <return_type> <func_name>(...);
        m = re.search(
            r"\b(?:void|int|uint\d+_t|size_t|ssize_t|char|long|short|float|double|"
            r"struct\s+\w+|\w+)(?:\s*\*+\s*|\s+)"
            r"(\w+)\s*\([^)]*\)\s*;",
            decl,
        )
        if m:
            name = m.group(1)
            if not name[0].isupper() and not name.startswith("_"):
                name_line = pending_start
                for offset, pending_line in enumerate(pending):
                    if re.search(rf"\b{re.escape(name)}\s*\(", pending_line):
                        name_line = pending_start + offset
                        break
                func_list.append((name, name_line))
        pending = []

    # For each ACSL block, find the nearest function immediately after it.
    result: dict[str, ACSLSpec] = {}
    for block_line, content in blocks:
        behaviors_raw = parse_acsl_block(content)
        if not behaviors_raw:
            continue

        # Find the function with the smallest line > block_line
        best_func = None
        best_dist = float("inf")
        for name, fline in func_list:
            dist = fline - block_line
            if 0 < dist < best_dist:
                best_func = name
                best_dist = dist

        if best_func is None:
            continue

        spec = ACSLSpec(func_name=best_func)
        for b in behaviors_raw:
            spec.behaviors.append(ACSLBehavior(
                name=b["name"],
                assumes=b.get("assumes", []),
                ensures=b.get("ensures", []),
                assigns=b.get("assigns", ""),
            ))
        result[best_func] = spec

    return result


def parse_acsl(header_path: str | Path) -> dict[str, ACSLSpec]:
    """
    Parse ACSL annotations from a C header file.

    Returns:
        { function_name: ACSLSpec }
    """
    text = Path(header_path).read_text()
    text = strip_line_comments(text)

    # Extract ACSL blocks with their positions
    blocks: list[tuple[int, str]] = []
    for m in re.finditer(r"/\*@(.*?)\*/", text, re.DOTALL):
        line_no = text[:m.start()].count("\n")
        blocks.append((line_no, m.group(1)))

    return associate_acsl_to_funcs(text, blocks)


def parse_acsl_from_text(header_text: str) -> dict[str, ACSLSpec]:
    """Parse ACSL from an in-memory header string (useful for testing)."""
    text = strip_line_comments(header_text)

    blocks: list[tuple[int, str]] = []
    for m in re.finditer(r"/\*@(.*?)\*/", text, re.DOTALL):
        line_no = text[:m.start()].count("\n")
        blocks.append((line_no, m.group(1)))

    return associate_acsl_to_funcs(text, blocks)
