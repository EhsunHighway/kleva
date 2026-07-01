from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..ast.model import CParam


class FixtureRequirementKind(str, Enum):
    STRING_BUFFER = "string-buffer"
    BYTE_BUFFER = "byte-buffer"
    OBJECT_PATH_BYTE_BUFFER = "object-path-byte-buffer"
    OBJECT_PATH_VALUE = "object-path-value"
    ROOT_VALID = "root-valid"
    ROOT_NULL = "root-null"


@dataclass(frozen=True)
class FixtureRequirement:
    kind:   FixtureRequirementKind
    target: str
    size:   str | None = None
    access: str | None = None
    relation: str | None = None
    value: str | None = None
    content: str | None = None


def string_buffer(target: str) -> FixtureRequirement:
    return FixtureRequirement(FixtureRequirementKind.STRING_BUFFER, target)


def byte_buffer(
    target:  str,
    size:    str = "64",
    access:  str = "readwrite",
    content: str | None = None,
) -> FixtureRequirement:
    return FixtureRequirement(FixtureRequirementKind.BYTE_BUFFER, target, size, access, content=content)


def object_path_byte_buffer(
    target:  str,
    size:    str = "64",
    access:  str = "readwrite",
    content: str | None = None,
) -> FixtureRequirement:
    return FixtureRequirement(FixtureRequirementKind.OBJECT_PATH_BYTE_BUFFER, target, size, access, content=content)


def object_path_value(target: str, relation: str, value: str) -> FixtureRequirement:
    return FixtureRequirement(
        FixtureRequirementKind.OBJECT_PATH_VALUE,
        target,
        relation=relation,
        value=value,
    )


def valid_root(target: str) -> FixtureRequirement:
    return FixtureRequirement(FixtureRequirementKind.ROOT_VALID, target)


def null_root(target: str) -> FixtureRequirement:
    return FixtureRequirement(FixtureRequirementKind.ROOT_NULL, target)


def requirements_for_valid_param(param: CParam) -> list[FixtureRequirement]:
    requirements: list[FixtureRequirement] = []
    if param.is_pointer and param.base_type == "char":
        requirements.append(string_buffer(param.name))
    if param.is_pointer and param.base_type == "uint8_t":
        requirements.append(byte_buffer(param.name))
    return requirements


def requirements_from_assumptions(assumes: list[str]) -> list[FixtureRequirement]:
    requirements: list[FixtureRequirement] = []
    for expr in assumes:
        requirements.extend(_valid_read_requirements(expr))
        requirements.extend(_valid_range_requirements(expr))
        requirements.extend(_valid_read_object_path_requirements(expr))
        requirements.extend(_valid_object_path_range_requirements(expr))
        requirements.extend(_root_validity_requirements(expr))
        requirements.extend(_object_path_value_requirements(expr))
        requirements.extend(_valid_requirements(expr))
    return requirements


def requirements_for_valid_params(
    params: list[CParam],
    valid_param_names: set[str],
) -> list[FixtureRequirement]:
    requirements: list[FixtureRequirement] = []
    for param in params:
        if param.name in valid_param_names:
            requirements.extend(requirements_for_valid_param(param))
    return requirements


def requirements_for_target(
    requirements: list[FixtureRequirement] | None,
    target: str,
) -> list[FixtureRequirement]:
    return [req for req in requirements or [] if req.target == target]


def has_requirement(
    requirements: list[FixtureRequirement] | None,
    target: str,
    kind: FixtureRequirementKind,
) -> bool:
    return any(req.kind == kind for req in requirements_for_target(requirements, target))


def first_requirement(
    requirements: list[FixtureRequirement] | None,
    target: str,
    kind: FixtureRequirementKind,
) -> FixtureRequirement | None:
    for req in requirements_for_target(requirements, target):
        if req.kind == kind:
            return req
    return None


def fixture_failure_comments(requirements: list[FixtureRequirement] | None) -> list[str]:
    failures: list[str] = []
    requirements = requirements or []

    valid_roots = {req.target for req in requirements if req.kind == FixtureRequirementKind.ROOT_VALID}
    null_roots = {req.target for req in requirements if req.kind == FixtureRequirementKind.ROOT_NULL}
    for root in sorted(valid_roots & null_roots):
        failures.append(f"/* fixture-failed: conflicting constraints: {root} is both null and valid */")

    exact_values: dict[str, str] = {}
    for req in requirements:
        if req.kind != FixtureRequirementKind.OBJECT_PATH_VALUE or req.relation != "==" or req.value is None:
            continue
        previous = exact_values.get(req.target)
        if previous is not None and previous != req.value:
            failures.append(
                f"/* fixture-failed: conflicting constraints: {req.target} == {previous} and {req.target} == {req.value} */"
            )
            continue
        exact_values[req.target] = req.value

    return _dedupe(failures)


def usable_requirements(requirements: list[FixtureRequirement] | None) -> list[FixtureRequirement]:
    requirements = requirements or []
    conflicting_targets: set[str] = set()
    exact_values: dict[str, str] = {}
    for req in requirements:
        if req.kind != FixtureRequirementKind.OBJECT_PATH_VALUE or req.relation != "==" or req.value is None:
            continue
        previous = exact_values.get(req.target)
        if previous is not None and previous != req.value:
            conflicting_targets.add(req.target)
        else:
            exact_values[req.target] = req.value

    valid_roots = {req.target for req in requirements if req.kind == FixtureRequirementKind.ROOT_VALID}
    null_roots = {req.target for req in requirements if req.kind == FixtureRequirementKind.ROOT_NULL}
    conflicting_roots = valid_roots & null_roots

    return [
        req for req in requirements
        if req.target not in conflicting_targets and req.target not in conflicting_roots
    ]


def _valid_read_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    pattern = re.compile(
        r"\\valid_read\(\s*(?:\([^)]*\)\s*)*"
        r"([A-Za-z_]\w*)\s*\+\s*\(\s*0\s*\.\.\s*([^)]+?)\s*\)\s*\)"
    )
    for match in pattern.finditer(expr):
        target, upper = match.groups()
        size = _size_from_upper_bound(upper.strip())
        requirements.append(byte_buffer(target, size, "read"))
    return requirements


def _valid_range_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    pattern = re.compile(
        r"\\valid\(\s*(?:\([^)]*\)\s*)*"
        r"([A-Za-z_]\w*)\s*\+\s*\(\s*0\s*\.\.\s*([^)]+?)\s*\)\s*\)"
    )
    for match in pattern.finditer(expr):
        target, upper = match.groups()
        size = _size_from_upper_bound(upper.strip())
        requirements.append(byte_buffer(target, size, "write"))
    return requirements


def _valid_read_object_path_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    pattern = re.compile(
        r"\\valid_read\(\s*"
        r"([A-Za-z_]\w*->[A-Za-z_]\w*)\s*\+\s*"
        r"\(\s*0\s*\.\.\s*([^)]+?)\s*\)\s*\)"
    )
    for match in pattern.finditer(expr):
        target, upper = match.groups()
        size = _size_from_upper_bound(upper.strip())
        requirements.append(object_path_byte_buffer(target, size, "read"))
    return requirements


def _valid_object_path_range_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    pattern = re.compile(
        r"\\valid\(\s*"
        r"([A-Za-z_]\w*->[A-Za-z_]\w*)\s*\+\s*"
        r"\(\s*0\s*\.\.\s*([^)]+?)\s*\)\s*\)"
    )
    for match in pattern.finditer(expr):
        target, upper = match.groups()
        size = _size_from_upper_bound(upper.strip())
        requirements.append(object_path_byte_buffer(target, size, "write"))
    return requirements


def _valid_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    pattern = re.compile(r"\\valid\(\s*(?:\([^)]*\)\s*)*([A-Za-z_]\w*)\s*\)")
    for match in pattern.finditer(expr):
        target = match.group(1)
        requirements.append(byte_buffer(target))
    return requirements


def _root_validity_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    for part in re.split(r"\s*&&\s*", expr):
        part = part.strip()
        valid = re.fullmatch(r"\\valid(?:_read)?\(\s*([A-Za-z_]\w*)\s*\)", part)
        if valid:
            requirements.append(valid_root(valid.group(1)))
            continue

        null_match = re.fullmatch(r"([A-Za-z_]\w*)\s*==\s*(?:\\null|NULL|0)", part)
        if null_match:
            requirements.append(null_root(null_match.group(1)))
            continue

        null_match = re.fullmatch(r"(?:\\null|NULL|0)\s*==\s*([A-Za-z_]\w*)", part)
        if null_match:
            requirements.append(null_root(null_match.group(1)))
    return requirements


def _object_path_value_requirements(expr: str) -> list[FixtureRequirement]:
    import re

    requirements: list[FixtureRequirement] = []
    value_pattern = r"(?:[A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)"
    path_pattern = (
        r"[A-Za-z_]\w*"
        r"(?:->(?:[A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?|\.(?:[A-Za-z_]\w*)(?:\s*\[\s*\d+\s*\])?)+"
    )
    for part in re.split(r"\s*&&\s*", expr):
        part = part.strip()
        match = re.fullmatch(rf"({path_pattern})\s*(==|>=|>|<=|<)\s*({value_pattern})", part)
        if not match:
            continue
        target, relation, value = match.groups()
        requirements.append(object_path_value(_normalize_object_path(target), relation, value))
    return requirements


def _size_from_upper_bound(upper: str) -> str:
    import re

    match = re.fullmatch(r"([A-Za-z_]\w*(?:->[A-Za-z_]\w*)?)\s*-\s*1", upper)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d+", upper):
        return str(int(upper) + 1)
    return f"(({upper}) + 1)"


def _normalize_object_path(target: str) -> str:
    import re

    return re.sub(r"\s+", "", target)


def _dedupe(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen:
            continue
        out.append(line)
        seen.add(line)
    return out
