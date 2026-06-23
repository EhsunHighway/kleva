"""
augment.py - implementation-derived YAML augmentation.

`kleva synth` is contract-driven. `kleva augment` is source-driven and
data-driven: users provide source patterns plus harness templates, and KLEVA
adds matching candidate cases that can be validated by the normal KLEE/EVA
pipeline.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AugmentedCase:
    name: str
    inferred_from: str
    body: list[str]
    outputs: list[str]
    cleanup: list[str]


@dataclass
class AugmentRule:
    name: str
    pattern: str
    body: list[str]
    outputs: list[str]
    cleanup: list[str]
    function_pattern: str | None = None


def _line_for_pattern(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    if not m:
        return 1
    return text.count("\n", 0, m.start()) + 1


def _rule_list_from_data(data: Any) -> list[dict[str, Any]]:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        raise ValueError("augment rules must be a YAML list or mapping")
    if isinstance(data.get("rules"), list):
        return data["rules"]
    augment = data.get("augment", {})
    if isinstance(augment, dict) and isinstance(augment.get("rules"), list):
        return augment["rules"]
    return []


def _load_rule_data(path: Path) -> list[dict[str, Any]]:
    return _rule_list_from_data(yaml.safe_load(path.read_text()))


def _coerce_lines(rule: dict[str, Any], key: str) -> list[str]:
    value = rule.get(key, [])
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError(f"augment rule {rule.get('name', '<unnamed>')}: {key} must be a string list")


def _compile_rule(raw: dict[str, Any]) -> AugmentRule:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("augment rule requires a non-empty name")

    when = raw.get("when", raw)
    if not isinstance(when, dict):
        raise ValueError(f"augment rule {name}: when must be a mapping")

    pattern = when.get("pattern") or when.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"augment rule {name}: when.pattern is required")

    function_pattern = when.get("function_pattern") or when.get("function_regex")
    if function_pattern is not None and not isinstance(function_pattern, str):
        raise ValueError(f"augment rule {name}: function_pattern must be a string")

    return AugmentRule(
        name             = name,
        pattern          = pattern,
        function_pattern = function_pattern,
        body             = _coerce_lines(raw, "body"),
        outputs          = _coerce_lines(raw, "outputs"),
        cleanup          = _coerce_lines(raw, "cleanup"),
    )


def _render_case(rule: AugmentRule, source_name: str, line: int) -> AugmentedCase:
    return AugmentedCase(
        name          = rule.name,
        inferred_from = f"{source_name}:{line}",
        body          = rule.body,
        outputs       = rule.outputs,
        cleanup       = rule.cleanup,
    )


def infer_cases(source_path: str | Path, raw_rules: list[dict[str, Any]]) -> list[AugmentedCase]:
    source      = Path(source_path)
    text        = source.read_text()
    source_name = str(source)
    cases: list[AugmentedCase] = []

    for raw in raw_rules:
        rule = _compile_rule(raw)
        if rule.function_pattern and not re.search(rule.function_pattern, text, re.DOTALL):
            continue
        if not re.search(rule.pattern, text, re.DOTALL):
            continue
        line = _line_for_pattern(text, rule.pattern)
        cases.append(_render_case(rule, source_name, line))

    return cases


def _case_to_yaml(case: AugmentedCase, output_base: str = "klee_build") -> dict[str, Any]:
    return {
        "name": case.name,
        "inferred_from": case.inferred_from,
        "ktest_dir": f"{output_base}/klee_out_{case.name}",
        "inputs": [],
        "body": case.body,
        "outputs": case.outputs,
        "cleanup": case.cleanup,
    }


def _resolve_existing_path(path_text: str | Path, base_dir: Path | None = None) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    if base_dir is not None:
        candidate = base_dir / path
        if candidate.exists():
            return candidate
    return Path.cwd() / path


def augment_yaml_text(
    config_text: str,
    source_path: str | Path | None = None,
    rules_path: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> str:
    data = yaml.safe_load(config_text)
    source = source_path or data.get("module", {}).get("source")
    if not source:
        raise ValueError("source path is required")

    base = Path(base_dir) if base_dir is not None else None
    source_file = _resolve_existing_path(source, base)

    raw_rules = _rule_list_from_data(data)
    if rules_path:
        rules_file = _resolve_existing_path(rules_path, base)
        raw_rules.extend(_load_rule_data(rules_file))

    cases = infer_cases(source_file, raw_rules)
    output_base = data.get("klee", {}).get("output_base", "klee_build")

    existing = data.get("functions", [])
    generated_names = {c.name for c in cases}
    data["functions"] = [
        fn for fn in existing
        if fn.get("name") not in generated_names
    ] + [_case_to_yaml(c, output_base) for c in cases]

    return yaml.safe_dump(data, sort_keys=False, width=1000)


def augment_yaml(
    config_path: str | Path,
    source_path: str | Path | None = None,
    output_path: str | Path | None = None,
    rules_path: str | Path | None = None,
) -> str:
    path = Path(config_path)
    text = augment_yaml_text(
        path.read_text(),
        source_path = source_path,
        rules_path  = rules_path,
        base_dir    = path.parent,
    )
    out = Path(output_path) if output_path else path
    out.write_text(text)
    return text


def run_augment(
    config: str,
    source: str | None = None,
    out: str | None = None,
    rules: str | None = None,
) -> None:
    try:
        text = augment_yaml(config, source, out, rules)
    except Exception as exc:
        print(f"kleva augment: {exc}", file=sys.stderr)
        sys.exit(1)

    cases = len([fn for fn in yaml.safe_load(text).get("functions", []) if "inferred_from" in fn])
    out_path = out or config
    print(f"kleva augment: wrote {out_path} with {cases} inferred case(s)", file=sys.stderr)
