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
