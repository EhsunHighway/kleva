from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ModuleQuality:
    module:                str
    unit_file:             Path
    trusted_tests:         int
    trusted_assertions:    int
    eva_proven_assertions: int
    unproved_tests:        int
    unproved_assertions:   int
    unproved_items:        int
    skipped_candidates:    Optional[int] = None
    recipes:               Optional[int] = None
    runtime_seconds:       Optional[float] = None


@dataclass(frozen=True)
class ApiTestComparison:
    api:       str
    old_tests: int
    new_tests: int
    added:     tuple[str, ...]
    removed:   tuple[str, ...]


def collect_quality(root: str | Path) -> list[ModuleQuality]:
    base = Path(root)
    files = sorted(base.rglob("test_*_kleva.c")) if base.is_dir() else [base]
    modules: list[ModuleQuality] = []
    for unit_file in files:
        if unit_file.name.endswith("_unproved.c"):
            continue
        modules.append(_quality_for_unit(unit_file))
    return modules


def render_quality_report(modules: list[ModuleQuality]) -> str:
    lines = [
        "# KLEVA Generated Test Quality",
        "",
        "| Module | Trusted tests | Trusted assertions | EVA-proven assertions | Unproved diagnostics | Skipped candidates | Runtime |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for module in modules:
        lines.append(
            "| "
            + " | ".join([
                module.module,
                str(module.trusted_tests),
                str(module.trusted_assertions),
                str(module.eva_proven_assertions),
                str(module.unproved_items),
                _display_optional_int(module.skipped_candidates),
                _display_runtime(module.runtime_seconds),
            ])
            + " |"
        )
    totals = _totals(modules)
    lines.extend([
        "",
        "## Totals",
        "",
        f"- trusted tests: {totals.trusted_tests}",
        f"- trusted assertions: {totals.trusted_assertions}",
        f"- EVA-proven assertions: {totals.eva_proven_assertions}",
        f"- unproved diagnostics: {totals.unproved_items}",
    ])
    return "\n".join(lines) + "\n"


def compare_generated_tests_by_api(
    old_unit_file: str | Path,
    new_unit_file: str | Path,
    api_names:     list[str],
) -> list[ApiTestComparison]:
    old_names = _test_names(Path(old_unit_file).read_text(errors="replace"))
    new_names = _test_names(Path(new_unit_file).read_text(errors="replace"))
    comparisons: list[ApiTestComparison] = []
    for api in api_names:
        old_api = _tests_for_api(old_names, api)
        new_api = _tests_for_api(new_names, api)
        comparisons.append(ApiTestComparison(
            api=api,
            old_tests=len(old_api),
            new_tests=len(new_api),
            added=tuple(sorted(new_api - old_api)),
            removed=tuple(sorted(old_api - new_api)),
        ))
    return comparisons


def render_generated_test_comparison(comparisons: list[ApiTestComparison]) -> str:
    lines = [
        "# KLEVA Generated Test Comparison",
        "",
        "| API | Old tests | New tests | Added | Removed |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for item in comparisons:
        lines.append(
            "| "
            + " | ".join([
                item.api,
                str(item.old_tests),
                str(item.new_tests),
                _display_names(item.added),
                _display_names(item.removed),
            ])
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_quality_report(root: str | Path, out: str | Path) -> None:
    report = render_quality_report(collect_quality(root))
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)


def _quality_for_unit(unit_file: Path) -> ModuleQuality:
    text = unit_file.read_text(errors="replace")
    unproved_file = unit_file.with_name(f"{unit_file.stem}_unproved{unit_file.suffix}")
    unproved_report = unit_file.with_name(f"{unit_file.stem}_unproved_report.md")
    unproved_text = unproved_file.read_text(errors="replace") if unproved_file.exists() else ""
    report_text = unproved_report.read_text(errors="replace") if unproved_report.exists() else ""
    summary = _load_summary(unit_file.with_name(f"{unit_file.stem}_summary.json"))
    return ModuleQuality(
        module=_module_name(unit_file),
        unit_file=unit_file,
        trusted_tests=_count_tests(text),
        trusted_assertions=text.count("assert("),
        eva_proven_assertions=text.count("EVA-proven oracle"),
        unproved_tests=_count_tests(unproved_text),
        unproved_assertions=unproved_text.count("assert("),
        unproved_items=_count_unproved_items(report_text, unproved_text),
        skipped_candidates=_summary_int(summary, "skipped_candidates"),
        recipes=_summary_int(summary, "recipes"),
        runtime_seconds=_summary_float(summary, "duration_seconds"),
    )


def _count_tests(text: str) -> int:
    return len(re.findall(r"(?m)^static\s+void\s+test_", text))


def _test_names(text: str) -> set[str]:
    return set(re.findall(r"(?m)^static\s+void\s+(test_[A-Za-z_]\w*)\s*\(", text))


def _tests_for_api(test_names: set[str], api: str) -> set[str]:
    prefix = f"test_{api}_"
    exact = f"test_{api}"
    return {name for name in test_names if name == exact or name.startswith(prefix)}


def _count_unproved_items(report_text: str, unproved_text: str) -> int:
    if report_text:
        return len(re.findall(r"(?m)^- .*?: EVA_UNPROVED\b", report_text))
    return unproved_text.count("EVA_UNPROVED:")


def _module_name(path: Path) -> str:
    name = path.stem
    if name.startswith("test_"):
        name = name[len("test_"):]
    if name.endswith("_kleva"):
        name = name[:-len("_kleva")]
    return name


def _load_summary(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _summary_int(summary: dict, key: str) -> Optional[int]:
    value = summary.get(key)
    return value if isinstance(value, int) else None


def _summary_float(summary: dict, key: str) -> Optional[float]:
    value = summary.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _display_optional_int(value: Optional[int]) -> str:
    return str(value) if value is not None else "n/a"


def _display_runtime(value: Optional[float]) -> str:
    return f"{value:.3f}s" if value is not None else "n/a"


def _display_names(names: tuple[str, ...]) -> str:
    return ", ".join(names) if names else "none"


def _totals(modules: list[ModuleQuality]) -> ModuleQuality:
    return ModuleQuality(
        module="TOTAL",
        unit_file=Path(""),
        trusted_tests=sum(m.trusted_tests for m in modules),
        trusted_assertions=sum(m.trusted_assertions for m in modules),
        eva_proven_assertions=sum(m.eva_proven_assertions for m in modules),
        unproved_tests=sum(m.unproved_tests for m in modules),
        unproved_assertions=sum(m.unproved_assertions for m in modules),
        unproved_items=sum(m.unproved_items for m in modules),
    )
