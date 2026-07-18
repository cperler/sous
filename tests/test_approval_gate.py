"""blocked_on_human as a mechanism (2026-07-01 design pass §4): a held task is
non-terminal, non-dispatchable, non-cascading; the approval artifact is the gate.
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import ExecutionLane, RunState, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_held_task_is_not_dispatchable_and_keeps_the_run_open(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="approve the decomposed graph")
    assert eng.dispatchable("r1") == []  # not dispatchable...
    run = eng.store.load_run("r1")
    assert run.state is RunState.RUNNING  # ...but non-terminal: run does not finalize
    assert run.progress().blocked_on_human == 1


def test_approve_writes_the_artifact_and_releases_the_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="live PR to the product repo")
    eng.approve("r1", "t1", approved_by="craig", what="live PR to the product repo")
    assert eng.dispatchable("r1") == ["t1"]
    artifact = eng.store.load_approval("r1", "t1")
    assert artifact["approved_by"] == "craig"
    assert artifact["what"] == "live PR to the product repo"
    assert artifact["at"]  # timestamped
    # the released task completes normally
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED


def test_hold_commits_event_atomically_with_the_task_doc(tmp_path, project) -> None:
    # #199: hold_for_approval routes through commit_task_events, so a crash in the task-doc
    # write can never leave a BLOCKED_ON_HUMAN task with no held_for_approval event. Inject a
    # failure at the task-doc write and assert the event is durable while the doc is unchanged.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    orig_write = eng.store._write_task
    eng.store._write_task = lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        eng.hold_for_approval("r1", "t1", what="x")
    eng.store._write_task = orig_write
    # The event is on disk (appended BEFORE the failed task-doc write)...
    events = [e for e in eng.store.read_events("r1") if e["type"] == "held_for_approval"]
    assert len(events) == 1
    # ...but the task never advanced — no orphaned event/state mismatch.
    assert eng.store.load_task("r1", "t1").state is TaskState.PENDING


def test_approve_commits_event_atomically_and_writes_no_artifact_on_write_failure(
    tmp_path, project
) -> None:
    # #199: approve commits the `approved` event with the PENDING task doc via
    # commit_task_events; write_approval runs AFTER. A crash in the task-doc write leaves the
    # task still held and writes NO approval artifact (the gate record follows the commit).
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="x")
    orig_write = eng.store._write_task
    eng.store._write_task = lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        eng.approve("r1", "t1", approved_by="craig")
    eng.store._write_task = orig_write
    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN
    assert eng.store.load_approval("r1", "t1") is None  # write_approval never reached


def test_hold_refuses_in_flight_and_terminal_tasks(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.next_work("r1", "t1")  # outstanding dispatch (lease held)
    with pytest.raises(ContractError, match="outstanding dispatch"):
        eng.hold_for_approval("r1", "t1", what="x")


def test_approve_refuses_a_task_that_is_not_held(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    with pytest.raises(ContractError, match="not held"):
        eng.approve("r1", "t1", approved_by="craig")
    assert eng.store.load_approval("r1", "t1") is None  # no artifact on a refused approve


def test_reject_closes_a_held_task_and_finalizes_the_run(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="scope reported the task infeasible")
    eng.reject("r1", "t1", rejected_by="craig", reason="genuinely infeasible")
    # the held task is now terminal-CLOSED_INFEASIBLE (distinct from FAILED, #53) and the
    # run finalizes (no longer open forever)
    assert eng.store.load_task("r1", "t1").state is TaskState.CLOSED_INFEASIBLE
    assert eng.dispatchable("r1") == []
    # rejection-only run → honest non-failure rollup (#67), not FAILED
    assert eng.store.load_run("r1").state is RunState.COMPLETED_WITH_REJECTIONS
    # the durable rejection artifact IS the gate record: who/why/when
    artifact = eng.store.load_rejection("r1", "t1")
    assert artifact["rejected_by"] == "craig"
    assert artifact["reason"] == "genuinely infeasible"
    assert artifact["at"]  # timestamped


def test_reject_commits_event_atomically_and_writes_no_artifact_on_write_failure(
    tmp_path, project
) -> None:
    # #199: reject commits the terminal transition + `rejected` event via commit_task_events;
    # write_rejection + finalize follow. A crash in the task-doc write leaves the task still
    # held and writes NO rejection artifact (the gate record follows the commit, and its
    # read-back by _finalize_task_terminal is never reached).
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="infeasible")
    orig_write = eng.store._write_task
    eng.store._write_task = lambda _t: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        eng.reject("r1", "t1", rejected_by="craig", reason="nope")
    eng.store._write_task = orig_write
    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN
    assert eng.store.load_rejection("r1", "t1") is None  # write_rejection never reached


def test_reject_refuses_a_task_that_is_not_held(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    with pytest.raises(ContractError, match="not held"):
        eng.reject("r1", "t1", rejected_by="craig", reason="nope")
    assert eng.store.load_rejection("r1", "t1") is None  # no artifact on a refused reject


def test_reject_cascade_blocks_a_dependent_task(tmp_path, project) -> None:
    project.task_source.deps = {"t2": ["t1"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")  # depends on t1
    eng.hold_for_approval("r1", "t1", what="infeasible")
    eng.reject("r1", "t1", rejected_by="craig", reason="infeasible")
    assert eng.store.load_task("r1", "t1").state is TaskState.CLOSED_INFEASIBLE
    assert eng.store.load_task("r1", "t2").state is TaskState.CASCADE_BLOCKED
    assert eng.store.load_run("r1").state is RunState.FAILED
