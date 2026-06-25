from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..ast.model import CTypeCatalog, DerivedLocal
from .candidates import BranchCandidate


@dataclass(frozen=True)
class TableShapeOps:
    good_path_setup_from_source:     Callable[..., list[str]]
    host_to_network_fn:              Callable[[str], str]
    cast_field_expr:                 Callable[[str, str, str], str]
    function_pointer_stub_preamble:  Callable[..., list[str]]
    function_pointer_stub_name:      Callable[[str], str]
    safe_c_name:                     Callable[[str], str]


def loop_table_candidates(
    body: str,
    aliases: dict[str, tuple[str, str]],
    decoded_aliases: dict[str, tuple[str, str, str]],
    direct_aliases: dict[str, tuple[str, str]],
    derived_aliases: dict[str, DerivedLocal],
    type_catalog: CTypeCatalog | None,
    shaping_features: set[str],
    ops: TableShapeOps,
) -> list[BranchCandidate]:
    if not type_catalog or "loop-tables" not in shaping_features:
        return []

    candidates: list[BranchCandidate] = []
    good_setup = ops.good_path_setup_from_source(
        body,
        aliases,
        decoded_aliases,
        direct_aliases,
        derived_aliases,
    )

    for alias, (cast_type, expr) in aliases.items():
        pattern = (
            rf"{re.escape(alias)}->(\w+)->(\w+)\s*\[\s*(\w+)\s*\]\.(\w+)\s*==\s*([A-Za-z_]\w*|\d+)"
            rf"\s*&&\s*{re.escape(alias)}->\1->\2\s*\[\s*\3\s*\]\.(\w+)\s*==\s*(\w+)"
        )
        for m in re.finditer(pattern, body):
            (
                container_field,
                array_field,
                _idx,
                match_field_a,
                match_value_a,
                match_field_b,
                match_value_b,
            ) = m.groups()
            container_param = type_catalog.field_type(cast_type, container_field)
            if not container_param:
                continue
            container_type = container_param.base_type
            array_param = type_catalog.field_type(container_type, array_field)
            if not array_param:
                continue
            element_type = array_param.base_type
            element_fields = type_catalog.struct_fields.get(element_type, {})

            preamble: list[str] = []
            setup = list(good_setup)
            state_var = ops.safe_c_name(f"kleva_{alias}_{container_field}")
            setup.extend([
                f"{container_type} {state_var};",
                f"memset(&{state_var}, 0, sizeof({state_var}));",
                f"(({cast_type} *){expr})->{container_field} = &{state_var};",
            ])

            decoded_match = decoded_aliases.get(match_value_b)
            if decoded_match:
                decode_fn, decoded_alias, decoded_field = decoded_match
                if decoded_alias in aliases:
                    decoded_cast, decoded_expr = aliases[decoded_alias]
                    encode_fn = ops.host_to_network_fn(decode_fn)
                    if encode_fn:
                        target = ops.cast_field_expr(decoded_cast, decoded_expr, decoded_field)
                        setup.append(f"{target} = {encode_fn}(1);")
                        match_value_b = "1"

            setup.extend([
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_a} = {match_value_a};",
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_b} = {match_value_b};",
            ])

            for field_name, field_param in element_fields.items():
                fp_decl = type_catalog.function_pointer(field_param.base_type)
                if fp_decl and "function-pointers" in shaping_features:
                    preamble.extend(ops.function_pointer_stub_preamble(fp_decl))
                    setup.append(
                        f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{field_name} = "
                        f"{ops.function_pointer_stub_name(fp_decl.name)};"
                    )

            candidates.append(BranchCandidate(
                ops.safe_c_name(f"source_{alias}_{array_field}_match"),
                setup,
                preamble,
            ))

            miss_setup = list(good_setup)
            miss_setup.extend([
                f"{container_type} {state_var};",
                f"memset(&{state_var}, 0, sizeof({state_var}));",
                f"(({cast_type} *){expr})->{container_field} = &{state_var};",
                f"(({cast_type} *){expr})->{container_field}->{array_field}[0].{match_field_a} = 0;",
            ])
            candidates.append(BranchCandidate(
                ops.safe_c_name(f"source_{alias}_{array_field}_miss"),
                miss_setup,
                [],
            ))

    return candidates
