from __future__ import annotations

import re


def host_to_network_fn(decode_fn: str) -> str:
    """Return the inverse byte-order conversion function when it follows ntoh/hton naming."""
    if "ntoh" not in decode_fn:
        return ""
    return decode_fn.replace("ntoh", "hton", 1)


def propagate_local_aliases(body: str, aliases: dict) -> dict:
    changed = True
    while changed:
        changed = False
        for m in re.finditer(
            r"\b(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)\s+(\w+)\s*=\s*(\w+)\s*;",
            body,
        ):
            dst, src = m.groups()
            if src in aliases and dst not in aliases:
                aliases[dst] = aliases[src]
                changed = True
        for m in re.finditer(r"\b(\w+)\s*=\s*(\w+)\s*;", body):
            dst, src = m.groups()
            if src in aliases and dst not in aliases:
                aliases[dst] = aliases[src]
                changed = True
    return aliases


def decoded_field_aliases(body: str) -> dict[str, tuple[str, str, str]]:
    decoded: dict[str, tuple[str, str, str]] = {}
    for m in re.finditer(
        r"\b(?:uint(?:8|16|32|64)_t|int(?:8|16|32|64)_t|size_t|int)\s+(\w+)\s*=\s*([A-Za-z_]\w*ntoh[sl])\s*\(\s*(\w+)->(\w+)\s*\)\s*;",
        body,
    ):
        local, fn, alias, field = m.groups()
        decoded[local] = (fn, alias, field)
    return propagate_local_aliases(body, decoded)

