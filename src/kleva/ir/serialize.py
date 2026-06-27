from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from .model import FunctionIR


def ir_to_jsonable(functions: dict[str, FunctionIR]) -> dict[str, Any]:
    return {
        name: _to_jsonable(func)
        for name, func in sorted(functions.items())
    }


def write_ir_json(functions: dict[str, FunctionIR], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ir_to_jsonable(functions), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        out: dict[str, Any] = {"kind": type(value).__name__}
        for field in fields(value):
            out[field.name] = _to_jsonable(getattr(value, field.name))
        return out
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
