"""Failure retrospective auto-generation (DEFERRED row 2b)."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_retrospective
from orchestrator.retrospective import detect_failure_patterns
from orchestrator.schemas.enums import ResultStatus
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _fail(eng, run, task, *, error="boom", failures=None):
    """Dispatch the task's current stage and record a FAILURE for it."""
    w = eng.next_work(run, task)
    out = {"failures": failures} if failures is not None else {}
    return eng.record(run, make_result(w, status=ResultStatus.FAILURE, error=error,
                                       structured_output=out))


def test_max_attempts_failure_retrospective(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake ok
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope ok
    # implement fails 3 times with DIFFERENT errors -> exhausts attempts (not breaker)
    for i in range(3):
        out = _fail(eng, "r1", "t1", error=f"compile error line {i}")
    assert out["task_state"] == "failed"

    retro = eng.retrospective("r1")
    assert retro["run_state"] == "failed"
    assert retro["totals"] == {
        "total": 1, "completed": 0, "failed": 1, "cascade_blocked": 0, "closed_infeasible": 0,
    }
    ft = retro["failed_tasks"][0]
    assert ft["task_id"] == "t1"
    assert ft["failing_stage"] == "implement"
    assert ft["attempts"] == 3
    assert ft["terminal_reason"] == "task_failed_max_attempts"
    assert len(ft["learnings"]) == 3  # one per failed attempt
    # the artifact was auto-written at finalize
    assert (tmp_path / "retrospective.md").exists()
    assert "Failure retrospective" in (tmp_path / "retrospective.md").read_text()


def test_breaker_plateau_is_detected_as_pattern(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=9, breaker_threshold=2)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    # SAME failing-test set twice -> identical signature -> breaker trips
    _fail(eng, "r1", "t1", error="x", failures=["test_a", "test_b"])
    out = _fail(eng, "r1", "t1", error="x", failures=["test_a", "test_b"])
    assert out["task_state"] == "failed"

    retro = eng.retrospective("r1")
    assert retro["failed_tasks"][0]["terminal_reason"] == "task_failed_breaker"
    patterns = retro["patterns"]
    assert len(patterns) == 1
    assert patterns[0]["stage"] == "implement"
    assert patterns[0]["occurrences"] == 2
    assert patterns[0]["within_task_plateau"] is True  # the breaker's plateau


def test_cascade_blocked_appears_in_retrospective(tmp_path, project) -> None:
    project.task_source.deps = {"B": ["A"]}
    eng = _engine(tmp_path, project, max_attempts=1)
    eng.create_run("r1")
    eng.add_task("r1", "A")
    eng.add_task("r1", "B")  # depends on A
    _fail(eng, "r1", "A")  # A fails immediately (max_attempts=1)

    retro = eng.retrospective("r1")
    assert retro["totals"]["failed"] == 1 and retro["totals"]["cascade_blocked"] == 1
    assert retro["cascade_blocked_tasks"] == ["B"]
    assert retro["failed_tasks"][0]["blocked_dependents"] == ["B"]


def test_clean_run_writes_no_retrospective(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert not (tmp_path / "retrospective.md").exists()  # nothing to retrospect
    retro = eng.retrospective("r1")
    assert retro["failed_tasks"] == [] and retro["patterns"] == []


def test_detect_failure_patterns_cross_task() -> None:
    """The same signature hitting two tasks is flagged cross_task."""
    logs = {
        "A": [{"stage": "test", "status": "failure", "error": "ImportError: no module foo",
               "structured_output": {"failures": ["test_foo"]}}],
        "B": [{"stage": "test", "status": "failure", "error": "ImportError: no module foo",
               "structured_output": {"failures": ["test_foo"]}}],
    }
    patterns = detect_failure_patterns(logs)
    assert len(patterns) == 1
    assert patterns[0]["cross_task"] is True
    assert patterns[0]["tasks"] == ["A", "B"]
    assert patterns[0]["occurrences"] == 2


def test_render_retrospective_smoke() -> None:
    retro = {
        "run_id": "r1", "run_state": "failed",
        "totals": {"total": 2, "completed": 0, "failed": 1, "cascade_blocked": 1},
        "failed_tasks": [{"task_id": "A", "title": "do A", "failing_stage": "implement",
                          "attempts": 3, "terminal_reason": "task_failed_max_attempts",
                          "final_error": "boom", "learnings": ["implement (attempt 0): boom"],
                          "blocked_dependents": ["B"]}],
        "cascade_blocked_tasks": ["B"],
        "patterns": [{"signature": "abc123", "stage": "implement", "occurrences": 3,
                      "tasks": ["A"], "within_task_plateau": True, "cross_task": False,
                      "sample_error": "boom"}],
    }
    md = render_retrospective(retro)
    assert "# Failure retrospective — r1" in md
    assert "`A`" in md and "task failed max attempts" in md
    assert "Blocked dependents: `B`" in md
    assert "Recurring failure patterns" in md and "`abc123`" in md
