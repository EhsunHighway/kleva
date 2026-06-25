from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BranchCandidate:
    """A source-derived path goal that can be added to a valid fixture."""
    name:              str
    setup:             list[str]
    preamble:          list[str] = field(default_factory=list)
    oracle:            bool = True
    witness_outputs:   bool = False

