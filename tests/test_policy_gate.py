"""REVIEW's policy gate blocks only on what the task caused (#591): the benchmark half (one
noisy reading or an overloaded machine does not block) and the lint half (reading tool output
into findings and comparing them against what INTAKE recorded at base)."""

from __future__ import annotations

import os
import subprocess

import pytest

from orchestrator.policy_gate import (
    MAX_GATE_OUTPUT_CHARS,
    apply_lint_baseline,
    fingerprints,
    gate_output_fields,
    parse_diagnostics,
    run_bench_gate,
    split_against_baseline,
)

# ruff 0.15's real default output, including the uv warning its stderr carries in a worktree.
RUFF_DEFAULT = """\
F401 [*] `os` imported but unused
 --> pkg/lt.py:1:8
  |
1 | import os
  |        ^^
2 | try:
3 |     pass
  |
help: Remove unused import: `os`

E722 Do not use bare `except`
 --> pkg/lt.py:4:1
  |
2 | try:
3 |     pass
4 | except:
  | ^^^^^^
5 |     pass
  |

Found 2 errors.
[*] 1 fixable with the `--fix` option.
warning: `VIRTUAL_ENV=/x/.venv` does not match the project environment path `.venv`
"""


def _proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["bench"], rc, out, "")


def _bench(monkeypatch, outcomes: list, *, load: float | None = 1.0, cpus: int | None = 8):
    """Run the gate with canned run outcomes. ``load`` is one number or a per-sample list."""
    samples = list(load) if isinstance(load, list) else None

    def getloadavg():
        value = samples.pop(0) if samples is not None else load
        if value is None:
            raise OSError("no load average here")
        return (value, value, value)

    monkeypatch.setattr(os, "getloadavg", getloadavg)
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    calls: list[list[str]] = []

    def run(argv, cwd):  # noqa: ANN001, ARG001 - mirrors the adapter's subprocess function
        calls.append(argv)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    entries = run_bench_gate(["bench", "--json"], "/wt", run)
    records = [e["gate_record"] for e in entries if "gate_record" in e]
    findings = [e for e in entries if "gate_record" not in e]
    assert len(records) == 1, "every outcome is recorded exactly once"
    return findings, records[0], calls


# --- the benchmark ----------------------------------------------------------------------

def test_green_runs_once_and_records_load_and_cpus(monkeypatch) -> None:
    findings, record, calls = _bench(monkeypatch, [_proc(0)], load=[2.5, 3.5])
    assert findings == [] and len(calls) == 1
    assert record["verdict"] == "passed" and record["cpu_count"] == 8
    (run,) = record["runs"]
    assert (run["rc"], run["load_start"], run["load_end"]) == (0, 2.5, 3.5)
    assert run["duration_s"] >= 0


def test_red_then_green_is_inconclusive_not_a_blocker(monkeypatch) -> None:
    findings, record, calls = _bench(monkeypatch, [_proc(1, "request 1.37x"), _proc(0)])
    assert len(calls) == 2 and record["verdict"] == "inconclusive"
    (finding,) = findings
    assert finding["blocking"] is False and finding["disposition"] == "drop"
    assert "inconclusive" in finding["description"]
    # The red run's numbers are what a reader wants, not the green re-run's empty output.
    assert "request 1.37x" in finding["description"]
    assert "do not re-run it by hand" in finding["description"]


def test_red_twice_at_low_load_blocks_and_says_not_to_rerun(monkeypatch) -> None:
    findings, record, calls = _bench(monkeypatch, [_proc(1), _proc(1, "request 1.4x")])
    assert len(calls) == 2 and record["verdict"] == "regressed"
    (finding,) = findings
    assert finding["blocking"] is True and finding["severity"] == "critical"
    assert "request 1.4x" in finding["description"]
    assert "8 CPUs" in finding["description"]
    assert "do not re-run it by hand" in finding["suggested_fix"]


@pytest.mark.parametrize("samples", [
    [9.0, 1.0, 1.0, 1.0],  # high before the first run
    [1.0, 9.0, 1.0, 1.0],  # high after the first run
    [1.0, 1.0, 9.0, 1.0],  # high before the re-run
    [1.0, 1.0, 1.0, 8.0],  # exactly one per CPU, after the re-run
])
def test_red_twice_with_high_load_at_any_sample_is_inconclusive(monkeypatch, samples) -> None:
    findings, record, _ = _bench(monkeypatch, [_proc(1), _proc(1)], load=samples)
    assert record["verdict"] == "inconclusive"
    assert findings[0]["blocking"] is False
    assert "overloaded" in findings[0]["description"]


def test_load_just_under_the_cpu_count_still_blocks(monkeypatch) -> None:
    findings, _, _ = _bench(monkeypatch, [_proc(1), _proc(1)], load=7.9)
    assert findings[0]["blocking"] is True


@pytest.mark.parametrize("second", [
    subprocess.TimeoutExpired(["bench"], 1800), FileNotFoundError("gone"), _proc(2, "usage"),
])
def test_rerun_that_gives_no_verdict_is_inconclusive(monkeypatch, second) -> None:
    findings, record, _ = _bench(monkeypatch, [_proc(1, "slow"), second])
    assert record["verdict"] == "inconclusive"
    assert findings[0]["blocking"] is False
    assert "slow" in findings[0]["description"]


@pytest.mark.parametrize("load,cpus", [(None, 8), (50.0, None)])
def test_unknown_load_or_cpu_count_does_not_count_as_high(monkeypatch, load, cpus) -> None:
    findings, record, _ = _bench(monkeypatch, [_proc(1), _proc(1)], load=load, cpus=cpus)
    assert findings[0]["blocking"] is True
    assert record["cpu_count"] == cpus
    assert (record["runs"][0]["load_start"] is None) == (load is None)


@pytest.mark.parametrize("first", [
    _proc(2, "usage"), subprocess.TimeoutExpired(["bench"], 1800), FileNotFoundError("gone"),
])
def test_first_run_with_no_verdict_is_unverified_with_no_rerun(monkeypatch, first) -> None:
    findings, record, calls = _bench(monkeypatch, [first])
    assert len(calls) == 1 and record["verdict"] == "unverified"
    assert findings[0]["blocking"] is False
    assert "UNVERIFIED" in findings[0]["description"]
    assert "disposition" not in findings[0]  # still files, as it always has


def test_benchmark_output_keeps_its_tail(monkeypatch) -> None:
    findings, _, _ = _bench(monkeypatch, [_proc(1), _proc(1, "x" * 5000 + "\nsolve_ms: 412")])
    assert "solve_ms: 412" in findings[0]["description"]
    assert len(findings[0]["description"]) < 2600


# --- reading tool output ----------------------------------------------------------------

def test_parses_ruffs_real_default_output() -> None:
    parsed = parse_diagnostics(RUFF_DEFAULT)
    assert [d.fingerprint for d in parsed] == [
        "pkg/lt.py: F401 [*] `os` imported but unused",
        "pkg/lt.py: E722 Do not use bare `except`",
    ]
    assert parsed[1].display == "pkg/lt.py:4: E722 Do not use bare `except`"


def test_ruff_concise_and_default_output_share_a_fingerprint() -> None:
    concise = "pkg/lt.py:4:1: E722 Do not use bare `except`\nFound 1 error."
    default = "E722 Do not use bare `except`\n --> pkg/lt.py:4:1\n"
    assert fingerprints(concise) == fingerprints(default)


def test_parses_mypy_and_ignores_its_notes() -> None:
    output = (
        'x.py:3: error: Incompatible types  [assignment]\n'
        'x.py:3: note: Consider using "Sequence"\n'
        "Found 1 error in 1 file (checked 2 source files)\n"
    )
    assert fingerprints(output) == ["x.py: error: Incompatible types [assignment]"]


def test_parses_tsc() -> None:
    assert fingerprints("src/a.ts(12,5): error TS2345: Bad argument.\n") == [
        "src/a.ts: error TS2345: Bad argument."
    ]


def test_parses_rustc_and_ignores_its_notes() -> None:
    output = (
        "error[E0308]: mismatched types\n"
        "  --> src/a.rs:2:5\n"
        "note: the lint level is defined here\n"
        "  --> src/lib.rs:1:9\n"
    )
    assert fingerprints(output) == ["src/a.rs: error[E0308]: mismatched types"]


def test_a_line_shift_does_not_change_the_fingerprint() -> None:
    before = 'x.py:10: error: Name "a" already defined on line 3  [no-redef]'
    after = 'x.py:25: error: Name "a" already defined on line 18  [no-redef]'
    assert fingerprints(before) == fingerprints(after)


def test_absolute_worktree_paths_are_stripped_so_a_baseline_can_be_shared() -> None:
    one = fingerprints("/wt/591/x.py:1:1: F401 unused\n", "/wt/591")
    two = fingerprints("/wt/592/x.py:9:1: F401 unused\n", "/wt/592/")
    assert one == two == ["x.py: F401 unused"]


def test_symlinked_worktree_path_is_stripped_too(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert fingerprints(f"{real.resolve()}/x.py:1:1: F401 unused\n", str(link)) == [
        "x.py: F401 unused"
    ]


@pytest.mark.parametrize("output", [
    "",
    "Traceback (most recent call last):\n  ValueError: boom\n",
    "lint bad",
    # The tool counted more than could be read, so something is being missed.
    "x.py:1:1: F401 unused\nx.py: error: unreadable form\nFound 2 errors.\n",
    # mypy stopped here, so a clean read of the rest would hide what it never checked.
    "x.py:1: error: invalid syntax  [syntax]\n"
    "Found 1 error in 1 file (errors prevented further checking)\n",
])
def test_output_that_cannot_be_trusted_reads_as_none(output) -> None:
    assert parse_diagnostics(output) is None


def test_source_lines_in_a_snippet_are_not_findings() -> None:
    output = "E1 bad\n --> a.py:1:1\n  |\n1 | x = 'b.py:3: not a finding'\n  |\n"
    assert fingerprints(output) == ["a.py: E1 bad"]


def test_comparison_is_a_multiset() -> None:
    parsed = parse_diagnostics("a.py:1:1: E722 bare\na.py:9:1: E722 bare\n")
    new, inherited = split_against_baseline(parsed, ["a.py: E722 bare"])
    assert [d.display for d in inherited] == ["a.py:1: E722 bare"]
    assert [d.display for d in new] == ["a.py:9: E722 bare"]


# --- applying the baseline to a finding -------------------------------------------------

def _red(output: object, **extra) -> dict:
    return {"description": "ruff gate is RED. Output: all of it", "severity": "critical",
            "suggested_fix": "Run ruff and fix what it reports.", "blocking": True,
            "gate_leg": "ruff (lint)", "gate_output": output, **extra}


def test_everything_inherited_becomes_one_advisory() -> None:
    (advisory,) = apply_lint_baseline(
        [_red("a.py:1:1: E722 bare\n")], ["a.py: E722 bare"], "/wt"
    )
    assert advisory["blocking"] is False and advisory["disposition"] == "drop"
    assert advisory["description"].startswith("ruff (lint) gate: 1 finding(s) already at base")
    assert "gate_output" not in advisory


def test_new_finding_blocks_and_lists_only_what_is_new() -> None:
    output = "a.py:1:1: E722 bare\nb.py:2:1: F401 unused\n"
    blocking, advisory = apply_lint_baseline([_red(output)], ["a.py: E722 bare"], "/wt")
    assert blocking["blocking"] is True and blocking["severity"] == "critical"
    assert "b.py:2: F401 unused" in blocking["description"]
    assert "E722" not in blocking["description"]
    assert "Run ruff" not in blocking["suggested_fix"]  # would send the fix after old debt
    assert not {"gate_leg", "gate_output"} & set(blocking)
    assert advisory["blocking"] is False and "E722" in advisory["description"]


def test_a_second_copy_of_an_inherited_error_blocks() -> None:
    blocking, _ = apply_lint_baseline(
        [_red("a.py:1:1: E722 bare\na.py:9:1: E722 bare\n")], ["a.py: E722 bare"], "/wt"
    )
    assert blocking["blocking"] is True and "1 new finding(s)" in blocking["description"]


@pytest.mark.parametrize("finding", [
    _red("not a tool's output"),      # unreadable
    _red("b.py:2:1: F401 unused\n"),  # nothing inherited, so the tool's own text stands
    _red(None),                       # not a string
])
def test_finding_is_left_as_written_when_nothing_can_be_excused(finding) -> None:
    (out,) = apply_lint_baseline([finding], ["a.py: E722 bare"], "/wt")
    assert out["blocking"] is True
    assert out["description"] == finding["description"]
    assert out["suggested_fix"] == finding["suggested_fix"]
    assert not {"gate_leg", "gate_output"} & set(out)


def test_finding_without_gate_output_blocks_and_an_empty_baseline_excuses_nothing() -> None:
    plain = {"description": "e2e policy: no spec change", "blocking": True}
    assert apply_lint_baseline([plain], ["a.py: E722 bare"], None) == [plain]
    (out,) = apply_lint_baseline([_red("a.py:1:1: E722 bare\n")], [], "/wt")
    assert out["blocking"] is True


def test_an_advisory_is_never_split() -> None:
    advisory = _red("a.py:1:1: E722 bare\n", blocking=False)
    (out,) = apply_lint_baseline([advisory], ["a.py: E722 bare"], "/wt")
    assert out["description"] == advisory["description"]


def test_a_long_list_of_new_findings_is_capped_and_says_so() -> None:
    output = "".join(f"f{i}.py:1:1: F401 unused import of a long name\n" for i in range(500))
    blocking, _ = apply_lint_baseline(
        [_red(output + "z.py:1:1: E1 old\n")], ["z.py: E1 old"], "/wt"
    )
    assert "500 new finding(s)" in blocking["description"]
    assert "more" in blocking["description"].splitlines()[-1]
    assert len(blocking["description"]) < 2300


def test_output_too_large_to_read_whole_is_not_sent() -> None:
    assert gate_output_fields("ruff", _proc(1, "x" * (MAX_GATE_OUTPUT_CHARS + 1))) == {}
    assert gate_output_fields("ruff", _proc(1, "x")) == {"gate_leg": "ruff", "gate_output": "x"}


# --- a benchmark the diff cannot reach (#634) ---------------------------------------------

def _bench_with_reach(monkeypatch, outcomes: list, changed, paths):
    """Run the gate with a changed-file list and a ``bench_paths`` declaration."""
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    calls: list[list[str]] = []

    def run(argv, cwd):  # noqa: ANN001, ARG001 - mirrors the adapter's subprocess function
        calls.append(argv)
        return outcomes.pop(0)

    entries = run_bench_gate(["bench"], "/wt", run, changed_files=changed, bench_paths=paths)
    records = [e["gate_record"] for e in entries if "gate_record" in e]
    findings = [e for e in entries if "gate_record" not in e]
    assert len(records) == 1, "every outcome is recorded exactly once"
    return findings, records[0], calls


@pytest.mark.parametrize("paths", [
    ("src/taker",), ["src/taker/"], ("./src/taker",), ("src/taker/**",), ("src/*/book.py",),
])
def test_change_outside_the_bench_paths_skips_without_running(monkeypatch, paths) -> None:
    changed = ["tools/report.py", "docs/x.md", "src/maker/quote.py", "src/takerx/a.py"]
    findings, record, calls = _bench_with_reach(monkeypatch, [], changed, paths)
    assert calls == [] and findings == []
    assert record["verdict"] == "skipped" and record["runs"] == []
    assert record["changed_files_count"] == 4
    assert record["bench_paths"] == list(paths)
    assert "none of the 4 changed file(s)" in record["reason"]


@pytest.mark.parametrize("changed", [
    ["tools/report.py", "src/taker/book.py"],  # one file under a directory prefix
    ["src/taker"],                             # the declared path itself
    ["./src/taker/new_module.py"],             # an untracked new file, ./-prefixed
])
def test_change_touching_the_bench_paths_runs_as_before(monkeypatch, changed) -> None:
    findings, record, calls = _bench_with_reach(monkeypatch, [_proc(0)], changed, ["src/taker"])
    assert len(calls) == 1 and findings == [] and record["verdict"] == "passed"


def test_glob_bench_path_that_matches_runs(monkeypatch) -> None:
    _, record, calls = _bench_with_reach(
        monkeypatch, [_proc(0)], ["src/taker/book.py"], ["src/*/book.py"]
    )
    assert len(calls) == 1 and record["verdict"] == "passed"


def test_red_reading_on_a_touched_path_still_reruns_and_blocks(monkeypatch) -> None:
    findings, record, calls = _bench_with_reach(
        monkeypatch, [_proc(1), _proc(1, "request 1.4x")], ["src/taker/book.py"], ["src/taker"]
    )
    assert len(calls) == 2 and record["verdict"] == "regressed"
    assert findings[0]["blocking"] is True and "request 1.4x" in findings[0]["description"]


@pytest.mark.parametrize("changed,paths", [
    (None, ["src/taker"]),            # changed files could not be read
    (["tools/x.py"], None),           # nothing declared
    (["tools/x.py"], []),             # empty declaration
    (["tools/x.py"], "src/taker"),    # a bare string, not a list
    (["tools/x.py"], ["src/taker", ""]),  # an empty entry
    (["tools/x.py"], ["src/taker", 3]),   # a non-string entry
    ("tools/x.py", ["src/taker"]),    # changed files not a list
])
def test_unknown_reach_or_a_broken_declaration_never_skips(monkeypatch, changed, paths) -> None:
    _, record, calls = _bench_with_reach(monkeypatch, [_proc(0)], changed, paths)
    assert len(calls) == 1 and record["verdict"] == "passed"


def test_empty_change_with_bench_paths_skips(monkeypatch) -> None:
    """A known, empty change cannot reach anything."""
    _, record, calls = _bench_with_reach(monkeypatch, [], [], ["src/taker"])
    assert calls == [] and record["verdict"] == "skipped"
