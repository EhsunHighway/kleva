from __future__ import annotations

import subprocess
import tempfile
from hashlib import sha1
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PreprocessedSource:
    path: Path
    text: str


class PreprocessWorkspace:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kleva-preprocessed-")
        self.path = Path(self._tmp.name)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def preprocess_source(
    source_path: str | Path,
    include_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    clang: str = "clang",
    out_dir: str | Path | None = None,
) -> PreprocessedSource:
    include_dirs = include_dirs or []
    extra_args = extra_args or []
    source = Path(source_path)
    target_dir = Path(out_dir) if out_dir is not None else source.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = sha1(str(source.resolve()).encode()).hexdigest()[:10]
    out_path = target_dir / f"{source.stem}.{digest}.kleva.i"
    cmd = [
        clang,
        "-E",
        "-P",
        *(f"-I{d}" for d in include_dirs),
        *extra_args,
        str(source),
    ]
    text = subprocess.check_output(cmd, text=True)
    out_path.write_text(text)
    return PreprocessedSource(path=out_path, text=text)
