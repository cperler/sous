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
    # the held task is now terminal-FAILED and the run finalizes (no longer open forever)
    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED
    assert eng.dispatchable("r1") == []
    assert eng.store.load_run("r1").state is RunState.FAILED
    # the durable rejection artifact IS the gate record: who/why/when
    artifact = eng.store.load_rejection("r1", "t1")
    assert artifact["rejected_by"] == "craig"
    assert artifact["reason"] == "genuinely infeasible"
    assert artifact["at"]  # timestamped


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
    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED
    assert eng.store.load_task("r1", "t2").state is TaskState.CASCADE_BLOCKED
    assert eng.store.load_run("r1").state is RunState.FAILED
