from __future__ import annotations


def safe_name(value: str, fallback: str = "value") -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value).strip("_") or fallback
