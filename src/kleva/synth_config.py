from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .shaping.ir_parsers import HelperCallRule

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
    "branch-conditions",
    "function-pointers",
    "parser-headers",
    "quantified-arrays",
    "casted-fields",
    "byte-order",
    "loop-tables",
    "state-switches",
    "callee-success",
    "fallback-lookups",
    "regex-fallbacks",
}
DEFAULT_SHAPING_FEATURES = frozenset(SHAPING_FEATURES - {"regex-fallbacks"})


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


def load_helper_call_rules(paths: list[str] | None) -> tuple[HelperCallRule, ...]:
    rules: list[HelperCallRule] = []
    for raw_path in paths or []:
        path = Path(raw_path)
        data = yaml.safe_load(path.read_text())
        for raw_rule in _helper_rule_items(data):
            rules.append(_compile_helper_rule(raw_rule))
    return tuple(rules)


def _helper_rule_items(data: Any) -> list[dict[str, Any]]:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("helper rules must be a YAML list or mapping")
    if isinstance(data.get("helper_call_rules"), list):
        return data["helper_call_rules"]
    synth = data.get("synth", {})
    if isinstance(synth, dict) and isinstance(synth.get("helper_call_rules"), list):
        return synth["helper_call_rules"]
    return []


def _compile_helper_rule(raw: dict[str, Any]) -> HelperCallRule:
    if not isinstance(raw, dict):
        raise ValueError("helper rule must be a mapping")
    callee = raw.get("callee")
    if not isinstance(callee, str) or not callee:
        raise ValueError("helper rule requires a non-empty callee")
    return HelperCallRule(
        callee,
        success_setup=tuple(_coerce_rule_lines(raw, "success_setup")),
        failure_setup=tuple(_coerce_rule_lines(raw, "failure_setup")),
    )


def _coerce_rule_lines(rule: dict[str, Any], key: str) -> list[str]:
    value = rule.get(key, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    callee = rule.get("callee", "<unnamed>")
    raise ValueError(f"helper rule {callee}: {key} must be a string list")
