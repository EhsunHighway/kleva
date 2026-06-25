from __future__ import annotations

import os
import re
from pathlib import Path


def collect_visible_headers(
    header_path: Path,
    include_dirs: list[Path] | None = None,
    seen: set[Path] | None = None,
) -> list[str]:
    """Read a header and recursively read local quoted includes."""
    include_dirs = include_dirs or []
    seen = seen or set()

    try:
        resolved = header_path.resolve()
    except FileNotFoundError:
        return []

    if resolved in seen or not resolved.exists():
        return []
    seen.add(resolved)

    text = resolved.read_text()
    parts = [text]
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [resolved.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                parts.extend(collect_visible_headers(candidate, include_dirs, seen))
                break
    return parts


def collect_visible_header_paths(
    header_path: Path,
    include_dirs: list[Path] | None = None,
    seen: set[Path] | None = None,
) -> list[Path]:
    """Return a header and recursively discovered local quoted includes."""
    include_dirs = include_dirs or []
    seen = seen or set()

    try:
        resolved = header_path.resolve()
    except FileNotFoundError:
        return []

    if resolved in seen or not resolved.exists():
        return []
    seen.add(resolved)

    paths = [resolved]
    text = resolved.read_text()
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [resolved.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                paths.extend(collect_visible_header_paths(candidate, include_dirs, seen))
                break
    return paths


def collect_source_include_headers(
    source_path: str | Path,
    include_dirs: list[Path] | None = None,
) -> list[str]:
    """Read local quoted headers included directly by a source file."""
    source = Path(source_path)
    if not source.exists():
        return []

    include_dirs = include_dirs or []
    parts: list[str] = []
    text = source.read_text()
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [source.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if candidate.exists():
                parts.extend(collect_visible_headers(candidate, include_dirs))
                break
    return parts


def source_include_names(source_path: str | Path, include_dirs: list[Path] | None = None) -> list[str]:
    source = Path(source_path)
    if not source.exists():
        return []
    include_dirs = include_dirs or []
    text = source.read_text()
    names: list[str] = []
    for m in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', text, flags=re.MULTILINE):
        include_name = m.group(1)
        candidates = [source.parent / include_name]
        candidates.extend(include_dir / include_name for include_dir in include_dirs)
        for candidate in candidates:
            if not candidate.exists():
                continue
            for header_path in collect_visible_header_paths(candidate, include_dirs):
                name = header_path.name
                if name not in names:
                    names.append(name)
            break
    return names


def format_path_for_yaml(path: Path) -> str:
    return os.path.relpath(path.resolve(), Path.cwd().resolve())


def suggest_extra_sources(
    header_path: Path,
    include_dirs: list[Path],
    primary_source: str,
) -> list[str]:
    """Suggest .c files sitting next to recursively included project headers."""
    primary = Path(primary_source)
    try:
        primary_resolved = primary.resolve()
    except FileNotFoundError:
        primary_resolved = primary

    suggestions: list[str] = []
    seen: set[str] = set()
    for h in collect_visible_header_paths(header_path, include_dirs):
        c_path = h.with_suffix(".c")
        if not c_path.exists():
            continue
        try:
            if c_path.resolve() == primary_resolved:
                continue
        except FileNotFoundError:
            pass
        formatted = format_path_for_yaml(c_path)
        if formatted not in seen:
            seen.add(formatted)
            suggestions.append(formatted)
    return suggestions


def dedupe_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        try:
            key = str(path.resolve())
        except FileNotFoundError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(raw)
    return result
