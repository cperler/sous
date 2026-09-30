"""The OPTIMIZE benchmark gate (#520): the engine measures the speed claim itself.

#519 shipped OPTIMIZE with model-reported ``measurements`` — a claim no check could falsify.
This gate is the check CLAUDE.md asks for in place of firmer prose: the engine runs the
PROJECT ADAPTER's declared benchmark argv at the last-good checkpoint and again at the
stage's commit, and keeps the commit only when the numbers improve beyond the tolerance.

Two halves, tested separately:

* the pure decision layer (``orchestrator.benchmark_gate``) — what a benchmark's output is
  allowed to look like, and what counts as a win;
* the engine wiring, against a REAL git worktree, because "not kept" is a git reset and a
  dropped checkpoint, not a flag. The four outcomes the issue names each get a case: win
  kept, no win reverted, the benchmark red/unreadable, and no benchmark declared at all.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from orchestrator.benchmark_gate import (
    DEFAULT_BENCHMARK_TOLERANCE,
    Metric,
    compare_benchmarks,
    format_metric_rows,
    parse_benchmark_output,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_completion_note
from orchestrator.schemas.enums import ExecutionLane, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

# --- the pure decision layer --------------------------------------------------------------


def test_a_bare_number_is_a_millisecond_style_metric() -> None:
    """The short form: lower is better, because the common metric is a duration."""
    metrics, error = parse_benchmark_output('{"score_batch": 420.5}')
    assert error is None and metrics == {"score_batch": Metric(420.5, "", True)}


def test_the_long_form_carries_unit_and_direction() -> None:
    metrics, error = parse_benchmark_output(
        '{"throughput": {"value": 900, "unit": "ops/s", "lower_is_better": false}}'
    )
    assert error is None
    assert metrics == {"throughput": Metric(900.0, "ops/s", False)}


def test_only_the_last_non_empty_line_is_parsed() -> None:
    """A real benchmark prints its own table first; that must not defeat the gate."""
    metrics, error = parse_benchmark_output(
        'collecting...\nname  mean  stddev\n\n{"score_batch": 3.0}\n\n'
    )
    assert error is None and metrics is not None and metrics["score_batch"].value == 3.0


@pytest.mark.parametrize(
    ("stdout", "fragment"),
    [
        ("", "no output"),
        ("   \n\n", "no output"),
        ("not json at all", "not JSON"),
        ("[1, 2, 3]", "not an object"),
        ("{}", "empty metric object"),
        ('{"a": "fast"}', "value must be a number"),
        ('{"a": true}', "value must be a number"),
        ('{"a": {"value": null}}', "value must be a number"),
        ('{"a": Infinity}', "not finite"),
        ('{"a": {"value": 1, "lower_is_better": "yes"}}', "must be a bool"),
    ],
)
def test_unreadable_output_is_explained_not_raised(stdout, fragment) -> None:
    """An adapter printing garbage degrades the gate to advisory; it never breaks record()."""
    metrics, error = parse_benchmark_output(stdout)
    assert metrics is None and error is not None and fragment in error


def test_one_malformed_metric_fails_the_whole_parse() -> None:
    """Judging the readable subset would silently drop part of what was measured."""
    metrics, error = parse_benchmark_output('{"good": 1.0, "bad": "x"}')
    assert metrics is None and error is not None and "'bad'" in error


def _verdict(before: dict, after: dict, **kw) -> dict:
    return compare_benchmarks(
        {k: Metric(*v) if isinstance(v, tuple) else Metric(v) for k, v in before.items()},
        {k: Metric(*v) if isinstance(v, tuple) else Metric(v) for k, v in after.items()},
        **kw,
    )


def test_a_real_speedup_is_a_win() -> None:
    verdict = _verdict({"hot": 100.0}, {"hot": 50.0})
    assert verdict["kept"] and verdict["reason"] == "win"
    assert verdict["metrics"][0]["delta_fraction"] == pytest.approx(0.5)
    assert verdict["metrics"][0]["direction"] == "improved"


def test_higher_is_better_reads_a_throughput_gain_as_a_win() -> None:
    verdict = _verdict({"tps": (100.0, "ops/s", False)}, {"tps": (200.0, "ops/s", False)})
    assert verdict["kept"] and verdict["metrics"][0]["delta_fraction"] == pytest.approx(1.0)
    # The same numbers under the default direction would be a doubling of a duration.
    assert _verdict({"tps": 100.0}, {"tps": 200.0})["reason"] == "regressed"


def test_noise_inside_the_tolerance_is_not_a_win() -> None:
    assert _verdict({"hot": 100.0}, {"hot": 97.0})["reason"] == "no_win"
    assert _verdict({"hot": 100.0}, {"hot": 100.0})["reason"] == "no_win"


def test_the_tolerance_is_a_strict_threshold() -> None:
    """Exactly the tolerance is not "beyond" it — the boundary belongs to the noise band."""
    assert _verdict({"hot": 100.0}, {"hot": 95.0}, tolerance=0.05)["reason"] == "no_win"
    assert _verdict({"hot": 100.0}, {"hot": 94.9}, tolerance=0.05)["reason"] == "win"
    # A looser project tolerance keeps the same 5% measurement from clearing the bar.
    assert _verdict({"hot": 100.0}, {"hot": 90.0}, tolerance=0.2)["reason"] == "no_win"


def test_a_regression_beats_a_win_in_the_report() -> None:
    """Halving one path while doubling another has earned nothing, and the regression is the
    more serious half of that finding, so it is what the verdict names."""
    verdict = _verdict({"a": 100.0, "b": 100.0}, {"a": 10.0, "b": 300.0})
    assert not verdict["kept"] and verdict["reason"] == "regressed"


def test_a_metric_only_one_side_measured_is_reported_and_not_judged() -> None:
    verdict = _verdict({"gone": 10.0, "both": 100.0}, {"new": 1.0, "both": 10.0})
    assert verdict["reason"] == "win"  # `both` still carries the verdict
    assert {n["notice"] for n in verdict["notices"]} == {
        "metric_missing_after", "metric_missing_before"
    }
    assert [r["name"] for r in verdict["metrics"]] == ["both"]


def test_no_shared_metric_is_not_a_win() -> None:
    verdict = _verdict({"a": 1.0}, {"b": 1.0})
    assert not verdict["kept"] and verdict["reason"] == "no_metrics_compared"


def test_a_changed_unit_is_excluded_from_the_verdict() -> None:
    """100 ms -> 0.05 s is a speedup the numbers cannot prove; comparing them would."""
    verdict = _verdict({"hot": (100.0, "ms")}, {"hot": (0.05, "s")})
    assert verdict["reason"] == "no_metrics_compared"
    assert verdict["metrics"][0]["judged"] is False
    assert verdict["notices"][0]["notice"] == "metric_unit_changed"


def test_a_flipped_direction_keeps_the_baselines_meaning() -> None:
    """Adopting the new run's direction could turn a regression into a win."""
    verdict = _verdict({"hot": (100.0, "ms", True)}, {"hot": (200.0, "ms", False)})
    assert verdict["reason"] == "regressed"
    assert verdict["notices"][0]["notice"] == "metric_direction_changed"


def test_a_zero_baseline_has_no_ratio_but_still_has_a_direction() -> None:
    """0 ms -> 3 ms is a regression however small the absolute number; calling it flat would
    let a pass that made a free operation cost something through the gate."""
    regressed = _verdict({"hot": 0.0}, {"hot": 3.0})
    assert regressed["reason"] == "regressed"
    assert regressed["metrics"][0]["delta_fraction"] is None
    assert _verdict({"hot": 0.0}, {"hot": 0.0})["reason"] == "no_win"
    assert _verdict({"tps": (0.0, "", False)}, {"tps": (5.0, "", False)})["reason"] == "win"


def test_metric_lines_state_the_numbers_and_the_change() -> None:
    (line,) = format_metric_rows(_verdict({"hot": (100.0, "ms")}, {"hot": (40.0, "ms")})["metrics"])
    assert line == "hot: 100.0 ms -> 40.0 ms (+60.0%, improved)"
    (zero,) = format_metric_rows(_verdict({"hot": 0.0}, {"hot": 1.0})["metrics"])
    assert "baseline 0 (no ratio)" in zero


# --- engine wiring ------------------------------------------------------------------------


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _commit(cwd, message: str, *, name: str, body: str = "x") -> str:
    (cwd / name).write_text(body)
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-qm", message)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _head(cwd) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


# A benchmark stand-in: prints the JSON object for whichever revision is checked out, read
# from the worktree itself, so the engine's before/after resets are what change the numbers.
_BENCH = (
    "import pathlib,sys;"
    "sys.stdout.write('warming up\\n');"
    "sys.stdout.write(pathlib.Path('bench.json').read_text())"
)


class BenchmarkingProject(FakeProject):
    """A project that opts into OPTIMIZE *and* declares how to verify it."""

    benchmark_argv: list[str] | None = [sys.executable, "-c", _BENCH]
    declared_tolerance: object = None

    def agent_for(self, stage: Stage, role: str | None = None):
        return (
            {"implement": "impl-agent", "review": "code-reviewer",
             "docstring": "docstring-agent", "optimize": "performance-optimizer"}
        ).get(role)

    def benchmark_cmd(self) -> list[str]:
        if self.benchmark_argv is None:
            raise RuntimeError("no benchmark here")
        return list(self.benchmark_argv)

    def __getattr__(self, name: str):  # only consulted for attributes that don't exist
        if name == "benchmark_tolerance" and self.declared_tolerance is not None:
            return self.declared_tolerance
        raise AttributeError(name)


@pytest.fixture
def worktree(tmp_path):
    """A real git repo standing in for the task worktree, with a baseline benchmark result."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    base = _commit(wt, "base commit", name="bench.json", body=json.dumps({"hot": 100.0}))
    return wt, base


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _optimize_output(**kw) -> dict:
    return {
        "files_changed": ["hot.py"], "summary": "batched the inner loop",
        "committed": True, "measurements": [], "suggestions": [], **kw,
    }


def _to_optimize(eng: Engine, *, worktree: str, base_sha: str):
    """Drive the pipeline until the next dispatch is OPTIMIZE, with the context the gate reads."""
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    intake = {"branch": "b", "baseline_captured": True, "worktree": worktree,
              "base_sha": base_sha}
    eng.record("r1", make_result(eng.next_work("r1", "t1"), structured_output=intake))
    while (work := eng.next_work("r1", "t1")).stage is not Stage.OPTIMIZE:
        # No stage before OPTIMIZE checkpoints in this fake, so base_sha stays the anchor.
        eng.record("r1", make_result(work))
    return work


def _events(eng: Engine, etype: str) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == etype]


def _gate_events(eng: Engine) -> list[dict]:
    return [e for e in eng.store.read_events("r1")
            if str(e["type"]).startswith("optimize_benchmark_")]


def _record_optimize(eng: Engine, work, wt, *, after: object, message: str = "Optimize the loop"):
    """Commit a benchmark result standing in for the stage's own speed change, then record."""
    sha = _commit(wt, message, name="bench.json", body=json.dumps(after))
    outcome = eng.record(
        "r1",
        make_result(work, structured_output=_optimize_output(),
                    checkpoint={"tag": "task/r1/t1/optimize/0", "sha": sha}),
    )
    return sha, outcome


def test_a_measured_win_keeps_the_commit(tmp_path, worktree) -> None:
    wt, base = worktree
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    sha, _ = _record_optimize(eng, work, wt, after={"hot": 40.0})

    assert _head(wt) == sha  # the commit survives, and the tree is back at it
    task = eng.store.load_task("r1", "t1")
    assert task.last_checkpoint == {"tag": "task/r1/t1/optimize/0", "sha": sha}
    block = task.stages[Stage.OPTIMIZE].output["benchmark"]
    assert (block["status"], block["reason"], block["reverted"]) == ("verified", "win", False)
    assert block["metrics"][0]["before"] == 100.0 and block["metrics"][0]["after"] == 40.0
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_verified" and event["level"] == "info"
    # The engine ran the adapter's argv, not a tool it chose itself.
    assert [leg["argv"] for leg in block["legs"]] == [[sys.executable, "-c", _BENCH]] * 2
    assert [leg["phase"] for leg in block["legs"]] == ["before", "after"]


def test_no_measured_win_reverts_to_the_last_checkpoint(tmp_path, worktree) -> None:
    """The heart of the issue: an unmeasurable claim costs the commit."""
    wt, base = worktree
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    sha, outcome = _record_optimize(eng, work, wt, after={"hot": 98.0})

    assert _head(wt) == base and _head(wt) != sha  # reset to the last-good checkpoint
    task = eng.store.load_task("r1", "t1")
    assert task.last_checkpoint is None  # the unverified commit never becomes the anchor
    assert task.stages[Stage.OPTIMIZE].status.value == "completed"  # not a failure to retry
    assert outcome["outcome"] != "stage_failed"
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_no_win"
    assert event["level"] == "warning" and event["reverted"] is True
    # The event NAMES the measured numbers, so the revert is arguable from the run log alone.
    assert event["metrics"][0]["before"] == 100.0 and event["metrics"][0]["after"] == 98.0
    assert event["tolerance"] == DEFAULT_BENCHMARK_TOLERANCE
    assert "hot: 100.0 -> 98.0" in event["summary"][0]


def test_a_regression_reverts_even_though_something_improved(tmp_path, worktree) -> None:
    wt, base = worktree
    _git(wt, "rm", "-q", "bench.json")
    base = _commit(wt, "two metrics", name="bench.json",
                   body=json.dumps({"hot": 100.0, "cold": 100.0}))
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 10.0, "cold": 400.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_no_win" and event["reason"] == "regressed"


def test_an_adapter_tolerance_overrides_the_default(tmp_path, worktree) -> None:
    wt, base = worktree
    project = BenchmarkingProject()
    project.declared_tolerance = 0.5  # this project's benchmark is noisy; 10% proves nothing
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 85.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_no_win" and event["tolerance"] == 0.5


@pytest.mark.parametrize(
    ("declared", "notice"),
    [("half", "tolerance_unreadable"), (-0.2, "tolerance_invalid"),
     (float("nan"), "tolerance_invalid")],
)
def test_a_nonsense_tolerance_falls_back_to_the_default_loudly(
    tmp_path, worktree, declared, notice
) -> None:
    """A tolerance nobody can read must not quietly become a gate that passes everything."""
    wt, base = worktree
    project = BenchmarkingProject()
    project.declared_tolerance = declared
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 10.0})

    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_verified"  # a 90% win clears any sane bar
    assert event["tolerance"] == DEFAULT_BENCHMARK_TOLERANCE
    assert event["notices"][0]["notice"] == notice


def test_a_red_benchmark_reverts_and_reports_its_output(tmp_path, worktree) -> None:
    wt, base = worktree
    project = BenchmarkingProject()
    project.benchmark_argv = [sys.executable, "-c", "import sys;sys.stderr.write('boom');sys.exit(3)"]
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 1.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_failed" and event["level"] == "warning"
    assert event["reason"] == "benchmark_red" and event["reverted"] is True
    assert [leg["rc"] for leg in event["legs"]] == [3, 3]
    assert "boom" in event["legs"][0]["output_tail"]
    assert eng.store.load_task("r1", "t1").last_checkpoint is None


def test_a_benchmark_that_cannot_be_invoked_reverts(tmp_path, worktree) -> None:
    """A missing binary is a red leg, not an exception out of record()."""
    wt, base = worktree
    project = BenchmarkingProject()
    project.benchmark_argv = ["./definitely-not-a-real-benchmark-binary"]
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 1.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_failed"
    assert event["legs"][0]["rc"] == -1 and "error (" in event["legs"][0]["output_tail"]


def test_unreadable_benchmark_output_reverts(tmp_path, worktree) -> None:
    """Unverified must never read as green — including when the numbers are unparseable."""
    wt, base = worktree
    project = BenchmarkingProject()
    project.benchmark_argv = [sys.executable, "-c", "print('much faster, trust me')"]
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 1.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_failed"
    assert event["reason"] == "unreadable_output" and "not JSON" in event["detail"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda p: setattr(p, "benchmark_argv", None), "getter_raised"),
        (lambda p: setattr(p, "benchmark_argv", ["true"]), "noop"),
        (lambda p: setattr(p, "benchmark_argv", []), "noop"),
    ],
)
def test_an_undeclared_benchmark_is_advisory_and_keeps_nothing(
    tmp_path, worktree, mutate, reason
) -> None:
    wt, base = worktree
    project = BenchmarkingProject()
    mutate(project)
    eng = _engine(tmp_path, project)
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 1.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_unavailable"
    assert event["level"] == "warning" and event["reason"] == reason
    assert event["reverted"] is True
    assert eng.store.load_task("r1", "t1").last_checkpoint is None


def test_a_project_with_no_benchmark_hook_at_all_is_advisory(tmp_path, worktree) -> None:
    """The first user of OPTIMIZE wires its benchmark separately; until it does, the stage is
    unverified and therefore cannot keep anything."""
    wt, base = worktree

    class NoBenchmark(BenchmarkingProject):
        benchmark_cmd = None  # type: ignore[assignment]

    eng = _engine(tmp_path, NoBenchmark())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    _record_optimize(eng, work, wt, after={"hot": 1.0})

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_unavailable" and event["reason"] == "absent"


def test_a_no_op_pass_is_skipped_with_a_receipt(tmp_path, worktree) -> None:
    """Nothing committed means nothing to keep or revert — but a skipped gate must not read
    like a verified one, so the receipt still fires."""
    wt, base = worktree
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)

    eng.record("r1", make_result(
        work, structured_output=_optimize_output(files_changed=[], committed=False),
        checkpoint={"tag": "task/r1/t1/optimize/0", "sha": base},
    ))

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_skipped" and event["reason"] == "no_changes"
    # A no-op pass did not invalidate the anchor, so the checkpoint is absorbed as usual.
    assert eng.store.load_task("r1", "t1").last_checkpoint["sha"] == base


def test_uncommitted_leftovers_are_not_silently_measured_over(tmp_path, worktree) -> None:
    """Measuring the anchor means resetting the tree, which would delete them — so refuse to
    measure, and revert exactly as a retry would."""
    wt, base = worktree
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)
    sha = _commit(wt, "Optimize the loop", name="bench.json", body=json.dumps({"hot": 1.0}))
    (wt / "scratch.txt").write_text("half-finished work")

    eng.record("r1", make_result(work, structured_output=_optimize_output(),
                                 checkpoint={"tag": "task/r1/t1/optimize/0", "sha": sha}))

    assert _head(wt) == base
    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_unavailable"
    assert event["reason"] == "worktree_dirty"


def test_a_missing_worktree_degrades_loudly_instead_of_raising(tmp_path) -> None:
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(tmp_path / "gone"), base_sha="a" * 40)

    eng.record("r1", make_result(work, structured_output=_optimize_output(),
                                 checkpoint={"tag": "task/r1/t1/optimize/0", "sha": "b" * 40}))

    (event,) = _gate_events(eng)
    assert event["type"] == "optimize_benchmark_unavailable"
    assert event["reason"] == "worktree_missing" and event["reverted"] is False
    assert eng.store.load_task("r1", "t1").last_checkpoint is None


def test_the_completion_note_reports_the_verdict_not_the_claim(tmp_path, worktree) -> None:
    wt, base = worktree
    eng = _engine(tmp_path, BenchmarkingProject())
    work = _to_optimize(eng, worktree=str(wt), base_sha=base)
    _record_optimize(eng, work, wt, after={"hot": 98.0})

    note = render_completion_note(eng.store.load_task("r1", "t1"))

    assert "REVERTED — no measured win" in note
    assert "hot: 100.0 -> 98.0" in note
    assert "required improvement: more than 5.0%" in note
