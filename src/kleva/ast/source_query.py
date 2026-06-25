from __future__ import annotations

import re

from .model import CFunction
from .parser import strip_comments


def lower_first(s: str) -> str:
    """Lowercase the first character of a string."""
    return s[0].lower() + s[1:] if s else s


def camel_to_snake(s: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def visible_function(name: str, source_text: str | None) -> bool:
    if not source_text:
        return False
    return bool(re.search(rf"\b{re.escape(name)}\s*\(", source_text))


def function_body(source_text: str | None, func_name: str) -> str:
    if not source_text:
        return ""
    start = -1
    name_pat = re.compile(rf"\b{re.escape(func_name)}\s*\(")
    for m in name_pat.finditer(source_text):
        line_prefix = source_text[source_text.rfind("\n", 0, m.start()) + 1:m.start()]
        if ";" in line_prefix:
            continue
        close = source_text.find(")", m.end())
        if close == -1:
            continue
        brace = source_text.find("{", close)
        semi = source_text.find(";", close)
        if brace == -1 or (semi != -1 and semi < brace):
            continue
        start = brace + 1
        break
    if start == -1:
        return ""
    depth = 1
    i = start
    while i < len(source_text) and depth:
        if source_text[i] == "{":
            depth += 1
        elif source_text[i] == "}":
            depth -= 1
        i += 1
    return source_text[start:i - 1]


def function_definition_body(source_text: str | None, func_name: str) -> str:
    """Return a body only when `func_name` is found as a C function definition."""
    if not source_text:
        return ""
    text = strip_comments(source_text)
    pattern = re.compile(
        rf"\b(?:static\s+)?(?:inline\s+)?"
        rf"(?:const\s+)?(?:struct\s+)?\w+(?:\s*\*+)?"
        rf"\s*{re.escape(func_name)}\s*\([^)]*\)\s*\{{",
        flags=re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return ""

    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def function_frees_param(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Detect simple ownership transfer: the target function calls some *_free(param)
    or free(param), so generated tests should not free that parameter again.
    """
    body = function_body(source_text, func_name)
    if not body:
        return False
    return bool(re.search(rf"\b(?:\w+_free|free)\s*\(\s*{re.escape(param_name)}\s*\)", body))


def function_accepts_null_param(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Decide whether a no-ACSL pointer parameter is safe to test as NULL.

    A pointer type alone is not a contract. Without an explicit ACSL null
    behavior, synth only emits a NULL case when the source has a recognizable
    null guard for that parameter.
    """
    body = function_body(source_text, func_name)
    if not body:
        return False
    name = re.escape(param_name)
    if re.search(rf"\bif\s*\(\s*!\s*{name}\s*\)", body):
        return True
    if re.search(rf"\bif\s*\(\s*{name}\s*==\s*NULL\s*\)", body):
        return True
    if re.search(rf"\bif\s*\(\s*NULL\s*==\s*{name}\s*\)", body):
        return True
    if func_name.endswith(("_free", "_destroy")) and re.search(rf"\bif\s*\(\s*{name}\s*\)", body):
        return True
    return False


def function_takes_param_ownership(source_text: str | None, func_name: str, param_name: str) -> bool:
    """
    Detect simple enqueue/ownership transfer patterns.

    If the function stores a pointer into an owner object or queue, a generated
    cleanup that also frees that pointer can double-free after the owner is
    destroyed. This is intentionally conservative: leaks in generated tests are
    better than invalid cleanup.
    """
    body = function_body(source_text, func_name)
    if not body:
        return False
    name = re.escape(param_name)
    return bool(re.search(rf"\b\w*(?:add|push|insert|append|schedule|enqueue)\w*\s*\([^;]*\b{name}\b(?!\s*->)", body))


def function_returns_owned_pointer(func: CFunction) -> bool:
    if not func.return_is_pointer:
        return False
    snake_type = camel_to_snake(func.return_base)
    lower_type = lower_first(func.return_base)
    constructor_names = {
        f"{lower_type}_create",
        f"{snake_type}_create",
        f"{lower_type}_new",
        f"{snake_type}_new",
        f"create_{lower_type}",
        f"create_{snake_type}",
    }
    return func.name in constructor_names or bool(re.search(r"(?:create|new|alloc)$", func.name))
