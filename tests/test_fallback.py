"""Graceful model fallback: capacity-aware downgrade + rate-limit re-dispatch.

Wires the previously-dead MODEL_CHAIN / fallback_after / allow_fallback so the engine
degrades to a cheaper model instead of stalling (capacity) or hard-failing (rate-limit).
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import CapacityExhausted
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


# --- capacity backpressure (no silent model downgrade) -----------------------

def test_capacity_raises_for_every_path(tmp_path, project) -> None:
    """At/over the per-call gate, next_work refuses to dispatch (the caller waits) —
    capacity is backpressure, not a silent model downgrade."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake at util 0
    with pytest.raises(CapacityExhausted):
        eng.next_work("r1", "t1", util_pct=95)


def test_below_gate_dispatches_role_default(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=50)  # below the gate
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-4-8"  # role default, no downgrade


def test_rate_limit_fallback_respects_capacity_gate(tmp_path, project) -> None:
    """A queued rate-limit fallback still obeys backpressure: at capacity, next_work
    waits; once capacity frees, it dispatches the cheaper model."""
    eng = _engine(tmp_path, project)
    w = _advance_to(eng, Stage.SCOPE)
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    with pytest.raises(CapacityExhausted):
        eng.next_work("r1", "t1", util_pct=95)  # over capacity -> still waits
    nxt = eng.next_work("r1", "t1", util_pct=0)  # capacity frees
    assert nxt.model == "claude-sonnet-4-6"  # the queued fallback, not lost


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


def test_rate_limit_no_fallback_when_lane_disallows(tmp_path, project) -> None:
    """allow_fallback is honored (not dead): with it off, no cheaper-model re-queue —
    the rate limit is waited out (cooldown), never dodged with a downgrade."""
    from orchestrator.routing import Router
    eng = _engine(tmp_path, project, router=Router(allow_fallback=False),
                  max_attempts=3, breaker_threshold=9)
    w = _advance_to(eng, Stage.SCOPE)
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_rate_limited_cooldown"  # wait, don't downgrade
    task = eng.store.load_task("r1", "t1")
    assert task.pending_fallback_model is None  # no cheaper model queued
    assert task.not_before is not None  # parked until the window resets


def test_rate_limit_with_no_wait_budget_is_hard_failure(tmp_path, project) -> None:
    """max_rate_limit_waits=0 restores the old immediate-failure floor semantics."""
    from orchestrator.routing import Router
    eng = _engine(tmp_path, project, router=Router(allow_fallback=False),
                  max_attempts=3, breaker_threshold=9, max_rate_limit_waits=0)
    w = _advance_to(eng, Stage.SCOPE)
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_failed_will_retry"
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
    """Rate-limited on the cheapest model with NO cooldown budget -> normal failure.
    intake is deterministic (no model), so reach the floor by degrading a model stage
    down the chain opus -> sonnet -> haiku. (With budget, the floor now cooldowns —
    see test_capacity_wiring.py.)"""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9,
                  max_rate_limit_waits=0)
    w = _advance_to(eng, Stage.SCOPE)  # first model stage, on opus
    assert w.model == "claude-opus-4-8"
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w = eng.next_work("r1", "t1")
    assert w.model == "claude-sonnet-4-6"
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w = eng.next_work("r1", "t1")
    assert w.model == "claude-haiku-4-5"  # the floor
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
