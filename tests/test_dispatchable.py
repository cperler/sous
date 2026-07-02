"""Dispatch-eligibility is one predicate (review Phase A #3).

Engine.dispatchable is the single source of truth; Scheduler.dispatchable delegates to
it. These tests pin the semantics at the boundary where the old Engine.ready disagreed
with Scheduler.dispatchable — a RETRYING task (which ready wrongly excluded) and a leased
task (which ready wrongly included).
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_engine_and_scheduler_agree(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    sched = Scheduler(eng)
    # a fresh PENDING task with no deps is dispatchable on both paths
    assert eng.dispatchable("r1") == ["t1"]
    assert sched.dispatchable("r1") == eng.dispatchable("r1")


def test_leased_task_is_not_dispatchable(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.next_work("r1", "t1")  # takes the dispatch lease (pending_work_item_id set)
    # old ready() ignored the lease and would re-pick t1; dispatchable excludes it
    assert eng.dispatchable("r1") == []
    assert Scheduler(eng).dispatchable("r1") == []


def test_retrying_task_is_dispatchable(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    # fail the first stage once -> the task lands in RETRYING with the lease released
    work = eng.next_work("r1", "t1")
    eng.record("r1", make_result(work, status=ResultStatus.FAILURE, error="boom",
                                 structured_output={}))
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.RETRYING
    assert task.pending_work_item_id is None
    # old ready() (PENDING/BLOCKED-only) excluded RETRYING; dispatchable includes it,
    # which is what the scheduler needs to re-dispatch the retry.
    assert eng.dispatchable("r1") == ["t1"]
    assert Scheduler(eng).dispatchable("r1") == ["t1"]


def test_terminal_task_is_not_dispatchable(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED
    assert eng.dispatchable("r1") == []
