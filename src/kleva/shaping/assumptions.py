from __future__ import annotations

import re

from ..ast.model import CParam


def append_unique(lines: list[str], line: str, seen: set[str]) -> None:
    if line not in seen:
        lines.append(line)
        seen.add(line)


def literal_for_relation(op: str, rhs: str) -> str:
    if op == "<":
        return f"(({rhs}) > 0 ? ({rhs}) - 1 : 0)"
    if op == "<=":
        return rhs
    if op == ">":
        return f"(({rhs}) + 1)"
    return rhs


def nonmatching_value(value: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", value):
        return f"(({value}) + 1)"
    return f"(({value}) + 1)"


def param_access(param: str, suffix: str, param_refs: dict[str, tuple[str, str]] | None) -> str:
    if param_refs and param in param_refs:
        base, sep = param_refs[param]
        return f"{base}{sep}{suffix}"
    return f"{param}->{suffix}"


def rewrite_value(value: str, param_args: dict[str, str] | None) -> str:
    if param_args and value in param_args:
        return param_args[value]
    return value


def setup_for_quantified_arrays(
    expr: str,
    param_refs: dict[str, tuple[str, str]] | None,
    param_args: dict[str, str] | None,
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    exists = re.search(
        r"\\exists\s+integer\s+(\w+)\s*;\s*0\s*<=\s*\1\s*<\s*([A-Za-z_]\w*|\d+)\s*&&\s*(.+)",
        expr,
        flags=re.DOTALL,
    )
    if exists:
        idx, _bound, body = exists.groups()
        for obj, arr, field, value in re.findall(
            rf"(\w+)->(\w+)\s*\[\s*{re.escape(idx)}\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        ):
            value = rewrite_value(value, param_args)
            append_unique(lines, f"{param_access(obj, f'{arr}[0].{field}', param_refs)} = {value};", seen)
        return lines

    forall = re.search(
        r"\\forall\s+integer\s+(\w+)\s*;\s*0\s*<=\s*\1\s*<\s*([A-Za-z_]\w*|\d+)\s*==>\s*(.+)",
        expr,
        flags=re.DOTALL,
    )
    if forall:
        idx, bound, body = forall.groups()
        eq = re.search(
            rf"(\w+)->(\w+)\s*\[\s*{re.escape(idx)}\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
            body,
        )
        if eq:
            obj, arr, field, value = eq.groups()
            value = rewrite_value(value, param_args)
            target = param_access(obj, f"{arr}[kleva_i].{field}", param_refs)
            append_unique(lines, f"for (int kleva_i = 0; kleva_i < {bound}; kleva_i++) {target} = {value};", seen)
        return lines

    return lines


def assumption_setup_lines(
    assumes_exprs: list[str],
    params_by_name: dict[str, CParam],
    param_refs: dict[str, tuple[str, str]] | None = None,
    param_args: dict[str, str] | None = None,
    shaping_features: set[str] | None = None,
) -> list[str]:
    """
    Convert simple ACSL assumptions into concrete fixture setup.

    This is intentionally conservative: it never asserts an oracle. It only
    tries to build an input state closer to the behavior's preconditions.
    """
    lines: list[str] = []
    seen: set[str] = set()
    shaping_features = shaping_features or set()

    for expr in assumes_exprs:
        if "quantified-arrays" in shaping_features:
            for line in setup_for_quantified_arrays(expr, param_refs, param_args):
                append_unique(lines, line, seen)

        for part in re.split(r"\s*&&\s*", expr):
            part = part.strip()

            m = re.fullmatch(r"(\w+)->(\w+)\s*(==|>=|>|<=|<)\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)", part)
            if m:
                obj, field, op, rhs = m.groups()
                if obj in params_by_name:
                    rhs = rewrite_value(rhs, param_args)
                    value = rhs if op == "==" else literal_for_relation(op, rhs)
                    append_unique(lines, f"{param_access(obj, field, param_refs)} = {value};", seen)
                continue

            m = re.fullmatch(r"(\w+)\s*==\s*(\w+)->(\w+)", part)
            if m:
                lhs, obj, field = m.groups()
                if lhs in params_by_name and obj in params_by_name:
                    target = param_args.get(lhs, lhs) if param_args else lhs
                    source = param_access(obj, field, param_refs)
                    append_unique(lines, f"{target} = {source};", seen)
                continue

            m = re.fullmatch(r"(\w+)->(\w+)\s*==\s*(\w+)", part)
            if m:
                obj, field, rhs = m.groups()
                if rhs in params_by_name and obj in params_by_name:
                    target = param_args.get(rhs, rhs) if param_args else rhs
                    source = param_access(obj, field, param_refs)
                    append_unique(lines, f"{target} = {source};", seen)
                continue

            m = re.fullmatch(r"(\w+)->(\w+)(?:->(\w+))?\s*(==|!=)\s*(0x[0-9a-fA-F]+|\d+)", part)
            if m:
                obj, field1, field2, op, rhs = m.groups()
                if obj in params_by_name:
                    suffix = f"{field1}->{field2}" if field2 else field1
                    value = nonmatching_value(rhs) if op == "!=" else rhs
                    append_unique(lines, f"{param_access(obj, suffix, param_refs)} = {value};", seen)
                continue

            m = re.fullmatch(r"(\w+)->(\w+)\s*>=\s*(\w+)->(\w+)\s*\+\s*([A-Za-z_]\w*|\d+)", part)
            if m and m.group(1) == m.group(3) and m.group(1) in params_by_name:
                obj, field, _same_obj, base, offset = m.groups()
                offset = rewrite_value(offset, param_args)
                append_unique(lines, f"{param_access(obj, field, param_refs)} = {param_access(obj, base, param_refs)} + {offset};", seen)
                continue

            m = re.fullmatch(r"(\w+)->(\w+)\s*<\s*(\w+)->(\w+)\s*\+\s*([A-Za-z_]\w*|\d+)", part)
            if m and m.group(1) == m.group(3) and m.group(1) in params_by_name:
                obj, field, _same_obj, base, _offset = m.groups()
                append_unique(lines, f"{param_access(obj, field, param_refs)} = {param_access(obj, base, param_refs)};", seen)
                continue

            m = re.search(r"\\valid_read\(\s*(\w+)->data\s*\+\s*\(0\s*\.\.\s*\1->len\s*-\s*1\)\s*\)", part)
            if m:
                obj = m.group(1)
                if obj in params_by_name:
                    append_unique(lines, f"if ({obj}->len == 0) {obj}->len = 1;", seen)
                    append_unique(lines, f"memset({obj}->data, 0, {obj}->len);", seen)
                continue

            m = re.fullmatch(
                r"\(\(\s*([A-Za-z_]\w*)\s*\*\s*\)(\w+)->data\)->(\w+)\s*==\s*([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)",
                part,
            )
            if m:
                cast_type, obj, field, value = m.groups()
                if obj in params_by_name:
                    value = rewrite_value(value, param_args)
                    data_expr = param_access(obj, "data", param_refs)
                    append_unique(lines, f"(({cast_type} *){data_expr})->{field} = {value};", seen)
                continue

    return lines
