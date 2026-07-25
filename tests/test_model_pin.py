"""Per-task model pin (#84): a task can pin a model (e.g. claude-fable-5) that overrides the
role default on model-lane stages, is honored by the capacity downgrade (pins win), still
degrades down the rate-limit chain, and is validated against the task's provider at add time.
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.model_table import ENGINE_MODEL
from orchestrator.schemas.enums import ExecutionMode, ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _downgrade_events(eng, run_id="r1"):
    return [e for e in eng.store.read_events(run_id) if e.get("type") == "model_downgraded"]


# --- add-task provider validation --------------------------------------------

def test_add_task_stores_resolved_pin_from_alias(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", model="fable")
    assert task.model_pin == "claude-fable-5"  # alias resolved to the canonical id
    # and it round-trips through the store
    assert eng.store.load_task("r1", "t1").model_pin == "claude-fable-5"


def test_codex_task_cannot_pin_a_claude_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    with pytest.raises(ContractError, match="claude.*model but task"):
        eng.add_task("r1", "t1", provider_tag="codex", model="fable")


def test_claude_task_cannot_pin_a_codex_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    with pytest.raises(ContractError, match="codex.*model but task"):
        eng.add_task("r1", "t1", model="gpt-5.5")


def test_codex_task_can_pin_a_codex_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", provider_tag="codex", model="gpt-5.5")
    assert task.model_pin == "gpt-5.5"


def test_unknown_pin_raises_at_add_time(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    with pytest.raises(ValueError, match="unknown model"):
        eng.add_task("r1", "t1", model="gpt-9000")


# --- next_work honors the pin ------------------------------------------------

def test_pinned_model_wins_over_role_default_across_stages(tmp_path, project) -> None:
    """The pin overrides the role default on EVERY model-lane stage — a deep_reason stage
    (scope) and a review-role stage (test) both dispatch on the pinned tier, not opus/sonnet."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", model="fable")
    seen: dict[Stage, str] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.model
        eng.record("r1", make_result(w))
    # intake is deterministic (ENGINE lane) — the pin never touches it
    assert seen[Stage.INTAKE] == ENGINE_MODEL
    # every model-lane stage ran on the pin, regardless of its role
    assert seen[Stage.SCOPE] == "claude-fable-5"  # deep_reason (default opus)
    assert seen[Stage.TEST] == "claude-fable-5"  # review role (default sonnet)
    assert seen[Stage.REVIEW] == "claude-fable-5"


def test_unpinned_task_uses_role_default(tmp_path, project) -> None:
    """Sanity: without a pin, the first model stage still resolves the role default."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")  # scope
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-5"


def test_deterministic_stage_ignores_the_pin(tmp_path, project) -> None:
    """A stage opted onto the ENGINE lane runs at $0 on the engine sentinel, pin or not."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", model="fable", deterministic_stages=[Stage.TEST])
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.TEST:
        eng.record("r1", make_result(w))
    assert w is not None and w.stage is Stage.TEST
    assert w.lane_policy.execution_mode is ExecutionMode.ENGINE
    assert w.model == ENGINE_MODEL  # pin does not leak onto the deterministic runner


# --- interplay: rate-limit chain still degrades from the pin ------------------

def test_pinned_task_rate_limited_degrades_to_opus(tmp_path, project) -> None:
    """A pin is a STARTING tier, not an anti-fallback lock — a rate-limited fable dispatch
    re-queues on opus (fallback_after('claude-fable-5')), and that degrade takes precedence
    over the pin for the re-dispatch."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", model="fable")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")  # scope, on the pinned fable
    assert w.stage is Stage.SCOPE and w.model == "claude-fable-5"
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_rate_limited_fallback"
    assert eng.store.load_task("r1", "t1").pending_fallback_model == "claude-opus-5"
    nxt = eng.next_work("r1", "t1")  # the queued degrade wins over the pin
    assert nxt.stage is Stage.SCOPE and nxt.model == "claude-opus-5"


# --- interplay: capacity downgrade skips a pinned task -----------------------

def test_capacity_downgrade_skips_pinned_task(tmp_path, project) -> None:
    """Explicit pins win over the capacity downgrade (#12) — same rule as a lane pin. On a
    route_by_capacity run at high util, an unpinned task drops a tier but a pinned one holds."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", route_by_capacity=True)
    eng.add_task("r1", "t1", model="fable")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1", util_pct=75)  # high band
    assert w.stage is Stage.SCOPE and w.model == "claude-fable-5"  # held, not downgraded
    assert _downgrade_events(eng) == []
