"""Graceful model fallback: capacity-aware downgrade + rate-limit re-dispatch.

Wires the previously-dead MODEL_CHAIN / fallback_after / allow_fallback so the engine
degrades to a cheaper model instead of stalling (capacity) or hard-failing (rate-limit).
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import CapacityExhausted
from orchestrator.model_table import DEFAULT_MODEL_TABLE as MT
from orchestrator.schemas.enums import (
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
    TaskState,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to(eng, target: Stage):
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not target:
        eng.record("r1", make_result(w))
    assert w is not None and w.stage is target
    return w


# --- capacity-aware downgrade (#2) -------------------------------------------

def test_capacity_downgrades_instead_of_raising(tmp_path, project) -> None:
    """A deep-reason stage at capacity runs the floor model instead of stalling."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake at util 0
    # scope normally = opus (deep_reason); at capacity it degrades to the cheapest.
    w = eng.next_work("r1", "t1", util_pct=95)
    assert w.stage is Stage.SCOPE
    assert w.model == MT.cheapest()  # haiku, not opus


def test_capacity_raises_when_already_at_floor(tmp_path, project) -> None:
    """Intake already uses the floor model — at capacity there's nothing cheaper, so wait."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # intake role = cheap_shell = floor model; can't downgrade -> CapacityExhausted
    with pytest.raises(CapacityExhausted):
        eng.next_work("r1", "t1", util_pct=95)


def test_no_downgrade_when_fallback_disabled(tmp_path, project) -> None:
    from orchestrator.routing import Router
    eng = _engine(tmp_path, project, router=Router(allow_fallback=False))
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    with pytest.raises(CapacityExhausted):  # scope can't downgrade -> wait
        eng.next_work("r1", "t1", util_pct=95)


# --- rate-limit re-dispatch on a cheaper model (#1) --------------------------

def test_rate_limit_requeues_on_cheaper_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=2)
    w = _advance_to(eng, Stage.SCOPE)
    assert w.model == "claude-opus-4-8"
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_rate_limited_fallback"
    assert out["task_state"] == "retrying"
    assert out["next_stage"] == "scope"  # same stage re-queued
    task = eng.store.load_task("r1", "t1")
    assert task.pending_fallback_model == "claude-sonnet-4-6"  # opus -> sonnet
    assert task.learnings == []  # transient: no learning burned
    assert task.error_signatures == []  # breaker untouched
    # re-dispatch uses the cheaper model at the SAME attempt
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.SCOPE and nxt.model == "claude-sonnet-4-6" and nxt.attempt == 0
    # and the fallback flag is consumed
    assert eng.store.load_task("r1", "t1").pending_fallback_model is None


def test_rate_limit_steps_down_then_succeeds(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_to(eng, Stage.SCOPE)  # opus
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w2 = eng.next_work("r1", "t1")  # sonnet
    assert w2.model == "claude-sonnet-4-6"
    eng.record("r1", make_result(w2))  # succeeds on the cheaper model
    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].status is StageStatus.COMPLETED


def test_rate_limit_at_floor_becomes_failure(tmp_path, project) -> None:
    """Rate-limited on the cheapest model (nothing to fall back to) -> normal failure."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_to(eng, Stage.INTAKE)  # intake uses the floor model
    assert w.model == MT.cheapest()
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_failed_will_retry"  # degraded to a real failure
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.RETRYING
    assert "no cheaper fallback" in (task.last_error or "")


# --- runner classifies a rate-limit error so the fallback actually fires live ---

def test_runner_classifies_rate_limit() -> None:
    from adapters.execution.headless_claude import HeadlessClaudeRunner
    from adapters.execution.transport import RawResult
    from orchestrator.schemas.work import LanePolicy, WorkItem

    H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    wi = WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
                         schema_ref="implement", model="claude-opus-4-8", lane_policy=H, created_at="t")
    runner = HeadlessClaudeRunner(
        transport=lambda w: RawResult(None, exit_code=1, error="API error 429: rate limit exceeded")
    )
    assert runner.dispatch(wi).status is ResultStatus.RATE_LIMITED
    # a plain error is still a FAILURE, not a rate-limit
    runner2 = HeadlessClaudeRunner(
        transport=lambda w: RawResult(None, exit_code=1, error="TypeError: undefined")
    )
    assert runner2.dispatch(wi).status is ResultStatus.FAILURE
