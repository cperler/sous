"""Batch-wide circuit breaker (#58, ports batch-orchestrator.sh:784-811): N
consecutive task failures pause the run — a systemic cause (broken env, bad base
branch) fails fast instead of burning every task's retry budget. Unpause resumes."""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _failing_runner(work):
    """Every model stage fails; deterministic intake succeeds (it never reaches a
    runner — the registry handles it — so this only sees model WorkItems)."""
    return [
        make_result(w, status=ResultStatus.FAILURE, error=f"systemic: {w.task_id}")
        if w.stage is not Stage.INTAKE else make_result(w)
        for w in work
    ]


def _green_runner(work):
    return [make_result(w) for w in work]


def _setup(tmp_path, project, n_tasks: int, **engine_kw):
    eng = _engine(tmp_path, project, **engine_kw)
    eng.create_run("r1")
    for i in range(n_tasks):
        eng.add_task("r1", f"t{i}")
    return eng


def test_breaker_pauses_run_after_consecutive_task_failures(tmp_path, project) -> None:
    # max_attempts=1 + breaker_threshold high => each task fails terminally on its
    # first model-stage failure; 4 tasks, threshold 3 => the 4th is never dispatched.
    eng = _setup(tmp_path, project, 4, max_attempts=1, breaker_threshold=9)
    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=3)
    status = sched.run("r1", _failing_runner)

    assert status["run_state"] == "paused"
    states = [t["state"] for t in status["tasks"].values()]
    assert states.count("failed") == 3  # the breaker stopped the bleeding
    assert "pending" in states  # the 4th task's budget survived
    events = [e for e in eng.store.read_events("r1") if e["type"] == "run_paused"]
    assert events and "circuit breaker" in events[0]["reason"]


def test_completion_resets_the_streak(tmp_path, project) -> None:
    eng = _setup(tmp_path, project, 4, max_attempts=1, breaker_threshold=9)
    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=3)

    fail_ids = {"t0", "t1", "t3"}  # t2 succeeds between the failures

    def runner(work):
        return [
            make_result(w, status=ResultStatus.FAILURE, error="boom")
            if w.task_id in fail_ids and w.stage is not Stage.INTAKE else make_result(w)
            for w in work
        ]

    status = sched.run("r1", runner)
    # 2 failures, then a completion (reset), then 1 failure — never 3 consecutive.
    assert status["run_state"] == "failed"  # finalized normally, NOT paused
    assert [e for e in eng.store.read_events("r1") if e["type"] == "run_paused"] == []


def test_unpause_resumes_scheduling(tmp_path, project) -> None:
    eng = _setup(tmp_path, project, 4, max_attempts=1, breaker_threshold=9)
    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=3)
    sched.run("r1", _failing_runner)
    assert eng.store.load_run("r1").state.value == "paused"

    # a paused run refuses to schedule…
    again = sched.run("r1", _green_runner)
    assert again["run_state"] == "paused"
    assert [s for s in again["tasks"].values() if s["state"] == "pending"]

    # …until unpaused; then the surviving task runs to completion.
    eng.unpause_run("r1")
    final = sched.run("r1", _green_runner)
    assert final["run_state"] == "failed"  # 3 failed earlier, but…
    assert [s for s in final["tasks"].values() if s["state"] == "completed"]  # t3 finished


def test_unpause_refuses_non_paused_run(tmp_path, project) -> None:
    eng = _setup(tmp_path, project, 1)
    with pytest.raises(ContractError, match="not paused"):
        eng.unpause_run("r1")


def test_breaker_disabled_with_zero_threshold(tmp_path, project) -> None:
    eng = _setup(tmp_path, project, 4, max_attempts=1, breaker_threshold=9)
    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=0)
    status = sched.run("r1", _failing_runner)
    assert status["run_state"] == "failed"  # ran to the end, never paused
    assert all(t["state"] == "failed" for t in status["tasks"].values())
