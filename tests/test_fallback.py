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
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-5"  # role default, no downgrade


def test_rate_limit_fallback_respects_capacity_gate(tmp_path, project) -> None:
    """A queued rate-limit fallback still obeys backpressure: at capacity, next_work
    waits; once capacity frees, it dispatches the cheaper model."""
    eng = _engine(tmp_path, project)
    w = _advance_to(eng, Stage.SCOPE)
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    with pytest.raises(CapacityExhausted):
        eng.next_work("r1", "t1", util_pct=95)  # over capacity -> still waits
    nxt = eng.next_work("r1", "t1", util_pct=0)  # capacity frees
    assert nxt.model == "claude-sonnet-5"  # the queued fallback, not lost


# --- capacity-aware downgrade (#12, effort-first ordering #96) ----------------

def _downgrade_events(eng, run_id="r1"):
    return [e for e in eng.store.read_events(run_id) if e.get("type") == "model_downgraded"]


def _effort_events(eng, run_id="r1"):
    return [e for e in eng.store.read_events(run_id) if e.get("type") == "effort_downgraded"]


def test_capacity_downgrade_opt_in_required(tmp_path, project) -> None:
    """OFF by default: a high-util fresh dispatch still runs the role default at the
    stage-spec effort, no event on either lever."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")  # route_by_capacity defaults False
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)  # high band, but opt-in is off
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-5"
    assert w.effort == "high"  # the scope spec default, untouched
    assert _downgrade_events(eng) == [] and _effort_events(eng) == []


def test_capacity_downgrade_when_opted_in_drops_effort_first(tmp_path, project) -> None:
    """route_by_capacity + high util -> a FRESH dispatch downshifts EFFORT one step (#96:
    the cheaper lever) and KEEPS the role-default model, with an effort_downgraded event."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-5"  # model held
    assert w.effort == "medium"  # high -> medium, one step
    ev = _effort_events(eng)
    assert len(ev) == 1
    assert ev[0]["from"] == "high" and ev[0]["to"] == "medium"
    assert ev[0]["util_pct"] == 75 and ev[0]["stage"] == "scope"
    assert _downgrade_events(eng) == []  # the model lever was never touched


def test_capacity_downgrade_model_only_when_effort_at_floor(tmp_path, project) -> None:
    """The MODEL downgrades only when the effort lever is unavailable: a task pinned to
    the low-effort floor drops opus -> sonnet exactly as pre-#96, with the model event."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1", effort="low")  # effort pinned at the floor
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.stage is Stage.SCOPE and w.model == "claude-sonnet-5"  # opus -> sonnet
    assert w.effort == "low"  # the pin held
    ev = _downgrade_events(eng)
    assert len(ev) == 1
    assert ev[0]["from"] == "claude-opus-5" and ev[0]["to"] == "claude-sonnet-5"
    assert _effort_events(eng) == []


# --- effort-aware adaptive band (#155, closes the #96/#141 loop) --------------

def _seed_group(eng, stage: Stage, effort: str, *, n: int, attempt: int, status: str) -> None:
    """Append `n` synthetic ledger rows for a (stage, effort) group so by_effort() reports
    a retry/failure history the adaptive band can react to."""
    import json
    eng.ledger.path.parent.mkdir(parents=True, exist_ok=True)
    with eng.ledger.path.open("a", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({
                "ts": "2026-07-18T00:00:00Z", "run_id": "seed", "task_id": f"s{i}",
                "stage": stage.value, "effort": effort, "model": "claude-opus-5",
                "attempt": attempt, "status": status, "cost_usd": 0.0, "duration_s": 1.0,
            }) + "\n")


def test_adaptive_band_high_retry_group_not_downshifted(tmp_path, project) -> None:
    """A (stage, effort) group whose history retries heavily gets a SMALLER band: at a util
    that downshifts a low-retry stage, this stage keeps full effort (edge raised past util)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    # scope@high has always retried (rate 1.0) -> edge rises to ~89, above util 75.
    _seed_group(eng, Stage.SCOPE, "high", n=6, attempt=1, status="success")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.stage is Stage.SCOPE and w.effort == "high"  # NOT downshifted — smaller band
    assert _effort_events(eng) == [] and _downgrade_events(eng) == []


def test_adaptive_band_low_retry_group_still_downshifts_with_audit(tmp_path, project) -> None:
    """A group with ample, clean history keeps the base edge -> still downshifts at util 75,
    and the event carries the observed rate, sample size, and effective threshold (auditable)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    _seed_group(eng, Stage.SCOPE, "high", n=6, attempt=0, status="success")  # rate 0.0
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.effort == "medium"  # base edge held -> downshifted one step as pre-#155
    ev = _effort_events(eng)
    assert len(ev) == 1
    assert ev[0]["observed_rate"] == 0.0 and ev[0]["sample_size"] == 6
    assert ev[0]["downgrade_threshold"] == eng.capacity.downgrade_threshold


def test_adaptive_band_min_sample_guard_falls_back(tmp_path, project) -> None:
    """Sparse history (below the min-sample floor) is too noisy to trust: the band falls
    back to today's flat threshold and the high-retry group downshifts as before."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    # Only 2 rows (< ADAPTIVE_BAND_MIN_SAMPLE) even though all retried -> ignored.
    _seed_group(eng, Stage.SCOPE, "high", n=2, attempt=1, status="success")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.effort == "medium"  # flat-threshold fallback -> downshifted
    ev = _effort_events(eng)
    assert len(ev) == 1
    # Sparse data was NOT acted on: audit fields record the fallback (no rate moved the edge).
    assert ev[0]["observed_rate"] is None and ev[0]["sample_size"] is None
    assert ev[0]["downgrade_threshold"] == eng.capacity.downgrade_threshold


def test_adaptive_band_disabled_ignores_history(tmp_path, project) -> None:
    """With the adaptive band off, a heavy-retry history is ignored — the flat band stands."""
    from orchestrator.capacity import CapacityPolicy
    eng = _engine(tmp_path, project, capacity=CapacityPolicy(adaptive_band=False))
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    _seed_group(eng, Stage.SCOPE, "high", n=6, attempt=1, status="success")  # rate 1.0
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.effort == "medium"  # adaptive off -> flat edge -> downshifted despite history


def test_capacity_downgrade_edges(tmp_path, project) -> None:
    """Band edges: 69 -> spec-default effort (no event); 70 -> effort downshifted."""
    for util, expect_effort in ((69, "high"), (70, "medium")):
        eng = _engine(tmp_path / f"u{util}", project)
        eng.create_run("r1", route_by_capacity=True)
        eng.add_task("r1", "t1")
        eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
        w = eng.next_work("r1", "t1", util_pct=util)
        assert w.model == "claude-opus-5"  # the model lever never fires first
        assert w.effort == expect_effort
        assert bool(_effort_events(eng)) == (util >= 70)


def test_capacity_downgrade_critical_band_unchanged(tmp_path, project) -> None:
    """At/above the per-call gate the behavior is UNCHANGED — wait, never a silent
    downgrade — even with route_by_capacity on."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    with pytest.raises(CapacityExhausted):
        eng.next_work("r1", "t1", util_pct=90)
    assert _downgrade_events(eng) == [] and _effort_events(eng) == []


def test_capacity_downgrade_honors_lane_pin(tmp_path, project) -> None:
    """A lane that disallows fallback is a pin: NEITHER lever is capacity-downgraded."""
    from orchestrator.routing import Router
    eng = _engine(tmp_path, project, router=Router(allow_fallback=False))
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=80)
    assert w.model == "claude-opus-5"  # pinned, not downgraded
    assert w.effort == "high"  # effort lever equally pinned by the lane
    assert _downgrade_events(eng) == [] and _effort_events(eng) == []


def test_capacity_downgrade_then_rate_limited_composes(tmp_path, project) -> None:
    """A high-util dispatch (effort downshifted, model held) that THEN rate-limits degrades
    the MODEL down the chain, and the capacity levers must NOT fire again on the re-queue
    (pending_fallback_model set) — no double-drop, floor/cooldown logic intact."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    # High util downshifts effort (high -> medium); the model stays opus (#96 ordering).
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.model == "claude-opus-5" and w.effort == "medium"
    # That dispatch rate-limits: fallback queues the NEXT chain step (sonnet), and the
    # capacity levers must NOT fire again on the re-queue (pending_fallback_model set).
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_rate_limited_fallback"
    assert eng.store.load_task("r1", "t1").pending_fallback_model == "claude-sonnet-5"
    nxt = eng.next_work("r1", "t1", util_pct=75)  # still high util
    assert nxt.model == "claude-sonnet-5"  # the queued fallback, NOT re-downgraded past it
    assert nxt.effort == "high"  # spec default: the re-queue skips the capacity levers
    # exactly one effort event across the whole path (the fresh dispatch only)
    assert len(_effort_events(eng)) == 1 and _downgrade_events(eng) == []


def test_capacity_downgrade_at_floor_is_noop(tmp_path, project) -> None:
    """When the effort is pinned at its floor AND the resolved model is already the chain
    floor, a high-util downgrade is a no-op (no cheaper tier on either lever), no event."""
    from orchestrator.capacity import CapacityPolicy
    # A policy that would drop 3 steps still can't go past haiku.
    eng = _engine(tmp_path, project, capacity=CapacityPolicy(downgrade_steps=9))
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1", effort="low")  # effort lever floored -> the model lever fires
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)
    assert w.model == "claude-haiku-4-5"  # 3+ steps from opus floors at haiku
    assert len(_downgrade_events(eng)) == 1  # opus -> haiku, one event
    assert _effort_events(eng) == []


# --- rate-limit re-dispatch on a cheaper model (#1) --------------------------

def test_rate_limit_requeues_on_cheaper_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=2)
    w = _advance_to(eng, Stage.SCOPE)
    assert w.model == "claude-opus-5"
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_rate_limited_fallback"
    assert out["task_state"] == "retrying"
    assert out["next_stage"] == "scope"  # same stage re-queued
    task = eng.store.load_task("r1", "t1")
    assert task.pending_fallback_model == "claude-sonnet-5"  # opus -> sonnet
    assert task.learnings == []  # transient: no learning burned
    assert task.error_signatures == []  # breaker untouched
    # re-dispatch uses the cheaper model at the SAME attempt
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.SCOPE and nxt.model == "claude-sonnet-5" and nxt.attempt == 0
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
    assert w2.model == "claude-sonnet-5"
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
    assert w.model == "claude-opus-5"
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w = eng.next_work("r1", "t1")
    assert w.model == "claude-sonnet-5"
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
                         schema_ref="implement", model="claude-opus-5", lane_policy=H, created_at="t")
    runner = HeadlessClaudeRunner(
        transport=lambda w: RawResult(None, exit_code=1, error="API error 429: rate limit exceeded")
    )
    assert runner.dispatch(wi).status is ResultStatus.RATE_LIMITED
    # a plain error is still a FAILURE, not a rate-limit
    runner2 = HeadlessClaudeRunner(
        transport=lambda w: RawResult(None, exit_code=1, error="TypeError: undefined")
    )
    assert runner2.dispatch(wi).status is ResultStatus.FAILURE
