"""
eva.py — Run Frama-C EVA and parse singleton final-state values.

EVA (Value Analysis) abstract-interprets C code and, when the abstract value
of a variable at the end of a function is a singleton set {N}, that N is a
formally-proven concrete value — it can be used directly as an assert() oracle.

EVA log format (relevant lines):
    [eva:final-states] Values at end of function probe_create_tv001:
      out_len ∈ {0}          ← singleton   → use as oracle
      out_cap ∈ {255}        ← singleton
      p ∈ {{ &__malloc_42 }} ← heap address → not a singleton int, skip
      __fc_heap_status ∈ [--..--]  ← not singleton, skip
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess


@dataclass(frozen=True)
class EvaValue:
    name:      str
    raw:       str
    kind:      str
    singleton: int | None = None

    @property
    def is_singleton(self) -> bool:
        return self.kind == "singleton" and self.singleton is not None


@dataclass(frozen=True)
class EvaFunctionState:
    values:     dict[str, str] = field(default_factory=dict)
    singletons: dict[str, int] = field(default_factory=dict)
    nodes:      dict[str, EvaValue] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaPropertySummary:
    valid:   int = 0
    unknown: int = 0
    invalid: int = 0
    total:   int = 0


@dataclass(frozen=True)
class EvaReport:
    final_states: dict[str, EvaFunctionState] = field(default_factory=dict)
    warnings:     tuple[str, ...] = ()
    alarms:       tuple[str, ...] = ()
    parse_errors: tuple[str, ...] = ()
    assertions:   EvaPropertySummary = EvaPropertySummary()
    preconditions: EvaPropertySummary = EvaPropertySummary()
    raw_log_path: str | None = None
    timed_out:    bool = False

    def singletons_for(self, fn: str) -> dict[str, int]:
        state = self.final_states.get(fn)
        return dict(state.singletons) if state else {}

    def values_for(self, fn: str) -> dict[str, str]:
        state = self.final_states.get(fn)
        return dict(state.values) if state else {}

    def value_nodes_for(self, fn: str) -> dict[str, EvaValue]:
        state = self.final_states.get(fn)
        return dict(state.nodes) if state else {}

    def has_final_state(self, fn: str) -> bool:
        return fn in self.final_states


def run_eva(
    framac:        str,
    probe_file:    str,
    src_file:      str | None,
    src_inc:       str,
    precision:     int = 7,
    max_time:      int = 120,
    extra_flags:   list[str] | None = None,
    extra_sources: list[str] | None = None,
    extra_includes: list[str] | None = None,
    cpp_macros: list[str] | None = None,
) -> str:
    """
    Invoke frama-c EVA on (probe_file, optional src_file, *extra_sources).
    Returns the combined stdout+stderr log string.
    """
    all_incs = [src_inc] + (extra_includes or [])
    inc_args = " ".join([
        *(f"-I{d}" for d in all_incs),
        *(f"-D{macro}" for macro in (cpp_macros or [])),
    ])
    cmd = [
        framac,
        "-eva",
        "-eva-precision", str(precision),
        f"-cpp-extra-args={inc_args}",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(probe_file)
    if src_file is not None:
        cmd.append(src_file)
    if extra_sources:
        cmd.extend(extra_sources)

    timeout = None if max_time <= 0 else max_time
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout + result.stderr


# U+2208 ∈ appears literally in Frama-C output (not an ASCII equals)
# EVA emits two formats for final-state blocks:
#   one-line: [eva:final-states] Values at end of function XYZ:
#   two-line:  [eva:final-states]\n  Values at end of function XYZ:
_FN_RE      = re.compile(r'\[eva:final-states\] Values at end of function (\w+):')
_FN_BARE_RE = re.compile(r'^\[eva:final-states\]\s*$')   # bare tag, name on next line
_FN_CONT_RE = re.compile(r'^\s+Values at end of function (\w+):\s*$')  # continuation
_VALUE_RE   = re.compile(r'^\s+(\w+)\s+\u2208\s+(.+?)\s*$')
_SING_RE    = re.compile(r'^\s+(\w+)\s+\u2208\s+\{(-?\d+)\}\s*$')
_SECT_RE    = re.compile(r'^\[')    # any new top-level Frama-C section marker
_PROPERTY_SUMMARY_RE = re.compile(
    r'^\s*(Assertions|Preconditions)\s+'
    r'(\d+)\s+valid\s+(\d+)\s+unknown\s+(\d+)\s+invalid\s+(\d+)\s+total\b'
)


@dataclass(frozen=True)
class EvaLineToken:
    raw: str


@dataclass(frozen=True)
class EvaFinalStateStart(EvaLineToken):
    function: str


@dataclass(frozen=True)
class EvaFinalStateBare(EvaLineToken):
    pass


@dataclass(frozen=True)
class EvaFinalStateContinuation(EvaLineToken):
    function: str


@dataclass(frozen=True)
class EvaValueLine(EvaLineToken):
    value: EvaValue


@dataclass(frozen=True)
class EvaSectionLine(EvaLineToken):
    pass


@dataclass(frozen=True)
class EvaWarningLine(EvaLineToken):
    pass


@dataclass(frozen=True)
class EvaAlarmLine(EvaLineToken):
    pass


@dataclass(frozen=True)
class EvaParseErrorLine(EvaLineToken):
    pass


@dataclass(frozen=True)
class EvaPropertySummaryLine(EvaLineToken):
    kind:    str
    summary: EvaPropertySummary


@dataclass(frozen=True)
class EvaOtherLine(EvaLineToken):
    pass


def tokenize_eva_log(log: str) -> list[EvaLineToken]:
    """
    Tokenize Frama-C/EVA's text report into typed line nodes.

    Regex matching is deliberately contained here. The rest of KLEVA consumes
    EvaLineToken/EvaReport objects instead of scraping raw strings directly.
    """
    return [_tokenize_eva_line(line) for line in log.splitlines()]


def _tokenize_eva_line(line: str) -> EvaLineToken:
    stripped = line.strip()

    m = _FN_RE.search(line)
    if m:
        return EvaFinalStateStart(line, m.group(1))

    if _FN_BARE_RE.match(line):
        return EvaFinalStateBare(line)

    m = _FN_CONT_RE.match(line)
    if m:
        return EvaFinalStateContinuation(line, m.group(1))

    m_summary = _PROPERTY_SUMMARY_RE.match(line)
    if m_summary:
        return EvaPropertySummaryLine(
            line,
            m_summary.group(1),
            EvaPropertySummary(
                valid=int(m_summary.group(2)),
                unknown=int(m_summary.group(3)),
                invalid=int(m_summary.group(4)),
                total=int(m_summary.group(5)),
            ),
        )

    if "User Error:" in line or "Frama-C aborted" in line or "invalid user input" in line:
        return EvaParseErrorLine(line)

    if "[eva:alarm]" in line:
        return EvaAlarmLine(line)

    if "Warning:" in line:
        return EvaWarningLine(line)

    m_value = _VALUE_RE.match(line)
    if m_value:
        name, raw_value = m_value.group(1), m_value.group(2).strip()
        m_single = _SING_RE.match(line)
        singleton = int(m_single.group(2)) if m_single else None
        return EvaValueLine(line, EvaValue(name, raw_value, _classify_eva_value(raw_value, singleton), singleton))

    if _SECT_RE.match(line):
        return EvaSectionLine(line)

    return EvaOtherLine(line)


def _classify_eva_value(raw_value: str, singleton: int | None) -> str:
    if singleton is not None:
        return "singleton"
    if raw_value == "[--..--]":
        return "unknown"
    if raw_value.startswith("[") and raw_value.endswith("]"):
        return "interval"
    if raw_value.startswith("{{"):
        return "address_or_pointer_set"
    if raw_value.startswith("{") and raw_value.endswith("}"):
        return "set"
    return "other"


def parse_singletons(log: str) -> dict[str, dict[str, int]]:
    """
    Scan an EVA log and extract singleton integer values.

    Returns:
        { function_name: { variable_name: int_value } }

    Only variables whose abstract value is exactly {N} for some integer N are
    included. Heap addresses, intervals, and multi-element sets are ignored.
    """
    return {
        fn: state.singletons
        for fn, state in parse_eva_report(log).final_states.items()
    }


def parse_eva_report(log: str, *, raw_log_path: str | Path | None = None, timed_out: bool = False) -> EvaReport:
    """
    Parse the parts of a Frama-C/EVA report that KLEVA needs for trust and
    diagnosis.

    This intentionally keeps raw textual values in addition to singleton ints.
    A line such as ``out_x ∈ {4}`` becomes both a final-state value and a
    singleton. A line such as ``out_x ∈ {0; 1}`` is kept as a value but not a
    singleton, which lets diagnostics distinguish missing outputs from
    non-singleton outputs.
    """
    final_nodes: dict[str, dict[str, EvaValue]] = {}
    warnings: list[str] = []
    alarms: list[str] = []
    parse_errors: list[str] = []
    assertions = EvaPropertySummary()
    preconditions = EvaPropertySummary()

    cur_fn: str | None = None
    after_bare = False
    pending_warning_index: int | None = None

    for token in tokenize_eva_log(log):
        stripped = token.raw.strip()

        if isinstance(token, EvaParseErrorLine):
            parse_errors.append(stripped)

        if isinstance(token, EvaWarningLine) or (isinstance(token, EvaAlarmLine) and "Warning:" in token.raw):
            warnings.append(stripped)
            pending_warning_index = len(warnings) - 1
        elif pending_warning_index is not None and token.raw.startswith("  "):
            warnings[pending_warning_index] = f"{warnings[pending_warning_index]} {stripped}"
        else:
            pending_warning_index = None

        if isinstance(token, EvaAlarmLine):
            alarms.append(stripped)

        if isinstance(token, EvaPropertySummaryLine):
            if token.kind == "Assertions":
                assertions = token.summary
            else:
                preconditions = token.summary

        if isinstance(token, EvaFinalStateStart):
            cur_fn = token.function
            final_nodes[cur_fn] = {}
            after_bare = False
            continue

        if isinstance(token, EvaFinalStateBare):
            after_bare = True
            cur_fn = None
            continue

        if after_bare:
            if isinstance(token, EvaFinalStateContinuation):
                cur_fn = token.function
                final_nodes[cur_fn] = {}
            after_bare = False
            continue

        if cur_fn:
            if isinstance(token, EvaValueLine):
                final_nodes[cur_fn][token.value.name] = token.value
            elif isinstance(token, EvaSectionLine):
                cur_fn = None

    final_states = {
        fn: EvaFunctionState(
            values={name: value.raw for name, value in nodes.items()},
            singletons={
                name: value.singleton
                for name, value in nodes.items()
                if value.singleton is not None
            },
            nodes=nodes,
        )
        for fn, nodes in final_nodes.items()
    }
    return EvaReport(
        final_states=final_states,
        warnings=tuple(warnings),
        alarms=tuple(alarms),
        parse_errors=tuple(parse_errors),
        assertions=assertions,
        preconditions=preconditions,
        raw_log_path=str(raw_log_path) if raw_log_path is not None else None,
        timed_out=timed_out,
    )
