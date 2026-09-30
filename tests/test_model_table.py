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
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def test_model_for_role_is_provider_aware() -> None:
    t = DEFAULT_MODEL_TABLE
    # claude is the default provider (existing behavior preserved)
    assert t.model_for_role(Role.DEEP_REASON) == "claude-opus-5-5"
    assert t.model_for_role(Role.DEEP_REASON, Provider.CLAUDE) == "claude-opus-5-5"
    # a codex-routed stage resolves to a codex model id, not a claude one
    codex_deep = t.model_for_role(Role.DEEP_REASON, Provider.CODEX)
    assert codex_deep == "gpt-5.6-sol"
    assert not codex_deep.startswith("claude")
    # on codex the frontier and deep_reason roles share sol: there is no tier above it, so
    # the four roles collapse to three distinct models (terra, luna keep their own tiers)
    assert t.model_for_role(Role.FRONTIER, Provider.CODEX) == "gpt-5.6-sol"
    assert t.model_for_role(Role.FRONTIER, Provider.CODEX) == codex_deep
    assert t.model_for_role(Role.REVIEW, Provider.CODEX) == "gpt-5.6-terra"
    assert t.model_for_role(Role.CHEAP_SHELL, Provider.CODEX) == "gpt-5.6-luna"
    assert len({t.model_for_role(r, Provider.CODEX)
                for r in (Role.FRONTIER, Role.DEEP_REASON, Role.REVIEW, Role.CHEAP_SHELL)}) == 3


def test_claude_roles_map_to_four_distinct_models() -> None:
    t = DEFAULT_MODEL_TABLE
    by_role = {
        Role.FRONTIER: "claude-fable-5-1",
        Role.DEEP_REASON: "claude-opus-5-5",
        Role.REVIEW: "claude-sonnet-5-5",
        Role.CHEAP_SHELL: "claude-haiku-4-5",
    }
    for role, model in by_role.items():
        assert t.model_for_role(role, Provider.CLAUDE) == model
    assert len(set(by_role.values())) == 4


def test_stage_specs_assign_the_claude_tiers() -> None:
    """The stage -> role assignment, resolved through the table: scope/implement and review are
    all deep_reason (fable stays a per-task pin), deliver is prose and
    mechanics (review tier), and simplify/test are cheap_shell."""
    t = DEFAULT_MODEL_TABLE
    expected = {
        Stage.SCOPE: Role.DEEP_REASON,
        Stage.IMPLEMENT: Role.DEEP_REASON,
        Stage.REVIEW: Role.DEEP_REASON,
        Stage.DELIVER: Role.REVIEW,
        Stage.SIMPLIFY: Role.CHEAP_SHELL,
        Stage.TEST: Role.CHEAP_SHELL,
    }
    for stage, role in expected.items():
        assert STAGE_SPECS[stage].model_role == role, stage
    assert t.model_for_role(STAGE_SPECS[Stage.SCOPE].model_role) == "claude-opus-5-5"
    assert t.model_for_role(STAGE_SPECS[Stage.IMPLEMENT].model_role) == "claude-opus-5-5"
    assert t.model_for_role(STAGE_SPECS[Stage.REVIEW].model_role) == "claude-opus-5-5"
    assert t.model_for_role(STAGE_SPECS[Stage.DELIVER].model_role) == "claude-sonnet-5-5"
    assert t.model_for_role(STAGE_SPECS[Stage.SIMPLIFY].model_role) == "claude-haiku-4-5"
    assert t.model_for_role(STAGE_SPECS[Stage.TEST].model_role) == "claude-haiku-4-5"


def test_codex_models_are_priced_from_their_own_row() -> None:
    t = DEFAULT_MODEL_TABLE
    usage = TokenUsage(input=1_000_000, output=0)
    # codex model priced from the codex row, not a claude fallback
    assert t.cost_usd("gpt-5-codex", usage) == 1.25
    # sanity: different from the claude deep-reason model's input price
    assert t.cost_usd("claude-opus-5-5", usage) == 4.0


def test_fallback_stays_within_provider_chain() -> None:
    t = DEFAULT_MODEL_TABLE
    assert t.fallback_after("claude-opus-5-5") == "claude-sonnet-5-5"
    # codex degrades down its OWN generation: sol -> terra -> luna, and luna is the floor
    assert t.fallback_after("gpt-5.6-sol") == "gpt-5.6-terra"
    assert t.fallback_after("gpt-5.6-terra") == "gpt-5.6-luna"
    assert t.fallback_after("gpt-5.6-luna") is None
    # priceable-but-off-chain ids never degrade sideways into another generation
    assert t.fallback_after("gpt-5.5") is None
    assert t.fallback_after("nonexistent") is None


def test_try_cost_usd_tolerates_unknown_model() -> None:
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("some-future-model", TokenUsage(input=100))
    assert cost == 0.0 and priced is False
    cost, priced = DEFAULT_MODEL_TABLE.try_cost_usd("claude-opus-5-5", TokenUsage(input=100))
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

def test_current_claude_rows_are_priced_at_the_published_rates() -> None:
    t = DEFAULT_MODEL_TABLE
    inp = TokenUsage(input=1_000_000, output=0)
    out = TokenUsage(input=0, output=1_000_000)
    for model, (i, o) in {
        "claude-fable-5-1": (10.0, 50.0),
        "claude-opus-5-5": (4.0, 20.0),
        "claude-sonnet-5-5": (2.0, 10.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }.items():
        assert t.cost_usd(model, inp) == i, model
        assert t.cost_usd(model, out) == o, model


def test_cache_read_multipliers_differ_per_model() -> None:
    t = DEFAULT_MODEL_TABLE
    reads = TokenUsage(cache_read=1_000_000)
    assert t.cost_usd("claude-fable-5-1", reads) == pytest.approx(10.0 * 0.025)  # $0.25
    assert t.cost_usd("claude-opus-5-5", reads) == pytest.approx(4.0 * 0.05)  # $0.20
    assert t.cost_usd("claude-sonnet-5-5", reads) == pytest.approx(2.0 * 0.10)  # default 0.1x
    assert t.cost_usd("claude-haiku-4-5", reads) == pytest.approx(1.0 * 0.10)


def test_superseded_claude_ids_still_price_for_history() -> None:
    """Old ledger rows name the pre-5.x ids; removing them would make every prior row
    unpriced. They stay priceable (at their own rates) though nothing dispatches them."""
    t = DEFAULT_MODEL_TABLE
    inp = TokenUsage(input=1_000_000, output=0)
    out = TokenUsage(input=0, output=1_000_000)
    for model, (i, o) in {
        "claude-fable-5": (10.0, 50.0),
        "claude-opus-5": (5.0, 25.0),
        "claude-sonnet-5": (2.0, 10.0),  # its intro rate became the standard price
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-4-6": (3.0, 15.0),
    }.items():
        cost, priced = t.try_cost_usd(model, inp)
        assert priced is True and cost == i, model
        assert t.cost_usd(model, out) == o, model
    # ...and none of them is in the dispatch chain
    chain = {m for m in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5")
             if t.fallback_after(m) is not None}
    assert chain == set()


def test_fable_is_head_of_the_claude_chain() -> None:
    t = DEFAULT_MODEL_TABLE
    # a rate-limited fable dispatch degrades to opus, then down the chain to the haiku floor
    assert t.fallback_after("claude-fable-5-1") == "claude-opus-5-5"
    assert t.fallback_after("claude-opus-5-5") == "claude-sonnet-5-5"
    assert t.fallback_after("claude-sonnet-5-5") == "claude-haiku-4-5"
    assert t.fallback_after("claude-haiku-4-5") is None


def test_resolve_model_alias_maps_friendly_names() -> None:
    assert resolve_model_alias("fable") == "claude-fable-5-1"
    assert resolve_model_alias("opus") == "claude-opus-5-5"
    assert resolve_model_alias("sonnet") == "claude-sonnet-5-5"
    assert resolve_model_alias("haiku") == "claude-haiku-4-5"
    # exact table ids pass through (incl. superseded claude ids and codex)
    assert resolve_model_alias("claude-fable-5-1") == "claude-fable-5-1"
    assert resolve_model_alias("claude-fable-5") == "claude-fable-5"
    assert resolve_model_alias("gpt-5.5") == "gpt-5.5"


def test_resolve_model_alias_unknown_raises_listing_valid_names() -> None:
    with pytest.raises(ValueError, match="unknown model") as ei:
        resolve_model_alias("gpt-9000")
    msg = str(ei.value)
    assert "fable" in msg and "claude-fable-5-1" in msg and "gpt-5.5" in msg
    # the ENGINE sentinel is never a valid pin target
    assert "engine" not in resolve_model_alias.__doc__  # sanity: doc doesn't advertise it
    with pytest.raises(ValueError):
        resolve_model_alias("engine")


def test_provider_for_model_classifies_both_providers() -> None:
    assert provider_for_model("claude-fable-5-1") is Provider.CLAUDE
    assert provider_for_model("claude-opus-5-5") is Provider.CLAUDE
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
    assert work.model == "gpt-5.6-sol"
    assert not work.model.startswith("claude")
