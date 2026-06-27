from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class IrDiagnostic:
    backend: str
    source:  str
    status:  str
    error:   str | None = None


def write_ir_diagnostics(diagnostics: list[IrDiagnostic], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(item) for item in diagnostics], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
