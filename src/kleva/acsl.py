"""
acsl.py - Parse the small ACSL subset KLEVA uses from C headers.

ACSL annotations appear in C comments immediately before declarations:

    /*@
        behavior null:
            assumes p == \\null;
            assigns \\nothing;
            ensures \\result == -1;
    */
    int func(Type *p);

This module intentionally does not implement the whole ACSL language. It parses
the contract clauses KLEVA consumes:

  - behavior <name>:
  - assumes <expr>;
  - requires <expr>;   mapped to assumes on a synthetic valid behavior
  - ensures <expr>;
  - assigns <expr>;
  - complete behaviors;
  - disjoint behaviors;

The parser is scanner-based instead of regular-expression based. C syntax is
still owned by Clang elsewhere in KLEVA; this file only recognizes ACSL comment
blocks and associates each block with the next C declaration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ACSLBehavior:
    """One `behavior <name>: ...` section in a function contract."""

    name:     str
    assumes:  list[str] = field(default_factory=list)
    ensures:  list[str] = field(default_factory=list)
    assigns:  str = ""
    complete: bool = False


@dataclass
class ACSLSpec:
    """Full ACSL specification for one function."""

    func_name: str
    behaviors: list[ACSLBehavior] = field(default_factory=list)
    complete:  bool = False


class AcslParser(Protocol):
    """Parser interface for ACSL contracts."""

    name: str

    def parse_text(self, header_text: str) -> dict[str, ACSLSpec]:
        ...

    def parse_file(self, header_path: str | Path) -> dict[str, ACSLSpec]:
        ...


@dataclass(frozen=True)
class _AcslBlock:
    start_line: int
    end_offset: int
    content: str


class ScannerAcslParser:
    """Scanner-backed parser for KLEVA's ACSL subset."""

    name = "scanner"

    def parse_text(self, header_text: str) -> dict[str, ACSLSpec]:
        text = strip_line_comments(header_text)
        blocks = _extract_acsl_block_records(text)
        return _associate_block_records_to_funcs(text, blocks)

    def parse_file(self, header_path: str | Path) -> dict[str, ACSLSpec]:
        return self.parse_text(Path(header_path).read_text())


class RegexAcslParser(ScannerAcslParser):
    """
    Backward-compatible parser name.

    Older KLEVA code and downstream users may import `RegexAcslParser`.
    The implementation is now scanner-backed; the class remains as a
    compatibility alias.
    """

    name = "scanner"


def strip_line_comments(text: str) -> str:
    """Remove // line comments while preserving block comments and newlines."""
    out: list[str] = []
    i = 0
    in_string: str | None = None
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch in {'"', "'"}:
            in_string = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            while i < len(text) and text[i] != "\n":
                i += 1
            if i < len(text):
                out.append("\n")
                i += 1
            continue

        out.append(ch)
        i += 1
    return "".join(out)


def extract_acsl_blocks(text: str) -> list[str]:
    """
    Extract raw ACSL annotation blocks.

    Returns the content between `/*@` and `*/`, trimmed and deduplicated.
    """
    blocks: list[str] = []
    for block in _extract_acsl_block_records(text):
        content = block.content.strip()
        if content and content not in blocks:
            blocks.append(content)
    return blocks


def parse_acsl_block(block: str) -> list[dict]:
    """
    Parse one ACSL annotation block into behavior dictionaries.

    This compatibility helper returns dictionaries because older tests and
    callers used that shape directly.
    """
    behaviors = _parse_block_behaviors(block)
    return [
        {
            "name": behavior.name,
            "assumes": list(behavior.assumes),
            "ensures": list(behavior.ensures),
            "assigns": behavior.assigns,
        }
        for behavior in behaviors
    ]


def find_func_decl_line(header_text: str, func_name: str) -> int:
    """Find the line containing a function declaration name, or -1."""
    lines = header_text.splitlines()
    for index, line in enumerate(lines):
        pos = _find_identifier(line, func_name)
        if pos >= 0 and _next_nonspace(line, pos + len(func_name)) == "(":
            return index
    return -1


def associate_acsl_to_funcs(
    header_text: str,
    blocks: list[tuple[int, str]],
) -> dict[str, ACSLSpec]:
    """
    Compatibility wrapper for older callers that pass `(line, content)` blocks.

    New code uses `ScannerAcslParser`, which associates blocks by source offset.
    For this wrapper, line numbers are mapped back to approximate offsets.
    """
    line_offsets = _line_offsets(header_text)
    records = [
        _AcslBlock(
            start_line=line,
            end_offset=line_offsets[line] if 0 <= line < len(line_offsets) else 0,
            content=content,
        )
        for line, content in blocks
    ]
    return _associate_block_records_to_funcs(header_text, records)


def parse_acsl(header_path: str | Path) -> dict[str, ACSLSpec]:
    """Parse ACSL annotations from a C header file."""
    return ScannerAcslParser().parse_file(header_path)


def parse_acsl_from_text(header_text: str) -> dict[str, ACSLSpec]:
    """Parse ACSL from an in-memory header string."""
    return ScannerAcslParser().parse_text(header_text)


def _extract_acsl_block_records(text: str) -> list[_AcslBlock]:
    blocks: list[_AcslBlock] = []
    i = 0
    while i < len(text) - 2:
        if text[i:i + 3] != "/*@":
            i += 1
            continue

        start = i
        content_start = i + 3
        i = content_start
        while i < len(text) - 1 and text[i:i + 2] != "*/":
            i += 1
        if i >= len(text) - 1:
            break

        content = text[content_start:i]
        end_offset = i + 2
        blocks.append(_AcslBlock(text[:start].count("\n"), end_offset, content))
        i = end_offset
    return blocks


def _parse_block_behaviors(block: str) -> list[ACSLBehavior]:
    behaviors: list[ACSLBehavior] = []
    current: ACSLBehavior | None = None

    for statement in _split_acsl_statements(_clean_block_text(block)):
        if not statement:
            continue
        words = statement.split(None, 1)
        if not words:
            continue

        head = words[0]
        rest = words[1].strip() if len(words) > 1 else ""

        if head == "behavior" and rest.endswith(":"):
            name = rest[:-1].strip()
            if name:
                current = ACSLBehavior(name=name)
                behaviors.append(current)
            continue

        if head in {"complete", "disjoint"} and rest == "behaviors":
            continue

        if head == "requires":
            current = _ensure_current_behavior(behaviors, current)
            current.assumes.append(rest)
            continue

        if head in {"assumes", "ensures", "assigns"}:
            current = _ensure_current_behavior(behaviors, current)
            if head == "assumes":
                current.assumes.append(rest)
            elif head == "ensures":
                current.ensures.append(rest)
            else:
                current.assigns = rest
            continue

    return _propagate_global_requires(behaviors)


def _propagate_global_requires(behaviors: list[ACSLBehavior]) -> list[ACSLBehavior]:
    if len(behaviors) < 2:
        return behaviors
    global_behavior = behaviors[0]
    if (
        global_behavior.name != "valid"
        or not global_behavior.assumes
        or global_behavior.ensures
    ):
        return behaviors
    for behavior in behaviors[1:]:
        behavior.assumes = [*global_behavior.assumes, *behavior.assumes]
    return behaviors


def _ensure_current_behavior(
    behaviors: list[ACSLBehavior],
    current: ACSLBehavior | None,
) -> ACSLBehavior:
    if current is not None:
        return current
    current = ACSLBehavior(name="valid")
    behaviors.append(current)
    return current


def _clean_block_text(block: str) -> str:
    lines: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if line.startswith("@"):
            line = line[1:].strip()
        lines.append(line)
    return "\n".join(lines)


def _split_acsl_statements(text: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    depth = 0
    in_string: str | None = None

    for ch in text:
        if in_string:
            current.append(ch)
            if ch == in_string:
                in_string = None
            continue

        if ch in {'"', "'"}:
            in_string = ch
            current.append(ch)
            continue

        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1

        if ch == ":" and depth == 0:
            current.append(ch)
            item = " ".join("".join(current).split()).strip()
            if item.startswith("behavior ") and item.endswith(":"):
                statements.append(item)
                current = []
            continue

        if ch == ";" and depth == 0:
            item = " ".join("".join(current).split()).strip()
            if item:
                statements.append(item)
            current = []
            continue

        current.append(ch)

    tail = " ".join("".join(current).split()).strip()
    if tail:
        statements.append(tail)
    return statements


def _associate_block_records_to_funcs(
    header_text: str,
    blocks: list[_AcslBlock],
) -> dict[str, ACSLSpec]:
    result: dict[str, ACSLSpec] = {}
    for block in blocks:
        behaviors = _parse_block_behaviors(block.content)
        if not behaviors:
            continue

        func_name = _next_function_name_after(header_text, block.end_offset)
        if not func_name:
            continue

        spec = ACSLSpec(func_name=func_name, behaviors=behaviors)
        result[func_name] = spec
    return result


def _next_function_name_after(text: str, offset: int) -> str | None:
    i = offset
    while i < len(text):
        i = _skip_space_and_comments(text, i)
        decl_start = i
        while i < len(text) and text[i] not in ";{":
            i += 1
        if i >= len(text):
            return None
        if text[i] == "{":
            i += 1
            continue

        declaration = text[decl_start:i]
        name = _function_name_from_declaration(declaration)
        if name:
            return name
        i += 1
    return None


def _function_name_from_declaration(declaration: str) -> str | None:
    paren = declaration.find("(")
    if paren < 0:
        return None

    end = paren
    while end > 0 and declaration[end - 1].isspace():
        end -= 1
    if end == 0 or declaration[end - 1] == ")":
        return None

    start = end
    while start > 0 and _is_ident_char(declaration[start - 1]):
        start -= 1
    if start == end:
        return None

    name = declaration[start:end]
    if name in {"if", "for", "while", "switch", "return"}:
        return None
    if name.startswith("_") or name.upper() == name:
        return None
    return name


def _skip_space_and_comments(text: str, offset: int) -> int:
    i = offset
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            if end < 0:
                return len(text)
            i = end + 2
            continue
        if text[i:i + 2] == "//":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        break
    return i


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, ch in enumerate(text):
        if ch == "\n":
            offsets.append(index + 1)
    return offsets


def _find_identifier(text: str, name: str) -> int:
    start = 0
    while True:
        pos = text.find(name, start)
        if pos < 0:
            return -1
        before_ok = pos == 0 or not _is_ident_char(text[pos - 1])
        after = pos + len(name)
        after_ok = after >= len(text) or not _is_ident_char(text[after])
        if before_ok and after_ok:
            return pos
        start = pos + 1


def _next_nonspace(text: str, offset: int) -> str:
    i = offset
    while i < len(text) and text[i].isspace():
        i += 1
    return text[i] if i < len(text) else ""


def _is_ident_char(ch: str) -> bool:
    return ch == "_" or ch.isalnum()
