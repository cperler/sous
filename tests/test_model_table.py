"""Provider-aware model table (review Phase A #2, roadmap E1).

`model_for_role` resolves to the routed provider's model id (a codex stage no longer
gets a claude id shelled to `codex exec -m`), and `ledger.record` tolerates an unknown
model id (flag, don't raise) the way `analysis()` already does.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.model_table import DEFAULT_MODEL_TABLE, Role
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage
from orchestrator.status_store import StatusStore


def test_model_for_role_is_provider_aware() -> None:
    t = DEFAULT_MODEL_TABLE
    # claude is the default provider (existing behavior preserved)
    assert t.model_for_role(Role.DEEP_REASON) == "claude-opus-4-8"
    assert t.model_for_role(Role.DEEP_REASON, Provider.CLAUDE) == "claude-opus-4-8"
    # a codex-routed stage resolves to a codex model id, not a claude one
    codex_deep = t.model_for_role(Role.DEEP_REASON, Provider.CODEX)
    assert codex_deep == "gpt-5-codex"
    assert not codex_deep.startswith("claude")
    assert t.model_for_role(Role.CHEAP_SHELL, Provider.CODEX) == "gpt-5-mini"


def test_codex_models_are_priced_from_their_own_row() -> None:
    t = DEFAULT_MODEL_TABLE
    usage = TokenUsage(input=1_000_000, output=0)
    # codex model priced from the codex row, not a claude fallback
    assert t.cost_usd("gpt-5-codex", usage) == 1.25
    # sanity: different from the claude deep-reason model's input price
    assert t.cost_usd("claude-opus-4-8", usage) == 5.0


def test_fallback_stays_within_provider_chain() -> None:
    t = DEFAULT_MODEL_TABLE
    assert t.fallback_after("claude-opus-4-8") == "claude-sonnet-4-6"
    assert t.fallback_after("gpt-5-codex") == "gpt-5"
    assert t.fallback_after("gpt-5-mini") is None  # floor of the codex chain
    assert t.fallback_after("nonexistent") is None


def test_try_cost_usd_tolerates_unknown_model() -> None:
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("some-future-model", TokenUsage(input=100))
    assert cost == 0.0 and priced is False
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("claude-opus-4-8", TokenUsage(input=100))
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


def test_next_work_routes_codex_stage_to_codex_model(tmp_path: Path, project) -> None:
    # global codex switch: every stage routes to the codex provider (headless)
    eng = Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project,
        router=Router(execution_mode=ExecutionMode.HEADLESS, orchestrator_provider=Provider.CODEX),
    )
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")  # intake stage -> cheap_shell role
    assert work.lane_policy.provider is Provider.CODEX
    # the WorkItem model is a codex id (not a claude id shelled to `codex exec -m`)
    assert work.model == "gpt-5-mini"
    assert not work.model.startswith("claude")
