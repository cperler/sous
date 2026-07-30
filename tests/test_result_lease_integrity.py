"""#311 — a StageResult must answer the WorkItem that was actually dispatched.

``content_hash`` exists to tie a recorded result back to its dispatch; on the
interactive×claude lane the supervisor hand-assembles the WorkItem JSON, so a garbled or
cross-pasted digest is entirely representable (it happened twice in run ``batch-next5b``
on 2026-07-29). The engine's ``record()`` boundary already refused a mismatch, but nothing
LOCKED that behavior and the refusal left no trace in the run's durable log — a rejection
the operator only ever saw as an exception on stderr.

These tests pin both halves: the refusal itself (nothing lands, nothing is charged, the
lease survives so the stage can be re-recorded) and the warning-grade ``result_rejected``
audit event that makes it loud.
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Stage, StageStatus
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _scope_dispatch(eng: Engine, run: str = "r1", task: str = "t1"):
    """Drive past the deterministic intake and return the outstanding SCOPE WorkItem."""
    eng.create_run(run, ExecutionLane.FULL)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake (engine lane)
    work = eng.next_work(run, task)
    assert work.stage is Stage.SCOPE
    return work


def _types(eng: Engine, run: str = "r1") -> list[str]:
    return [e["type"] for e in eng.store.read_events(run)]


# --- the refusal --------------------------------------------------------------------

def test_truncated_content_hash_never_lands_as_a_stage_completion(tmp_path, project) -> None:
    """The live failure: a 16-char log preview echoed where the 64-char digest belongs."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    truncated = make_result(work).model_copy(
        update={"content_hash": work.content_hash[:16]}
    )

    with pytest.raises(ContractError, match="content_hash"):
        eng.record("r1", truncated)

    # Nothing recorded for THIS dispatch: no stage_recorded event naming it, and the
    # stage did not advance. (The earlier intake stage has its own, legitimate, record.)
    assert not [e for e in eng.store.read_events("r1")
                if e["type"] == "stage_recorded" and e["work_item_id"] == work.id]
    task = eng.store.load_task("r1", "t1")
    assert task.current_stage is Stage.SCOPE
    assert task.stages[Stage.SCOPE].status is StageStatus.RUNNING
    # Nothing charged: the refusal happens before the ledger row (the supervisor is not
    # billed for a result the engine would not accept).
    assert eng.ledger.existing_rows_for(work.id) == []
    # The lease SURVIVES, so the correct result can still be recorded (the refusal must
    # not strand the dispatch — see the recovery test below).
    assert task.pending_work_item_id == work.id
    assert task.pending_content_hash == work.content_hash


def test_refusal_emits_a_warning_grade_result_rejected_event(tmp_path, project) -> None:
    """A garbled hash must leave a trace in the run's log, not just on stderr."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    truncated = make_result(work).model_copy(
        update={"content_hash": work.content_hash[:16]}
    )

    with pytest.raises(ContractError):
        eng.record("r1", truncated)

    rejected = [e for e in eng.store.read_events("r1") if e["type"] == "result_rejected"]
    assert len(rejected) == 1
    ev = rejected[0]
    assert ev["level"] == "warning"
    assert ev["reason"] == "content_hash_mismatch"
    assert ev["task_id"] == "t1" and ev["stage"] == Stage.SCOPE.value
    assert ev["work_item_id"] == work.id
    # Both hashes are previewed WITH their length — a prefix alone renders a truncated
    # digest identical to the real one, which is exactly the failure being audited.
    assert "len 16" in ev["content_hash"] and "len 64" in ev["dispatched_content_hash"]
    assert "verbatim" in ev["detail"]  # the message tells the supervisor what to do


def test_a_hash_pasted_from_another_in_flight_dispatch_is_refused(tmp_path, project) -> None:
    """Consequence 2 of #311: several WorkItems in flight, the wrong digest copied.

    ``work_item_id`` is correct here — only the hash comes from the sibling task — so this
    is precisely the case where content_hash is the ONLY remaining guard."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    for task_id in ("t1", "t2"):
        eng.add_task("r1", task_id)
        eng.record("r1", make_result(eng.next_work("r1", task_id)))  # intake
    w1 = eng.next_work("r1", "t1")
    w2 = eng.next_work("r1", "t2")
    assert w1.content_hash != w2.content_hash  # different tasks => different work

    crossed = make_result(w1).model_copy(update={"content_hash": w2.content_hash})
    with pytest.raises(ContractError, match="content_hash"):
        eng.record("r1", crossed)

    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].status is StageStatus.RUNNING
    assert [e["reason"] for e in eng.store.read_events("r1")
            if e["type"] == "result_rejected"] == ["content_hash_mismatch"]


# --- the happy path is untouched ----------------------------------------------------

def test_the_correct_result_still_records_after_a_refusal(tmp_path, project) -> None:
    """A matching hash records exactly as today — and the refusal did not strand the
    dispatch, so the supervisor's recovery is simply to re-record the real result."""
    eng = _engine(tmp_path, project)
    work = _scope_dispatch(eng)
    with pytest.raises(ContractError):
        eng.record("r1", make_result(work).model_copy(
            update={"content_hash": work.content_hash[:16]}))

    out = eng.record("r1", make_result(work))

    assert out["recorded"] is True and out["outcome"] == "stage_completed"
    assert out["next_stage"] == Stage.IMPLEMENT.value
    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].status is StageStatus.COMPLETED
    assert [e["work_item_id"] for e in eng.store.read_events("r1")
            if e["type"] == "stage_recorded"][-1] == work.id


def test_engine_lane_stages_are_unaffected(tmp_path, project) -> None:
    """The deterministic ENGINE lane builds its own result from the WorkItem it was
    handed, so it can never disagree — a full run over it emits no rejection."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    while (work := eng.next_work("r1", "t1")) is not None:
        assert work.content_hash == eng.store.load_task("r1", "t1").pending_content_hash
        eng.record("r1", make_result(work))

    assert "result_rejected" not in _types(eng)
    assert eng.store.load_task("r1", "t1").state.value == "completed"
    # The deterministic lane really did run in this flow (not an all-model run).
    assert any(row["lane"] == ExecutionMode.ENGINE.value for row in eng.ledger.rows())
