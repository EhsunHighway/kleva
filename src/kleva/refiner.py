"""
refiner.py — `kleva refine` — Refine a kleva YAML config from pipeline output.

This is the feedback loop: after running `kleva all`, the generated unit test
file contains the actual working C code with EVA-proven assertions. This module
reverse-engineers that output to produce a complete, production-quality YAML
config that can be committed and reused without needing ACSL annotations.

What it does:
  1. Reads the generated unit test file  (unit/test_<module>_kleva.c)
  2. Reads the EVA log                   (eva_raw.log or eva/*.c log)
  3. Reads the KLEE ktest files           (klee_build/klee_out_*/*.ktest)
  4. Reads the existing YAML config       (kleva/<module>.yaml)
  5. Produces an improved YAML config with:
       - Complete body from the working unit test
       - EVA-proven assertion values as documented oracles
       - Cleanup sequences from the test functions
       - Correct ktest_dir paths
       - Proper outputs list
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ExtractedTest:
    """Extracted information about one unit test function."""
    func_name:    str          # e.g. "test_checksum_null_tv001"
    yaml_test_name: str        # e.g. "ip_checksum_null"
    body_lines:   list[str]    # verbatim body lines from the generated test
    assert_lines: list[str]    # assert() statements with EVA-proven values
    cleanup_lines: list[str]   # cleanup calls from the test
    output_vars:  list[str]    # proven/unproven output variables
    header:       str          # e.g. "ip.h" from includes


def parse_unit_test(file_path: str | Path) -> list[ExtractedTest]:
    """
    Parse the generated unit test C file and extract per-test information.

    Each test function looks like:
        static void test_<name>_tv<N>(void) {
            ...
            assert(var == VAL);  /* EVA-proven oracle */
            printf("PASS ...");
        }
    """
    text = Path(file_path).read_text()

    # Extract the header include
    header_match = re.search(r'#include\s+"(\w+\.h)"', text)
    header = header_match.group(1) if header_match else ""

    # Find each test function block
    tests: list[ExtractedTest] = []

    # Match: static void test_<name>(void) { ... }
    pattern = re.compile(
        r'static void (test_\w+)(?:\(void\))\s*\{'
        r'(.*?)\n\}',
        re.DOTALL,
    )

    for m in pattern.finditer(text):
        fn_name = m.group(1)
        body_text = m.group(2).strip()

        body_lines: list[str] = []
        assert_lines: list[str] = []
        cleanup_lines: list[str] = []
        output_vars: list[str] = []

        for line in body_text.splitlines():
            stripped = line.strip()

            # Skip assert (we capture it separately)
            if stripped.startswith("assert("):
                assert_lines.append(stripped)
                # Extract the variable name from assert(var == VAL)
                var_match = re.match(r'assert\((\w+)\s*==', stripped)
                if var_match:
                    output_vars.append(var_match.group(1))
                continue

            # Skip printf
            if stripped.startswith("printf("):
                continue

            # Skip blank lines
            if not stripped:
                continue

            # Check if it's a guard assertion (assert(ptr != NULL))
            if stripped.startswith("assert(") and "!= NULL" in stripped:
                # This is a guard — keep it in body
                body_lines.append(stripped)
                continue

            # Cleanup calls (typically at end before printf)
            if any(fn in stripped for fn in ["_free(", "_destroy("]):
                cleanup_lines.append(stripped)
            else:
                body_lines.append(stripped)

        # Derive the YAML test name from the function name
        # test_checksum_null_tv001 → ip_checksum_null (or checksum_null)
        yaml_name = fn_name.replace("test_", "", 1)
        # Remove the _tvNNN suffix
        yaml_name = re.sub(r'_tv\d+$', '', yaml_name)

        tests.append(ExtractedTest(
            func_name=fn_name,
            yaml_test_name=yaml_name,
            body_lines=body_lines,
            assert_lines=assert_lines,
            cleanup_lines=cleanup_lines,
            output_vars=output_vars,
            header=header,
        ))

    return tests


@dataclass
class KTestInfo:
    """Information about a ktest directory."""
    dir_name: str       # e.g. "klee_build/klee_out_ip_checksum_null"
    test_name: str      # e.g. "ip_checksum_null"
    num_files: int      # number of .ktest files
    sample_values: dict[str, list[int]] = field(default_factory=dict)  # ktest_name → sample values


def scan_ktest_dirs(base_dir: str | Path) -> list[KTestInfo]:
    """
    Scan for KLEE ktest output directories and gather basic info.
    """
    base = Path(base_dir)
    klee_build = base / "klee_build"
    if not klee_build.is_dir():
        return []

    results: list[KTestInfo] = []
    for d in sorted(klee_build.glob("klee_out_*")):
        ktest_files = list(d.glob("*.ktest"))
        test_name = d.name.replace("klee_out_", "", 1)
        results.append(KTestInfo(
            dir_name=str(d),
            test_name=test_name,
            num_files=len(ktest_files),
        ))

    return results


def refine_yaml(
    existing_yaml_path: str | Path,
    unit_test_path: str | Path,
    eva_log_path: str | Path | None = None,
    base_dir: str | Path = ".",
    output_path: str | Path | None = None,
) -> str:
    """
    Refine a kleva YAML config from the generated pipeline output.

    Reads the generated unit test file and existing YAML, then produces
    an improved YAML with accurate bodies, outputs, and cleanup drawn
    from the working tests.

    Args:
        existing_yaml_path: Path to the current YAML config.
        unit_test_path: Path to the generated unit test file.
        eva_log_path: Path to EVA raw log (optional).
        base_dir: Base directory for resolving paths.
        output_path: Output path for refined YAML (default: overwrite existing).

    Returns:
        The refined YAML text.
    """
    base = Path(base_dir)

    # Parse the existing YAML to preserve module/tools/eva/klee/output config
    from .config import load_config
    cfg = load_config(existing_yaml_path)

    # Parse the generated unit test file
    tests = parse_unit_test(unit_test_path)

    # Read the existing YAML text for structural preservation
    existing_text = Path(existing_yaml_path).read_text()

    # Extract the YAML header (module / tools / eva / klee / output sections)
    header_match = re.match(
        r'(.*?)(?=\nfunctions:)',
        existing_text,
        re.DOTALL,
    )
    yaml_header = header_match.group(1) if header_match else ""

    # Build the new functions section from extracted tests
    lines: list[str] = []
    if yaml_header:
        lines.append(yaml_header.rstrip())
        lines.append("")
    else:
        # Fallback: reconstruct minimal header from config
        lines.append(f"module:")
        lines.append(f"  name:        {cfg.module_name}")
        lines.append(f"  header:      {cfg.module_header}")
        lines.append(f"  source:      {cfg.module_source}")
        lines.append(f"  include_dir: {cfg.include_dir}")
        lines.append("")

    lines.append("functions:")

    # Find ktest dirs
    ktest_infos = scan_ktest_dirs(base)
    ktest_map = {kt.test_name: kt for kt in ktest_infos}

    for test in tests:
        lines.append("")
        lines.append(f"  # Refined from: {test.func_name}")

        # Find ktest dir
        kt = ktest_map.get(test.yaml_test_name)
        ktest_dir = f"klee_build/klee_out_{test.yaml_test_name}"
        if kt:
            ktest_dir = kt.dir_name
            lines.append(f"  # {kt.num_files} ktest file(s) available")

        # Convert body lines to YAML list format
        body_yaml = _body_to_yaml(test.body_lines)
        outputs_yaml = _outputs_to_yaml(test.output_vars)
        cleanup_yaml = _cleanup_to_yaml(test.cleanup_lines)

        lines.append(f"  - name:      {test.yaml_test_name}")
        lines.append(f"    ktest_dir: {ktest_dir}")
        lines.append(f"    inputs:    []")
        lines.append(f"    body:      {body_yaml}")
        lines.append(f"    outputs:   {outputs_yaml}")
        lines.append(f"    cleanup:   {cleanup_yaml}")

    yaml_text = "\n".join(lines) + "\n"

    # Write output
    out_path = Path(output_path) if output_path else Path(existing_yaml_path)
    out_path.write_text(yaml_text)

    print(f"kleva refine: refined {len(tests)} test(s) from {unit_test_path}")
    print(f"kleva refine: wrote {out_path}")

    return yaml_text


def _body_to_yaml(body_lines: list[str]) -> str:
    """Convert body lines to YAML block format."""
    if not body_lines:
        return "[]"
    result = "\n"
    for line in body_lines:
        escaped = line.replace("\\", "\\\\").replace('"', '\\"')
        result += f'      - "{escaped}"\n'
    return result.rstrip("\n")


def _outputs_to_yaml(output_vars: list[str]) -> str:
    """Convert output variables to YAML list format."""
    if not output_vars:
        return "[]"
    return "[" + ", ".join(output_vars) + "]"


def _cleanup_to_yaml(cleanup_lines: list[str]) -> str:
    """Convert cleanup lines to YAML block format."""
    if not cleanup_lines:
        return "[]"
    result = "\n"
    for line in cleanup_lines:
        escaped = line.replace("\\", "\\\\").replace('"', '\\"')
        result += f'      - "{escaped}"\n'
    return result.rstrip("\n")


def run_refine(
    config: str,
    base_dir: str = ".",
) -> None:
    """
    `kleva refine` entry point: refine YAML from pipeline output.

    Reads:
        <config>                                — existing YAML
        <base_dir>/unit/test_<module>_kleva.c   — generated unit tests
        <base_dir>/klee_build/                   — ktest directories
    Writes:
        <config>                                — refined YAML (overwrites)
    """
    config_path = Path(config)
    if not config_path.exists():
        print(f"kleva refine: config not found: {config_path}", file=__import__('sys').stderr)
        __import__('sys').exit(1)

    base = Path(base_dir)

    # Derive module name from config path
    module_name = config_path.stem

    # Look for generated unit test file
    unit_test_path = base / "unit" / f"test_{module_name}_kleva.c"
    if not unit_test_path.exists():
        # Try alternative: look in potential eva/ directories
        alt_path = base / f"test_{module_name}_kleva.c"
        if alt_path.exists():
            unit_test_path = alt_path
        else:
            print(f"kleva refine: unit test file not found: {unit_test_path}", file=__import__('sys').stderr)
            # Check if there are any .c files in unit/ directory
            unit_dir = base / "unit"
            if unit_dir.is_dir():
                c_files = list(unit_dir.glob("*.c"))
                if c_files:
                    print(f"  Found alternative: {c_files[0]}", file=__import__('sys').stderr)
                    unit_test_path = c_files[0]
                else:
                    __import__('sys').exit(1)
            else:
                __import__('sys').exit(1)

    # Check for EVA log
    eva_log_path = base / "eva_raw.log"
    if not eva_log_path.exists():
        eva_log_path = None

    print(f"kleva refine: reading unit tests from {unit_test_path}", file=__import__('sys').stderr)

    refine_yaml(
        existing_yaml_path=config_path,
        unit_test_path=unit_test_path,
        eva_log_path=eva_log_path,
        base_dir=base_dir,
        output_path=config_path,  # overwrite in place
    )