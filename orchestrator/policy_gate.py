"""Shared logic for the REVIEW policy gate (#65) that keeps it from blocking a task on
problems the task did not cause (#591). Adapters run the commands; this module decides what
their results mean, so a later fix reaches every adapter without regenerating one.

Two independent halves:

* **Lint/typecheck against base.** INTAKE records what the declared tools already report at
  ``base_sha`` (``Task.baseline_lint``, mirroring ``baseline_failures`` for tests). At REVIEW
  ``apply_lint_baseline`` blocks only on what that record does not contain. Everything here
  fails closed: output that cannot be read, or a baseline that was never captured, leaves the
  adapter's blocking finding exactly as it was.
* **The benchmark under machine load.** ``run_bench_gate`` re-runs a red benchmark once and
  records the load average and CPU count with every result. One red reading, or a red reading
  on an overloaded machine, is ``inconclusive`` (advisory). Only two red readings on a machine
  that was not overloaded block. Same idea as the flaky-test re-run in ``4dd12e6``.

The parsing and comparison functions are pure. ``run_bench_gate`` is the one function that
touches the clock and the load average; its subprocess call is injected.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

# Finding keys that are instructions to the engine, never part of a review issue.
_GATE_KEYS = frozenset({"gate_leg", "gate_output"})

# Output larger than this is not sent for comparison at all. Reading only part of a tool's
# output would hide any new finding in the part that was cut, so the finding stays blocking.
MAX_GATE_OUTPUT_CHARS = 1_000_000
# A fingerprint is compared exactly, so cutting it the same way on both sides is safe.
_MAX_FINGERPRINT_CHARS = 400
_NEW_FINDINGS_CAP = 2000  # chars of new findings listed in a blocking description
_BENCH_OUTPUT_CAP = 2000

# 1-minute load at or above this many runnable processes per CPU means the machine had no
# spare capacity, so a timing taken on it says nothing about the code being measured.
HIGH_LOAD_PER_CPU = 1.0


# --- reading tool output ----------------------------------------------------------------

# ``path:line[:col]: rest``: ruff --output-format=concise, flake8, mypy, go vet.
_SINGLE_LINE = re.compile(r"^(?P<path>[^\s:()]+):(?P<line>\d+)(?::\d+)*:\s*(?P<rest>\S.*)$")
# ``path(line,col): rest``: tsc.
_TSC = re.compile(r"^(?P<path>[^\s:()]+)\((?P<line>\d+),\d+\):\s*(?P<rest>\S.*)$")
# ``--> path:line:col`` under a ``CODE message`` line: ruff's default output, rustc, clippy.
_ARROW = re.compile(r"^\s*-->\s*(?P<path>[^\s:]+):(?P<line>\d+)(?::\d+)?\s*$")
# The tool's own count (ruff and mypy print one), used to catch a partial read.
_SUMMARY = re.compile(r"^Found (\d+) errors?\b")
_LINE_REFERENCE = re.compile(r"\bline \d+\b", re.IGNORECASE)
_NOT_A_FINDING = ("note:", "help:")
# mypy stopped at this error, so what it printed is not everything that is wrong.
_STOPPED_EARLY = "prevented further checking"


@dataclass(frozen=True)
class Diagnostic:
    """One finding: ``fingerprint`` has no location, so an unrelated edit that shifts lines
    cannot make an old error look new. ``display`` keeps the location for a human."""

    fingerprint: str
    display: str


def _diagnostic(path: str, line: str, message: str) -> Diagnostic | None:
    message = " ".join(message.split())
    if message.casefold().startswith(_NOT_A_FINDING):
        return None
    path = path.removeprefix("./")
    normalized = _LINE_REFERENCE.sub("line N", message)
    return Diagnostic(
        fingerprint=f"{path}: {normalized}"[:_MAX_FINGERPRINT_CHARS],
        display=f"{path}:{line}: {message}",
    )


def _strip_worktree(output: str, cwd: str | None) -> str:
    """Make absolute paths relative, so a baseline shared across worktrees still matches."""
    if not cwd:
        return output
    for prefix in {cwd.rstrip("/"), os.path.realpath(cwd).rstrip("/")}:
        if prefix:
            output = output.replace(f"{prefix}/", "")
    return output


def parse_diagnostics(output: str, cwd: str | None = None) -> list[Diagnostic] | None:
    """Read a red tool's output into findings, or None when it cannot be trusted: nothing
    recognizable (a crash), a count that disagrees with the tool's own summary, or a tool that
    says it stopped early. None makes the caller keep the finding blocking."""
    if _STOPPED_EARLY in output:
        return None
    found: list[Diagnostic] = []
    claimed: int | None = None
    previous = ""
    for raw in _strip_worktree(output, cwd).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        diagnostic = None
        if (arrow := _ARROW.match(line)) is not None:
            diagnostic = _diagnostic(arrow["path"], arrow["line"], previous)
        elif (match := _SINGLE_LINE.match(line) or _TSC.match(line)) is not None:
            diagnostic = _diagnostic(match["path"], match["line"], match["rest"])
        elif (summary := _SUMMARY.match(line)) is not None:
            claimed = int(summary[1])
        if diagnostic is not None:
            found.append(diagnostic)
        previous = line
    if not found or (claimed is not None and claimed != len(found)):
        return None
    return found


def fingerprints(output: str, cwd: str | None = None) -> list[str] | None:
    """What INTAKE stores for a red leg: the location-free fingerprints, or None if unreadable."""
    parsed = parse_diagnostics(output, cwd)
    return None if parsed is None else [d.fingerprint for d in parsed]


def split_against_baseline(
    diagnostics: Iterable[Diagnostic], baseline: Iterable[str]
) -> tuple[list[Diagnostic], list[Diagnostic]]:
    """``(new, inherited)``. A multiset comparison, so a second bare ``except`` in a file that
    already had one counts as new: each baseline entry excuses exactly one finding."""
    remaining = Counter(baseline)
    new: list[Diagnostic] = []
    inherited: list[Diagnostic] = []
    for diagnostic in diagnostics:
        if remaining[diagnostic.fingerprint] > 0:
            remaining[diagnostic.fingerprint] -= 1
            inherited.append(diagnostic)
        else:
            new.append(diagnostic)
    return new, inherited


def _capped_listing(diagnostics: list[Diagnostic]) -> str:
    lines: list[str] = []
    used = 0
    for index, diagnostic in enumerate(diagnostics):
        if used + len(diagnostic.display) > _NEW_FINDINGS_CAP:
            lines.append(f"… and {len(diagnostics) - index} more")
            break
        lines.append(diagnostic.display)
        used += len(diagnostic.display) + 1
    return "\n".join(lines)


def apply_lint_baseline(
    findings: Iterable[Mapping[str, object]], baseline: Iterable[str], cwd: str | None
) -> list[dict]:
    """Split each blocking finding that carries ``gate_output`` against the INTAKE baseline.

    A finding keeps its original text unless at least one of its diagnostics was already at
    base: no output, unreadable output, and all-new output all leave it as the adapter wrote
    it. Otherwise it becomes a blocking finding that lists only the new diagnostics (if any),
    so the fix cycle is not sent after inherited debt, plus one advisory giving the inherited
    count. That advisory is ``drop``, not ``file``: one inherited error would otherwise open
    one identical issue per task in a batch."""
    known = list(baseline)
    out: list[dict] = []
    for finding in findings:
        clean = {k: v for k, v in finding.items() if k not in _GATE_KEYS}
        output = finding.get("gate_output")
        parsed = (
            parse_diagnostics(output, cwd)
            if isinstance(output, str) and finding.get("blocking", True)
            else None
        )
        new, inherited = split_against_baseline(parsed or [], known)
        if not inherited:
            out.append(clean)
            continue
        leg = str(finding.get("gate_leg") or "policy")
        if new:
            out.append({
                **clean,
                "description": f"{leg} gate is RED on this change: {len(new)} new finding(s), "
                               f"on top of {len(inherited)} already at base. New:\n"
                               f"{_capped_listing(new)}",
                "suggested_fix": "Fix the findings listed above. The ones already at base are "
                                 "not this change's to fix.",
            })
        out.append({
            "description": f"{leg} gate: {len(inherited)} finding(s) already at base did not "
                           f"block.\n{_capped_listing(inherited)}",
            "severity": "important",
            "blocking": False,
            "disposition": "drop",
        })
    return out


def gate_output_fields(leg: str, proc: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """The keys an adapter adds to a red lint or type finding so the engine can compare it
    against base. Empty when the output is too large to read whole."""
    output = (proc.stdout or "") + (proc.stderr or "")
    return {} if len(output) > MAX_GATE_OUTPUT_CHARS else {"gate_leg": leg, "gate_output": output}


# --- the benchmark under machine load ---------------------------------------------------


@dataclass(frozen=True)
class _BenchRun:
    rc: int | None  # None: the command raised (timeout, failed to start)
    duration_s: float
    load_start: float | None
    load_end: float | None
    output: str
    error: str = ""

    def as_record(self) -> dict[str, object]:
        return {"rc": self.rc, "duration_s": self.duration_s,
                "load_start": self.load_start, "load_end": self.load_end}


def _sample_load() -> float | None:
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):  # unavailable on this platform
        return None


def _bench_once(
    argv: list[str], cwd: str, run: Callable[[list[str], str], subprocess.CompletedProcess[str]]
) -> _BenchRun:
    load_start = _sample_load()
    started = time.monotonic()
    rc: int | None = None
    output = error = ""
    try:
        proc = run(argv, cwd)
        rc = proc.returncode
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001 - a policy hook must never break record()
        error = f"{type(exc).__name__}: {exc}"
    return _BenchRun(rc, round(time.monotonic() - started, 3), load_start, _sample_load(),
                     output, error)


def _overloaded(run: _BenchRun, cpu_count: int | None) -> bool:
    """Unknown load or CPU count is not evidence of overload."""
    if not cpu_count:
        return False
    return any(load is not None and load >= cpu_count * HIGH_LOAD_PER_CPU
               for load in (run.load_start, run.load_end))


def _tail(run: _BenchRun) -> str:
    if len(run.output) > _BENCH_OUTPUT_CAP:
        return "…\n" + run.output[-_BENCH_OUTPUT_CAP:]
    return run.output


def _runs_summary(runs: list[_BenchRun], cpu_count: int | None) -> str:
    def load(value: float | None) -> str:
        return "unknown" if value is None else f"{value:.1f}"

    parts = [
        f"run {i}: {f'rc={r.rc}' if r.rc is not None else r.error}, "
        f"load {load(r.load_start)} to {load(r.load_end)}"
        for i, r in enumerate(runs, start=1)
    ]
    return f"{'; '.join(parts)}; {cpu_count or 'unknown'} CPUs"


def run_bench_gate(
    argv: list[str],
    cwd: str,
    run: Callable[[list[str], str], subprocess.CompletedProcess[str]],
) -> list[dict]:
    """Run the benchmark leg. Returns the finding (if any) plus one ``gate_record`` entry the
    engine events on every outcome, green included.

    rc 0 passes and rc 1 is a reported regression. Anything else on the first run means the
    benchmark did not judge the change: UNVERIFIED and advisory, with no re-run. rc 1 re-runs
    exactly once, and blocks only if both runs are rc 1 on a machine that was not overloaded.
    Otherwise the result is ``inconclusive``, advisory, and not filed as an issue, since it
    describes the machine at that moment rather than work anyone should pick up.

    Every message says the gate's result is the record: a stage told to go and recover the
    timings by hand spent its whole budget re-running a ten-minute benchmark (#591)."""
    command = " ".join(argv)
    cpu_count = os.cpu_count()
    runs = [_bench_once(argv, cwd, run)]
    first = runs[0]
    finding: dict | None
    if first.rc == 0:
        verdict, finding = "passed", None
    elif first.rc is None:
        verdict = "unverified"
        finding = {
            "description": f"benchmark gate could not run in the task worktree ({first.error}). "
                           "The gate is UNVERIFIED for this change.",
            "severity": "important",
            "blocking": False,
        }
    elif first.rc != 1:
        verdict = "unverified"
        finding = {
            "description": f"benchmark gate exited {first.rc} (not a pass/fail verdict) running "
                           f"`{command}`. The gate is UNVERIFIED for this change. "
                           f"Output:\n{_tail(first)}",
            "severity": "important",
            "blocking": False,
        }
    else:
        second = _bench_once(argv, cwd, run)
        runs.append(second)
        summary = _runs_summary(runs, cpu_count)
        overloaded = any(_overloaded(r, cpu_count) for r in runs)
        if second.rc == 1 and not overloaded:
            verdict = "regressed"
            finding = {
                "description": f"benchmark gate is RED on this change: `{command}` reported a "
                               f"regression on both runs with the machine not overloaded "
                               f"({summary}). Output:\n{_tail(second)}",
                "severity": "critical",
                "suggested_fix": "Fix the slowdown this output names. The gate already ran the "
                                 "benchmark twice and its result is the record, so do not "
                                 "re-run it by hand.",
                "blocking": True,
            }
        else:
            if second.rc == 1:
                why = "red on both runs, but the machine was overloaded"
            elif second.rc == 0:
                why = "red, then green on the re-run"
            else:
                why = "red, and the re-run gave no verdict"
            red = second if second.rc == 1 else first
            verdict = "inconclusive"
            finding = {
                "description": f"benchmark gate inconclusive: {why}.\n`{command}` ({summary}). "
                               f"Not a blocker, and this result is the record, so do not "
                               f"re-run it by hand. Output of the red run:\n{_tail(red)}",
                "severity": "important",
                "blocking": False,
                "disposition": "drop",
            }
    record = {"gate_record": {"leg": "benchmark", "verdict": verdict, "command": command,
                              "cpu_count": cpu_count, "runs": [r.as_record() for r in runs]}}
    return [finding, record] if finding is not None else [record]
