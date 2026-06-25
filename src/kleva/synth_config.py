from __future__ import annotations

# Default symbolic bounds for scalar types
SCALAR_BOUNDS: dict[str, tuple[int, int]] = {
    "uint8_t":  (0, 255),
    "uint16_t": (0, 65535),
    "uint32_t": (0, 4294967295),
    "uint64_t": (0, 1000000),
    "int":      (0, 2147483647),
    "size_t":   (1, 268435455),
}

SHAPING_FEATURES = {
    "function-pointers",
    "quantified-arrays",
    "casted-fields",
    "byte-order",
    "loop-tables",
    "state-switches",
    "callee-success",
    "fallback-lookups",
}
DEFAULT_SHAPING_FEATURES = frozenset(SHAPING_FEATURES)


def normalize_shaping_features(
    shaping: list[str] | None = None,
    no_shaping: list[str] | None = None,
) -> set[str]:
    """Resolve CLI shaping flags into the enabled feature set."""
    enabled = set(DEFAULT_SHAPING_FEATURES)
    if shaping:
        enabled = set()
        for raw in shaping:
            for item in raw.split(","):
                item = item.strip()
                if not item:
                    continue
                if item == "all":
                    enabled.update(SHAPING_FEATURES)
                elif item == "none":
                    enabled.clear()
                else:
                    enabled.add(item)

    for raw in no_shaping or []:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if item == "all":
                enabled.clear()
            elif item != "none":
                enabled.discard(item)

    unknown = enabled.difference(SHAPING_FEATURES)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unknown shaping feature(s): {names}")
    return enabled
