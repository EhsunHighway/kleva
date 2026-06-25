from __future__ import annotations

import re


def extract_null_params(assumes_exprs: list[str]) -> list[str]:
    """Extract simple ACSL assumptions like `iface == \\null`."""
    null_params: list[str] = []
    for expr in assumes_exprs:
        parts = re.split(r"\|\||&&", expr)
        for part in parts:
            m = re.search(r"(\w+)\s*==\s*\\(?:null|NULL)\b", part.strip())
            if m:
                null_params.append(m.group(1))
            m = re.search(r"\\(?:null|NULL)\b\s*==\s*(\w+)", part.strip())
            if m:
                null_params.append(m.group(1))
    return null_params


def extract_valid_params(assumes_exprs: list[str]) -> list[str]:
    """Extract parameter names from ACSL `\\valid(...)` and `\\valid_read(...)`."""
    valid_params: list[str] = []
    for expr in assumes_exprs:
        for m in re.finditer(r"\\(?:valid|valid_read)\((\w+)", expr):
            valid_params.append(m.group(1))
    return valid_params


def extract_non_null_params(assumes_exprs: list[str]) -> list[str]:
    """Extract simple ACSL assumptions like `ctx != \\null`."""
    params: list[str] = []
    for expr in assumes_exprs:
        parts = re.split(r"\|\||&&", expr)
        for part in parts:
            part = part.strip()
            m = re.search(r"(\w+)\s*!=\s*\\(?:null|NULL)\b", part)
            if m:
                params.append(m.group(1))
            m = re.search(r"\\(?:null|NULL)\b\s*!=\s*(\w+)", part)
            if m:
                params.append(m.group(1))
    return params


def extract_nonzero_params(assumes_exprs: list[str]) -> list[str]:
    """Extract simple ACSL assumptions like `port != 0` or `bw > 0`."""
    params: list[str] = []
    for expr in assumes_exprs:
        for part in re.split(r"\|\||&&", expr):
            part = part.strip()
            m = re.search(r"(\w+)\s*!=\s*0\b", part)
            if m:
                params.append(m.group(1))
            m = re.search(r"\b0\s*!=\s*(\w+)", part)
            if m:
                params.append(m.group(1))
            m = re.search(r"(\w+)\s*>\s*0\b", part)
            if m:
                params.append(m.group(1))
            m = re.search(r"\b0\s*<\s*(\w+)", part)
            if m:
                params.append(m.group(1))
    return params


def scalar_values_from_assumptions(assumes_exprs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for expr in assumes_exprs:
        for part in re.split(r"\|\||&&", expr):
            part = part.strip()
            m = re.fullmatch(r"(\w+)\s*==\s*(0x[0-9a-fA-F]+|\d+)", part)
            if m:
                values[m.group(1)] = m.group(2)
                continue
            m = re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)\s*==\s*(\w+)", part)
            if m:
                values[m.group(2)] = m.group(1)
                continue
            m = re.fullmatch(r"(\w+)\s*>\s*0\b", part)
            if m:
                values[m.group(1)] = "1"
                continue
            m = re.fullmatch(r"\b0\s*<\s*(\w+)", part)
            if m:
                values[m.group(1)] = "1"
    return values


def extract_result_value(ensures_exprs: list[str]) -> int | None:
    """
    Extract a singleton integer from ACSL `\\result == N` ensures clauses.

    Returns None when there is no result clause or multiple result values.
    """
    values: set[int] = set()
    for expr in ensures_exprs:
        simple = expr.strip()
        while simple.startswith("(") and simple.endswith(")"):
            inner = simple[1:-1].strip()
            if not inner:
                break
            simple = inner

        m = re.fullmatch(r"\\result\s*==\s*(0x[0-9a-fA-F]+|-?\d+)", simple)
        if m:
            raw = m.group(1)
            values.add(int(raw, 16) if raw.lower().startswith("0x") else int(raw))
            continue

        m = re.fullmatch(r"(0x[0-9a-fA-F]+|-?\d+)\s*==\s*\\result", simple)
        if m:
            raw = m.group(1)
            values.add(int(raw, 16) if raw.lower().startswith("0x") else int(raw))
    if len(values) == 1:
        return next(iter(values))
    return None
