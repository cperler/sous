"""Provider-aware model table (review Phase A #2, roadmap E1).

`model_for_role` resolves to the routed provider's model id (a codex stage no longer
gets a claude id shelled to `codex exec -m`), and `ledger.record` tolerates an unknown
model id (flag, don't raise) the way `analysis()` already does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.model_table import (
    DEFAULT_MODEL_TABLE,
    Role,
    provider_for_model,
    resolve_model_alias,
)
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def test_model_for_role_is_provider_aware() -> None:
    t = DEFAULT_MODEL_TABLE
    # claude is the default provider (existing behavior preserved)
    assert t.model_for_role(Role.DEEP_REASON) == "claude-opus-5"
    assert t.model_for_role(Role.DEEP_REASON, Provider.CLAUDE) == "claude-opus-5"
    # a codex-routed stage resolves to a codex model id, not a claude one
    codex_deep = t.model_for_role(Role.DEEP_REASON, Provider.CODEX)
    assert codex_deep == "gpt-5.5"
    assert not codex_deep.startswith("claude")
    # single supported codex model on the ChatGPT plan: every role pins to it
    assert t.model_for_role(Role.CHEAP_SHELL, Provider.CODEX) == "gpt-5.5"


def test_codex_models_are_priced_from_their_own_row() -> None:
    t = DEFAULT_MODEL_TABLE
    usage = TokenUsage(input=1_000_000, output=0)
    # codex model priced from the codex row, not a claude fallback
    assert t.cost_usd("gpt-5-codex", usage) == 1.25
    # sanity: different from the claude deep-reason model's input price
    assert t.cost_usd("claude-opus-5", usage) == 5.0


def test_fallback_stays_within_provider_chain() -> None:
    t = DEFAULT_MODEL_TABLE
    assert t.fallback_after("claude-opus-5") == "claude-sonnet-5"
    # single-entry codex chain: gpt-5.5 is both head and floor
    assert t.fallback_after("gpt-5.5") is None
    assert t.fallback_after("nonexistent") is None


def test_try_cost_usd_tolerates_unknown_model() -> None:
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("some-future-model", TokenUsage(input=100))
    assert cost == 0.0 and priced is False
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("claude-opus-5", TokenUsage(input=100))
    assert priced is True and cost > 0.0


def _result(model: str) -> StageResult:
    return StageResult(
        work_item_id="wi-1", content_hash="h", run_id="r1", task_id="t1",
        stage=Stage.IMPLEMENT, attempt=0, model=model, status=ResultStatus.SUCCESS,
        lane_used=LaneUsed(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX,
                           invocation="codex exec"),
        token_usage=TokenUsage(input=1000, output=200), completed_at="2026-07-01T00:00:00Z",
    )


def test_ledger_record_does_not_raise_on_unknown_model(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    # a model id absent from the table must be recorded, not dropped with a KeyError
    row = ledger.record(_result("model-not-in-table"))
    assert row["priced"] is False
    assert row["cost_usd"] == 0.0
    # it still counts as one recorded call and appears in analysis' unpriced set
    assert len(ledger.rows()) == 1
    assert "model-not-in-table" in ledger.analysis()["session_reuse"]["unpriced_models"]


def test_ledger_record_prices_known_codex_model(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    row = ledger.record(_result("gpt-5-codex"))
    assert row["priced"] is True
    assert row["cost_usd"] > 0.0


# --- per-task model pin: table-level surface (#84) -----------------------------

def test_fable_row_is_priced_at_the_published_rate() -> None:
    t = DEFAULT_MODEL_TABLE
    usage = TokenUsage(input=1_000_000, output=0)
    assert t.cost_usd("claude-fable-5", usage) == 10.0  # $10/input MTok (2x opus)
    assert t.cost_usd("claude-fable-5", TokenUsage(input=0, output=1_000_000)) == 50.0


def test_fable_is_head_of_the_claude_chain() -> None:
    t = DEFAULT_MODEL_TABLE
    # a rate-limited fable dispatch degrades to opus, then down the existing chain
    assert t.fallback_after("claude-fable-5") == "claude-opus-5"
    assert t.fallback_after("claude-opus-5") == "claude-sonnet-5"


def test_role_defaults_unchanged_by_fable_addition() -> None:
    t = DEFAULT_MODEL_TABLE
    # nothing dispatches chain[0] (fable) by default — role defaults stay opus/sonnet/haiku
    assert t.model_for_role(Role.DEEP_REASON) == "claude-opus-5"
    assert t.model_for_role(Role.REVIEW) == "claude-sonnet-5"
    assert t.model_for_role(Role.CHEAP_SHELL) == "claude-haiku-4-5"


def test_resolve_model_alias_maps_friendly_names() -> None:
    assert resolve_model_alias("fable") == "claude-fable-5"
    assert resolve_model_alias("opus") == "claude-opus-5"
    assert resolve_model_alias("sonnet") == "claude-sonnet-5"
    assert resolve_model_alias("haiku") == "claude-haiku-4-5"
    # exact table ids pass through (incl. codex)
    assert resolve_model_alias("claude-fable-5") == "claude-fable-5"
    assert resolve_model_alias("gpt-5.5") == "gpt-5.5"


def test_resolve_model_alias_unknown_raises_listing_valid_names() -> None:
    with pytest.raises(ValueError, match="unknown model") as ei:
        resolve_model_alias("gpt-9000")
    msg = str(ei.value)
    assert "fable" in msg and "claude-fable-5" in msg and "gpt-5.5" in msg
    # the ENGINE sentinel is never a valid pin target
    assert "engine" not in resolve_model_alias.__doc__  # sanity: doc doesn't advertise it
    with pytest.raises(ValueError):
        resolve_model_alias("engine")


def test_provider_for_model_classifies_both_providers() -> None:
    assert provider_for_model("claude-fable-5") is Provider.CLAUDE
    assert provider_for_model("claude-opus-5") is Provider.CLAUDE
    assert provider_for_model("gpt-5.5") is Provider.CODEX
    with pytest.raises(ValueError):
        provider_for_model("mystery-model")


def test_next_work_routes_codex_stage_to_codex_model(tmp_path: Path, project) -> None:
    # global codex switch: every stage routes to the codex provider (headless)
    eng = Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project,
        router=Router(execution_mode=ExecutionMode.HEADLESS, orchestrator_provider=Provider.CODEX),
    )
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")  # deterministic ENGINE lane — not a model call
    assert intake.lane_policy.execution_mode is ExecutionMode.ENGINE
    eng.record("r1", make_result(intake))
    work = eng.next_work("r1", "t1")  # scope -> first model stage, routed to codex
    assert work.lane_policy.provider is Provider.CODEX
    # the WorkItem model is a codex id (not a claude id shelled to `codex exec -m`)
    assert work.model == "gpt-5.5"
    assert not work.model.startswith("claude")
