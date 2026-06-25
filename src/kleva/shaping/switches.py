from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CTypeCatalog, DerivedLocal
from .candidates import BranchCandidate


@dataclass(frozen=True)
class StateSwitchOps:
    infer_lookup_shape:                 Callable[..., list]
    good_path_setup_from_source:         Callable[..., list[str]]
    alias_pointer_guard_setup:           Callable[..., list[str]]
    lookup_container_setup:              Callable[..., list[str]]
    lookup_condition_setup:              Callable[..., list[str]]
    expand_alias_expr:                   Callable[..., str]
    condition_setup_lines:               Callable[..., list[str]]
    condition_function_pointer_setup:    Callable[..., tuple[list[str], list[str]]]
    callee_success_setups_in_block:      Callable[..., tuple[list[str], list[str]]]
    rewrite_source_alias_exprs:          Callable[..., str]
    safe_c_name:                         Callable[[str], str]


def switch_case_blocks(body: str, switch_start: int) -> list[tuple[str, str]]:
    tail = body[switch_start:]
    case_iter = list(re.finditer(r"\bcase\s+([A-Za-z_]\w*|0x[0-9a-fA-F]+|\d+)\s*:", tail))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(case_iter):
        start = m.end()
        end = case_iter[i + 1].start() if i + 1 < len(case_iter) else len(tail)
        default_m = re.search(r"\bdefault\s*:", tail[start:end])
        if default_m:
            end = start + default_m.start()
        blocks.append((m.group(1), tail[start:end]))
    return blocks


def state_switch_candidates(
    body: str,
    source_text: str | None,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str],
    ops: StateSwitchOps,
) -> list[BranchCandidate]:
    if "state-switches" not in shaping_features or not type_catalog:
        return []

    candidates: list[BranchCandidate] = []
    for shape in ops.infer_lookup_shape(body, source_text, type_catalog):
        for m in re.finditer(rf"switch\s*\(\s*{re.escape(shape.result_var)}->(\w+)\s*\)", body):
            switch_field = m.group(1)
            for case, case_body in switch_case_blocks(body, m.end()):
                setup = []
                setup.extend(ops.good_path_setup_from_source(body, aliases, decoded_aliases, direct_aliases, derived_aliases))
                setup.extend(ops.alias_pointer_guard_setup(body, aliases, type_catalog, {shape.container_expr}))
                setup.extend(ops.lookup_container_setup(shape, aliases, type_catalog))
                setup.extend(ops.lookup_condition_setup(shape, aliases, decoded_aliases, direct_aliases, derived_aliases))
                container_expr = ops.expand_alias_expr(shape.container_expr, aliases)
                setup.append(f"{container_expr}->{shape.array_field}[0].{switch_field} = {case};")
                candidates.append(BranchCandidate(
                    ops.safe_c_name(f"source_{shape.result_var}_{switch_field}_{case}"),
                    setup,
                ))
                result_expr = f"{container_expr}->{shape.array_field}[0]"
                for idx, cond in enumerate(re.findall(r"\bif\s*\(([^{};]+)\)", case_body), 1):
                    cond_setup = ops.condition_setup_lines(
                        cond,
                        aliases,
                        decoded_aliases,
                        direct_aliases,
                        derived_aliases,
                        shape.result_var,
                        result_expr,
                    )
                    fp_setup, fp_preamble = ops.condition_function_pointer_setup(
                        cond,
                        shape.result_var,
                        result_expr,
                        shape.element_type,
                        type_catalog,
                    )
                    callee_setup, callee_preamble = ops.callee_success_setups_in_block(
                        case_body,
                        source_text,
                        type_catalog,
                    )
                    callee_setup = [
                        ops.rewrite_source_alias_exprs(line, aliases, shape.result_var, result_expr)
                        for line in callee_setup
                    ]
                    if not cond_setup and not fp_setup and not callee_setup:
                        continue
                    candidates.append(BranchCandidate(
                        ops.safe_c_name(f"source_{shape.result_var}_{switch_field}_{case}_guard_{idx}"),
                        [*setup, *cond_setup, *fp_setup, *callee_setup],
                        [*fp_preamble, *callee_preamble],
                    ))

    return candidates
