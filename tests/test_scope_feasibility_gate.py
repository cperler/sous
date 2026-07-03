"""The scope feasibility gate (issue #45): a completed SCOPE stage that explicitly
reports feasible=false parks the task at the human approval gate (BLOCKED_ON_HUMAN)
instead of advancing to implement a no-op. Fail-open on a missing/true field."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, Stage, StageStatus, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_scope(eng):
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.SCOPE
    return w


def test_scope_infeasible_parks_for_human(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_to_scope(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"feasible": False,
                           "blocked_reason": "requires an API that does not exist",
                           "plan": []},
    ))
    # Held at the human gate rather than advancing to implement.
    assert out["outcome"] == "scope_not_feasible_held"
    assert out["task_state"] == "blocked_on_human"

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.BLOCKED_ON_HUMAN
    # The scope stage itself is COMPLETED (it succeeded; feasibility is a routing gate).
    assert task.stages[Stage.SCOPE].status is StageStatus.COMPLETED
    # blocked_reason folded into task.context for the audit / downstream prompt.
    assert task.context.get("blocked_reason") == "requires an API that does not exist"
    # The cost of the (real) model call is still recorded.
    assert out["cost_usd"] > 0

    # An audit event carries WHY it parked.
    events = eng.store.read_events("r1")
    parked = [e for e in events if e.get("type") == "scope_not_feasible"]
    assert len(parked) == 1
    assert parked[0]["blocked_reason"] == "requires an API that does not exist"

    # No further work while held.
    assert eng.next_work("r1", "t1") is None
    # The run stays open (BLOCKED_ON_HUMAN is non-terminal).
    assert eng.store.load_run("r1").state.value == "running"

    # Human override: approve releases the task; next_work then skips the COMPLETED
    # scope stage and dispatches implement.
    eng.approve("r1", "t1", approved_by="human", what="override: proceed anyway")
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.IMPLEMENT


def test_scope_infeasible_without_reason_uses_default_message(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_to_scope(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"feasible": False, "plan": []},  # no blocked_reason
    ))
    assert out["outcome"] == "scope_not_feasible_held"
    events = eng.store.read_events("r1")
    parked = [e for e in events if e.get("type") == "scope_not_feasible"]
    assert len(parked) == 1
    assert "feasible=false" in parked[0]["blocked_reason"]


def test_scope_feasible_advances_as_before(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_to_scope(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"feasible": True, "plan": ["subtask-1"]},
    ))
    assert out["outcome"] == "stage_completed"
    assert out["next_stage"] == "implement"
    assert eng.store.load_task("r1", "t1").state is TaskState.RUNNING


def test_scope_fails_open_when_feasible_field_missing(tmp_path, project) -> None:
    """Fail-OPEN, mirroring the test-validate gate: a scope result that omits the
    feasible field must not dead-end otherwise-green work — only an explicit false parks."""
    eng = _engine(tmp_path, project)
    w = _advance_to_scope(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"plan": ["subtask-1"]},  # no feasible field
    ))
    assert out["outcome"] == "stage_completed"
    assert out["next_stage"] == "implement"
