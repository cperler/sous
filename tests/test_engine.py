"""End-to-end engine flow over the interactive lane (simulated runner)."""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import LANE_STAGES, ExecutionLane, ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _drive_to_completion(eng: Engine, run="r1", task="t1") -> list:
    """Supervisor loop: next -> simulate success -> record, until done."""
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        result = make_result(work)
        outcomes.append(eng.record(run, result))
    return outcomes


def test_full_task_runs_all_six_stages(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive_to_completion(eng)

    stages_run = [o["stage"] for o in outcomes]
    assert stages_run == [s.value for s in LANE_STAGES[ExecutionLane.FULL]]
    assert outcomes[-1]["outcome"] == "task_completed"

    status = eng.status("r1")
    assert status["tasks"]["t1"]["state"] == "completed"
    assert status["tasks"]["t1"]["pr_url"].endswith("/1234")
    assert all(
        status["tasks"]["t1"]["stages"][stage.value] == "completed"
        for stage in LANE_STAGES[ExecutionLane.FULL]
    )
    assert status["tasks"]["t1"]["stages"][Stage.SIMPLIFY.value] == "skipped"


def test_every_call_is_cost_attributed_clean(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    outcomes = _drive_to_completion(eng)

    assert all(o["lane_attributed"] for o in outcomes)  # each record saw the intended lane
    audit = eng.lane_audit("r1")
    assert audit["total_calls"] == 6  # one ledger row per stage — no bypass
    assert audit["clean"] is True
    assert audit["unattributed"] == 0 and audit["off_lane"] == 0
    # intake is the deterministic ENGINE lane; the five model stages are interactive:claude.
    assert audit["by_lane"] == {"engine:none": 1, "interactive:claude": 5}


def test_events_audit_balances_a_clean_run(tmp_path, project) -> None:
    # #175: a full run's dispatch/record timeline balances with no orphaned leases.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    _drive_to_completion(eng)

    audit = eng.status("r1")["events_audit"]
    assert audit["clean"] is True
    assert audit["orphans"] == []
    assert audit["dispatched"] == audit["recorded"] == 6  # one close per dispatch
    assert audit["superseded"] == 0 and audit["abandoned"] == 0
    assert audit["outstanding"] == 0  # a completed run holds no live lease


def test_events_audit_flags_an_orphaned_dispatch(tmp_path, project) -> None:
    # A stage_dispatched whose lease is never closed (the #142 orphan) is flagged. Drive the
    # audit over a synthetic timeline; the unknown run_id makes outstanding-lease lookup a
    # no-op, so an unclosed dispatch can only be an orphan.
    eng = _engine(tmp_path, project)
    events = [
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-closed"},
        {"type": "stage_recorded", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-closed"},
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t1",
         "stage": "review", "attempt": 1, "work_item_id": "w-orphan"},
    ]
    audit = eng.events_audit("r1", events=events)
    assert audit["clean"] is False
    assert [o["work_item_id"] for o in audit["orphans"]] == ["w-orphan"]
    assert audit["orphans"][0]["stage"] == "review"
    assert audit["dispatched"] == 2 and audit["recorded"] == 1


def test_events_audit_discounts_superseded_and_abandoned_leases(tmp_path, project) -> None:
    # A superseded (resume re-dispatch) or abandoned lease closes its dispatch — neither
    # is an orphan even though it never gets a stage_recorded.
    eng = _engine(tmp_path, project)
    events = [
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-old"},
        {"type": "lease_superseded", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-old", "superseded_by": "w-new"},
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-new"},
        {"type": "stage_recorded", "run_id": "r1", "task_id": "t1",
         "stage": "implement", "attempt": 1, "work_item_id": "w-new"},
        {"type": "stage_dispatched", "run_id": "r1", "task_id": "t2",
         "stage": "review", "attempt": 1, "work_item_id": "w-gone"},
        {"type": "dispatch_abandoned", "run_id": "r1", "task_id": "t2",
         "stage": "review", "attempt": 1, "work_item_id": "w-gone", "reason": "orphaned"},
    ]
    audit = eng.events_audit("r1", events=events)
    assert audit["clean"] is True
    assert audit["dispatched"] == 3
    assert audit["superseded"] == 1 and audit["abandoned"] == 1


def test_events_audit_clean_across_a_real_superseded_lease(tmp_path, project) -> None:
    # #142/#175: a resume re-dispatch supersedes the outstanding lease. The old
    # stage_dispatched never gets a stage_recorded, but its lease_superseded closes it —
    # the audit stays clean with the old lease counted, not flagged as an orphan.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w1 = eng.next_work("r1", "t1")
    assert w1 is not None
    w2 = eng.next_work("r1", "t1", resume=True)  # re-lease: supersedes w1
    assert w2 is not None and w2.id != w1.id
    eng.record("r1", make_result(w2))

    audit = eng.status("r1")["events_audit"]
    assert audit["clean"] is True
    assert audit["orphans"] == []
    assert audit["superseded"] == 1
    assert audit["dispatched"] == 2 and audit["recorded"] == 1


def test_lite_lane_skips_scope(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    outcomes = _drive_to_completion(eng)
    ran = [o["stage"] for o in outcomes]
    assert "scope" not in ran
    assert eng.status("r1")["tasks"]["t1"]["stages"]["scope"] == "skipped"


def test_contract_mismatch_rejected(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")
    bad = make_result(work).model_copy(update={"work_item_id": "not-the-one"})
    with pytest.raises(ContractError):
        eng.record("r1", bad)


def test_retry_then_fail_on_breaker(tmp_path, project) -> None:
    # Same failure signature twice -> structured circuit breaker trips before max_attempts.
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=2)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    # advance to the implement stage
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope

    w1 = eng.next_work("r1", "t1")
    assert w1.stage is Stage.IMPLEMENT and w1.attempt == 0
    o1 = eng.record("r1", make_result(w1, status=ResultStatus.FAILURE, error="boom X", structured_output={}))
    assert o1["outcome"] == "stage_failed_will_retry"
    assert o1["task_state"] == "retrying"

    w2 = eng.next_work("r1", "t1")
    assert w2.stage is Stage.IMPLEMENT and w2.attempt == 1  # retry with bumped attempt
    o2 = eng.record("r1", make_result(w2, status=ResultStatus.FAILURE, error="boom X", structured_output={}))
    assert o2["outcome"] == "task_failed_breaker"
    assert o2["task_state"] == "failed"


def test_resume_points_at_in_progress_stage(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake done
    eng.next_work("r1", "t1")  # scope dispatched but NOT recorded (simulate crash)

    # resume should point back at scope (running, no completed_at = crash marker)
    assert eng.resume("r1") == {"t1": "scope"}


def test_cost_recomputed_from_model_table(tmp_path, project) -> None:
    from orchestrator.model_table import DEFAULT_MODEL_TABLE
    from orchestrator.schemas.work import TokenUsage

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")  # intake -> haiku
    out = eng.record(
        "r1", make_result(work, tokens=TokenUsage(input=1_000_000, output=0))
    )
    expected = DEFAULT_MODEL_TABLE.cost_usd(work.model, TokenUsage(input=1_000_000, output=0))
    assert out["cost_usd"] == expected
