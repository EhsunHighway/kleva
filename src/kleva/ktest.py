"""
ktest.py — Parse KLEE .ktest files via ktest-tool.

ktest-tool prints something like:
    object 0: name: 'capacity'
    object 0: size: 8
    object 0: hex : 0x4000000000000000
    object 1: name: 'header'
    object 1: size: 3
    object 1: hex : 0xdeadbe

We parse that text output into KTestObject instances.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class KTestObject:
    name: str
    size: int       # declared size from ktest-tool
    data: bytes     # raw bytes (memory order)

    @property
    def uint(self) -> int:
        """Reconstruct the scalar value as a little-endian unsigned integer."""
        return int.from_bytes(self.data, byteorder="little")

    @property
    def hex_bytes(self) -> list[int]:
        return list(self.data)


def parse_ktest(ktest_tool: str, ktest_path: str) -> list[KTestObject]:
    """
    Run ktest-tool on one .ktest file and return the parsed symbolic objects.

    Raises subprocess.CalledProcessError if ktest-tool fails.
    """
    out = subprocess.check_output([ktest_tool, ktest_path], text=True)

    objects: list[KTestObject] = []
    cur: dict = {}

    for raw_line in out.splitlines():
        line = raw_line.strip()

        m = re.match(r"object \d+: name: '(.+)'", line)
        if m:
            if cur:
                objects.append(_finalise(cur))
            cur = {"name": m.group(1)}
            continue

        m = re.match(r"object \d+: size: (\d+)", line)
        if m and cur:
            cur["size"] = int(m.group(1))
            continue

        m = re.match(r"object \d+: hex\s*:\s*0x([0-9a-fA-F]+)", line)
        if m and cur:
            h = m.group(1)
            if len(h) % 2:
                h = "0" + h
            cur["raw"] = bytes.fromhex(h)

    if cur:
        objects.append(_finalise(cur))

    return objects


def _finalise(d: dict) -> KTestObject:
    raw  = d.get("raw", b"")
    size = d.get("size", len(raw))
    # If ktest-tool truncated the hex output, pad with zeros
    if len(raw) < size:
        raw = raw + b"\x00" * (size - len(raw))
    return KTestObject(name=d["name"], size=size, data=raw[:size])


def find_obj(objects: list[KTestObject], name: str) -> KTestObject | None:
    """Return the first KTestObject with the given name, or None."""
    return next((o for o in objects if o.name == name), None)
