from __future__ import annotations

import re
from pathlib import Path

from .acsl import ACSLBehavior
from .ast.model import CFunction
from .fixtures.construction import safe_c_name


def emit_str_list(lines: list[str], indent_n: int = 6) -> str:
    pad = " " * indent_n
    if not lines:
        return "[]"
    result = "\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace('"', '\\"')
        result += f'{pad}- "{escaped}"\n'
    return result.rstrip("\n")


def emit_output_list(outputs: list[str], indent_n: int = 6) -> str:
    pad = " " * indent_n
    if not outputs:
        return "[]"
    return "[" + ", ".join(outputs) + "]"


def emit_yaml_function(
    func: CFunction,
    behavior: ACSLBehavior,
    body: list[str],
    outputs: list[str],
    cleanup: list[str],
    ktest_dir: str,
    preamble: list[str] | None = None,
    source_include_names: list[str] | None = None,
    candidate: bool = False,
) -> list[str]:
    """Emit YAML lines for one function test entry."""
    preamble = preamble or []
    source_include_names = source_include_names or []
    body_text = "\n".join(body)
    for include_name in source_include_names:
        stem = Path(include_name).stem
        type_token = safe_c_name(stem).title().replace("_", "")
        if re.search(rf"\b{re.escape(stem)}_", body_text) or re.search(rf"\b{re.escape(type_token)}\b", body_text):
            include_line = f'#include "{include_name}"'
            if include_line not in preamble:
                preamble = [include_line, *preamble]
    lines: list[str] = [
        "",
        f"  # {func.name} — behavior: {behavior.name}",
        f"  - name:      {ktest_dir.replace('klee_build/klee_out_', '')}",
        f"    ktest_dir: {ktest_dir}",
        "    inputs:    []",
    ]
    if preamble:
        lines.append(f"    preamble:  {emit_str_list(preamble)}")
    lines.extend([
        f"    body:      {emit_str_list(body)}",
        f"    outputs:   {emit_output_list(outputs)}",
    ])
    if cleanup:
        lines.append(f"    cleanup:   {emit_str_list(cleanup)}")
    else:
        lines.append("    cleanup:   []")
    if candidate:
        lines.append("    candidate: true")
    return lines
