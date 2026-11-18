"""One mutation check that fails loudly instead of reading as a survivor (#542 part d).

A REVIEW proves its tests mean something by mutating a line and re-running the tests. Done
by hand in a shell loop, that check kept failing in ways that LOOK like results: zsh does
not word-split an unquoted ``pytest $T``, so a multi-path variable reaches pytest as one
nonexistent path; a project's ``-n auto`` collected nothing in a review workspace; a grep
over the output then printed nothing. Each time "no tests ran" read as "the mutation
survived".

``orchestrator mutation-check`` runs that check as one command:

* the test command is given as argv after ``--`` and executed without a shell, so there is
  no word-splitting to get wrong;
* the UNMUTATED baseline always runs first, and must pass with a non-zero count;
* each run is classified from the runner's own summary line, and a run that collected no
  tests, exited with a usage error, printed no pass/fail count, or timed out is reported as
  an error (exit 2, one JSON object naming which run and why) — never as a survivor;
* the mutated file is restored in ``finally`` and re-read to confirm it is byte-identical.

The count parsing follows pytest's terminal summary (``3 passed, 1 failed in 0.12s``,
``no tests ran in 0.01s``) and its exit codes (4 usage error, 5 no tests collected). That is
a convention of the runner, not of any project: a runner whose output carries no such
summary is reported as ``no_count`` — loud, never green — so the command is safe to point at
anything and decides nothing it cannot read.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

RunStatus = Literal[
    "passed", "failed", "no_tests", "usage_error", "no_count", "runner_error", "timeout"
]

#: Per-run default budget. Below a typical 120 s agent tool timeout for one run, so a
#: hanging mutant comes back as ``timeout`` instead of stalling the reviewer's shell call.
DEFAULT_TIMEOUT_S = 90.0

#: Exit code for every outcome that is NOT a verdict (bad baseline, unreadable run, timeout).
EXIT_NOT_CHECKED = 2

_TAIL_CHARS = 2000

# pytest's terminal summary line: counts, then the duration ("in 0.12s", "in 61.0s (0:01:01)").
_SUMMARY_DURATION = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_COUNT = re.compile(
    r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|rerun)\b"
)
_NO_TESTS = re.compile(r"\bno tests ran\b")

# pytest exit codes (pytest.ExitCode): 2 interrupted, 3 internal error, 4 usage error,
# 5 no tests collected. A usage error (a test path that does not exist — the unsplit zsh
# ``$T`` — or an unknown option) also prints "no tests ran"; the exit code names it better.
_RC_USAGE = 4
_RC_NO_TESTS = 5
_RC_ABORTED = (2, 3)


@dataclass(frozen=True)
class RunOutcome:
    """One test-command run, classified from its exit code and summary line."""

    status: RunStatus
    returncode: int | None
    passed: int = 0
    failed: int = 0  # failed + errors: anything that counts as a test going red
    duration_s: float = 0.0
    tail: str = ""


def parse_summary(output: str) -> dict[str, int] | None:
    """The counts on the runner's LAST summary line, or ``None`` when there is none.

    Searched from the bottom so a test that prints "3 passed" mid-run cannot pass for the
    summary. ``{}`` (an empty dict) means the summary said "no tests ran"."""
    for line in reversed(output.splitlines()):
        if not _SUMMARY_DURATION.search(line):
            continue
        counts: dict[str, int] = {}
        for n, kind in _COUNT.findall(line):
            key = "error" if kind.startswith("error") else kind
            counts[key] = counts.get(key, 0) + int(n)
        if counts:
            return counts
        if _NO_TESTS.search(line):
            return {}
    return None


def classify_run(returncode: int, output: str, *, duration_s: float = 0.0) -> RunOutcome:
    """Classify a finished run. Pure: no I/O, so every rule is unit-testable."""
    tail = output[-_TAIL_CHARS:]
    counts = parse_summary(output)

    def outcome(status: RunStatus, passed: int = 0, failed: int = 0) -> RunOutcome:
        return RunOutcome(status, returncode, passed, failed, round(duration_s, 3), tail)

    if returncode == _RC_USAGE:
        return outcome("usage_error")
    if returncode == _RC_NO_TESTS or counts == {}:
        return outcome("no_tests")
    if counts is None:
        return outcome("usage_error" if returncode in _RC_ABORTED else "no_count")
    passed = counts.get("passed", 0) + counts.get("xpassed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0)
    if passed + failed == 0:  # everything skipped / deselected: nothing was exercised
        return outcome("no_tests")
    if failed:
        return outcome("failed", passed, failed)
    if returncode != 0:  # red exit with no red test (e.g. a coverage floor): not a result
        return outcome("runner_error", passed, failed)
    return outcome("passed", passed, failed)


def run_tests(
    argv: list[str], *, cwd: Path, timeout_s: float, env: dict[str, str] | None = None
) -> RunOutcome:
    """Run ``argv`` (no shell) in ``cwd`` with a hard budget, and classify it.

    The child gets its own process group so a timeout kills pytest-xdist workers too, not
    just the parent. stderr is merged into stdout because pytest prints usage errors there."""
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True, env=env,
        )
    except OSError as exc:
        return RunOutcome("usage_error", None, tail=f"could not start {argv[0]!r}: {exc}")
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        return RunOutcome("timeout", None, duration_s=round(time.monotonic() - start, 3),
                          tail=(output or "")[-_TAIL_CHARS:])
    return classify_run(proc.returncode, output or "", duration_s=time.monotonic() - start)


@dataclass(frozen=True)
class Mutation:
    file: str
    line: int  # 1-indexed
    replacement: str

    @classmethod
    def parse(cls, spec: str) -> Mutation:
        """``FILE:LINE:REPLACEMENT`` — the replacement may itself contain colons."""
        parts = spec.split(":", 2)
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) < 1:
            raise ValueError(f"--mutate expects FILE:LINE:REPLACEMENT, got {spec!r}")
        return cls(parts[0], int(parts[1]), parts[2])


def _apply(original: str, mutation: Mutation) -> tuple[str, str, str]:
    """``(mutated text, original line, mutated line)``. The replacement takes the place of
    the line's text after its indentation, so ``return x + 1`` keeps the original's indent."""
    lines = original.splitlines(keepends=True)
    if mutation.line > len(lines):
        raise ValueError(f"{mutation.file} has {len(lines)} lines; no line {mutation.line}")
    old = lines[mutation.line - 1]
    body = old.rstrip("\r\n")
    ending = old[len(body):]
    indent = body[: len(body) - len(body.lstrip())]
    new_body = mutation.replacement if mutation.replacement[:1].isspace() else (
        indent + mutation.replacement
    )
    if new_body == body:
        raise ValueError(f"the mutation does not change {mutation.file}:{mutation.line}")
    lines[mutation.line - 1] = new_body + ending
    return "".join(lines), body, new_body


def split_quoted_command(argv: list[str], *, cwd: Path) -> list[str]:
    """Split a whole test command passed as ONE quoted word (``-- "uv run pytest -q t.py"``).

    Every model REVIEW in pred-2041-2116 did this first: the run has no shell, so the single
    word was taken as a program name and every check came back ``usage_error``. Only a lone
    word that contains whitespace and names no existing file is split; a command already in
    separate words is untouched, so an unsplit path list after a real program still fails
    loudly (sous#542)."""
    if len(argv) != 1 or not argv[0].split()[1:]:
        return argv
    if (cwd / argv[0]).exists():
        return argv
    return shlex.split(argv[0])


def mutation_check(
    argv: list[str], *, cwd: Path, mutation: Mutation | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, dict[str, object]]:
    """Baseline first, then the mutation. Returns ``(exit code, JSON-able report)``.

    ``outcome`` is ``killed`` / ``survived`` (a verdict, exit 0), ``baseline_passed`` (no
    mutation given, exit 0), or ``not_checked`` (exit 2) with ``failed_run`` and ``reason``
    naming which run could not be read and why."""
    argv = split_quoted_command(argv, cwd=cwd)
    if not argv:
        return EXIT_NOT_CHECKED, {"outcome": "not_checked", "failed_run": None,
                                  "reason": "no test command given after --"}
    report: dict[str, object] = {"argv": argv, "cwd": str(cwd)}

    def not_checked(run: str, reason: str) -> tuple[int, dict[str, object]]:
        return EXIT_NOT_CHECKED, {**report, "outcome": "not_checked",
                                  "failed_run": run, "reason": reason}

    target: Path | None = None
    mutated_text = original_bytes = None
    if mutation is not None:
        target = cwd / mutation.file
        try:
            original_bytes = target.read_bytes()
            mutated_text, old_line, new_line = _apply(original_bytes.decode(), mutation)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return not_checked("mutation", f"bad --mutate: {exc}")
        report["mutation"] = {"file": mutation.file, "line": mutation.line,
                              "original": old_line, "mutated": new_line}

    baseline = run_tests(argv, cwd=cwd, timeout_s=timeout_s)
    report["baseline"] = asdict(baseline)
    if baseline.status != "passed":
        return not_checked("baseline", baseline.status)
    if mutation is None or target is None or mutated_text is None or original_bytes is None:
        return 0, {**report, "outcome": "baseline_passed"}

    # Stale bytecode would run the ORIGINAL code: a .pyc is reused when the source's mtime
    # (whole seconds) and size match, and a same-length mutant written in the same second as
    # the baseline's compile matches both. So the mutant gets an mtime two seconds off the
    # original's, and the mutated run writes no bytecode for a later run to pick up.
    # Restoring puts back the original bytes AND times, leaving the file untouched.
    stat = target.stat()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        target.write_bytes(mutated_text.encode())
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        mutated = run_tests(argv, cwd=cwd, timeout_s=timeout_s, env=env)
    finally:
        target.write_bytes(original_bytes)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    report["mutated_run"] = asdict(mutated)
    if target.read_bytes() != original_bytes:
        return not_checked("mutation", f"{mutation.file} was not restored byte-for-byte")
    if mutated.status == "failed":
        return 0, {**report, "outcome": "killed"}
    if mutated.status == "passed":
        return 0, {**report, "outcome": "survived"}
    return not_checked("mutation", mutated.status)
