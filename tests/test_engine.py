"""End-to-end engine flow over the interactive lane (simulated runner)."""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage
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
    assert stages_run == [s.value for s in Stage]  # intake..review in order
    assert outcomes[-1]["outcome"] == "task_completed"

    status = eng.status("r1")
    assert status["tasks"]["t1"]["state"] == "completed"
    assert status["tasks"]["t1"]["pr_url"].endswith("/1234")
    assert all(v == "completed" for v in status["tasks"]["t1"]["stages"].values())


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
