"""#557: a headless claude stage is priced from the CLI's per-model session report.

Before this fix the ledger priced each stage from the FINAL result event's ``usage``, and
``stage-costs.jsonl`` under-recorded real spend by about 3-5x. The captured streams of
``paper-kpaper-c`` (prediction, 2026-10-12; the fixture beside this file) show three
separate causes, and each one is pinned here:

* A stage's stream can hold several ``type=="result"`` events (the CLI's structured-output
  re-prompt, background-task follow-ups, a resumed session's zero-turn echo). Each one's
  ``usage`` covers only its own turns, so reading the last one dropped most of the stage.
* ``modelUsage``/``total_cost_usd`` are complete, but CUMULATIVE over the provider session,
  which a task's stages share via ``--resume``. #309 deliver (routed to sonnet) reports
  opus and haiku too, but that is IMPLEMENT's opus and TEST's haiku carried forward. The
  stage's own spend is the delta, which is sonnet alone.
* 1h cache writes bill at 2.0x input, not the 5m 1.25x, and sonnet 5.5 cache reads at
  0.05x. With both, the engine reproduces the CLI's ``costUSD`` to the cent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapters.execution.transport as T
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.transport import RawResult, claude_cli_transport
from orchestrator.cost_ledger import ACCOUNTING_VERSION, CostLedger, cost_mismatch
from orchestrator.engine import Engine
from orchestrator.model_table import DEFAULT_MODEL_TABLE
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, StageResult, TokenUsage, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_session_cumulative_557.json").read_text()
)
_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"


def _events(task: str, stage: str) -> list[dict]:
    return _FIXTURE[task][stage]


def _wi(stage: Stage, model: str, wid: str) -> WorkItem:
    return WorkItem.create(
        id=wid, run_id="r1", task_id="#309", stage=stage, prompt="p",
        schema_ref=stage.value, model=model, lane_policy=_HEADLESS,
        created_at="2026-10-12T00:00:00Z", timeout_s=60,
    )


def _dispatch(tmp_path: Path, monkeypatch, events: list[dict], work: WorkItem) -> StageResult:
    """Run the REAL claude transport over a captured stream, then the runner."""
    stdout = "".join(json.dumps(e) + "\n" for e in events)

    def fake(argv, *, timeout, cwd, tee_path, env=None, **_kw):
        tee_path.parent.mkdir(parents=True, exist_ok=True)
        tee_path.write_text(stdout, encoding="utf-8")
        return 0, stdout, ""

    monkeypatch.setattr(T, "_run_teed", fake)
    raw = claude_cli_transport(run_log_root=tmp_path)(work)
    return HeadlessClaudeRunner(transport=lambda w: raw).dispatch(work)


def _rows(ledger: CostLedger, wid: str) -> list[dict]:
    return [r for r in ledger.rows() if r["work_item_id"] == wid]


# --- pricing -----------------------------------------------------------------------------

def test_one_hour_cache_writes_bill_at_twice_input() -> None:
    t = DEFAULT_MODEL_TABLE
    assert t.cost_usd(OPUS, TokenUsage(cache_write=1_000_000)) == pytest.approx(4.0 * 1.25)
    assert t.cost_usd(OPUS, TokenUsage(cache_write_1h=1_000_000)) == pytest.approx(4.0 * 2.0)


@pytest.mark.parametrize(("model", "task", "stage"), [
    (SONNET, "309", "deliver"), (OPUS, "309", "implement"), (HAIKU, "309", "test"),
    (OPUS, "310", "implement"), (HAIKU, "310", "test"),
])
def test_table_reproduces_the_cli_cost_for_each_model(model: str, task: str, stage: str) -> None:
    # The CLI's own per-model costUSD is the ground truth the price table must match.
    final = _events(task, stage)[-1]
    _, model_usage, _ = T._claude_usage_report(_events(task, stage))
    assert model_usage is not None
    assert DEFAULT_MODEL_TABLE.cost_usd(model, model_usage[model]) == pytest.approx(
        final["modelUsage"][model]["costUSD"], abs=1e-5
    )


# --- transport parse ---------------------------------------------------------------------

def test_usage_sums_every_result_event_in_the_stream() -> None:
    # #310 test-attempt0: a zero-turn resume echo, then FIVE result events. The old reader
    # kept only the last (573 output tokens); the stage really produced 11071.
    events = _events("310", "test")
    usage, _, _ = T._claude_usage_report(events)
    assert T._usage_from(events[-1]).output == 573
    assert usage.output == 7169 + 1699 + 990 + 640 + 573
    # ...which is exactly the haiku the session gained during this stage.
    before = _events("310", "implement")[-1]["modelUsage"]
    assert HAIKU not in before
    assert usage.output == events[-1]["modelUsage"][HAIKU]["outputTokens"]


def test_cache_creation_split_reads_the_one_hour_share() -> None:
    usage = T._usage_from({"usage": {
        "input_tokens": 1, "cache_creation_input_tokens": 100,
        "cache_creation": {"ephemeral_1h_input_tokens": 70, "ephemeral_5m_input_tokens": 30},
    }})
    assert (usage.cache_write, usage.cache_write_1h) == (30, 70)
    # No split reported (older CLIs, codex): the legacy all-5m reading.
    legacy = T._usage_from({"usage": {"cache_creation_input_tokens": 100}})
    assert (legacy.cache_write, legacy.cache_write_1h) == (100, 0)


def test_model_usage_and_session_cost_come_from_the_final_event() -> None:
    _, model_usage, session_cost = T._claude_usage_report(_events("309", "deliver"))
    assert model_usage is not None
    assert set(model_usage) == {OPUS, HAIKU, SONNET}
    assert session_cost == pytest.approx(1.9058181)


def test_without_model_usage_the_report_falls_back_to_summed_usage() -> None:
    events = [{k: v for k, v in e.items() if k != "modelUsage"} for e in _events("310", "test")]
    usage, model_usage, _ = T._claude_usage_report(events)
    assert model_usage is None
    assert usage.output == 11071


def test_schema_retry_adds_claude_usage_but_replaces_codex_usage() -> None:
    schema = json.dumps({"type": "object", "required": ["x"]})
    first = RawResult({}, usage=TokenUsage(output=10), session_ref="s")
    second = RawResult({"x": 1}, usage=TokenUsage(output=5), session_ref="s")
    work = _wi(Stage.IMPLEMENT, OPUS, "wi-r")

    added = T._schema_retry_loop(first, work, schema, 2, lambda *_: second, additive_usage=True)
    replaced = T._schema_retry_loop(first, work, schema, 2, lambda *_: second)
    assert added.usage.output == 15
    assert replaced.usage.output == 5  # codex: session-cumulative, de-cumulated later


# --- ledger: per-model session deltas ------------------------------------------------------

def test_a_resumed_session_charges_each_stage_only_its_own_spend(tmp_path, monkeypatch) -> None:
    # The #309 chain: implement (opus) -> test (haiku) -> deliver (sonnet), one session.
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    chain = [
        ("implement", Stage.IMPLEMENT, OPUS, 1.2218068),
        ("test", Stage.TEST, HAIKU, 1.4088199 - 1.2218068),
        ("deliver", Stage.DELIVER, SONNET, 1.9058181 - 1.4088199),
    ]
    for name, stage, model, cli_cost in chain:
        result = _dispatch(tmp_path, monkeypatch, _events("309", name),
                           _wi(stage, model, f"wi-{name}"))
        view = ledger.record(result, duration_s=1.0)
        rows = _rows(ledger, f"wi-{name}")

        # One row: the stage ran only its routed model. Before #557, deliver was $0.38
        # (last event, 1h writes at 5m) and copying modelUsage whole would have been $1.91.
        assert [r["model"] for r in rows] == [model]
        assert "phase" not in rows[0]
        assert view["cost_usd"] == pytest.approx(cli_cost, abs=1e-5)
        assert view["provider_cost_usd"] == pytest.approx(cli_cost, abs=1e-5)
        assert view["accounting_version"] == ACCOUNTING_VERSION
        assert cost_mismatch(view) is None

    assert ledger.metered_spend() == pytest.approx(1.9058181, abs=1e-5)


def test_multi_result_stage_is_priced_whole(tmp_path, monkeypatch) -> None:
    # #310: implement (opus), then a test stage whose stream holds six result events.
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    impl = _dispatch(tmp_path, monkeypatch, _events("310", "implement"),
                     _wi(Stage.IMPLEMENT, OPUS, "wi-i"))
    assert ledger.record(impl)["cost_usd"] == pytest.approx(2.5284998, abs=1e-5)
    test = _dispatch(tmp_path, monkeypatch, _events("310", "test"),
                     _wi(Stage.TEST, HAIKU, "wi-t"))
    view = ledger.record(test)
    assert view["model"] == HAIKU
    assert view["cost_usd"] == pytest.approx(3.119343 - 2.5284998, abs=1e-5)  # was ~$0.03


def _result(wid: str, model: str, model_usage: dict[str, TokenUsage], *,
            session: str | None = "s1", session_cost: float | None = None) -> StageResult:
    return make_result(_wi(Stage.DELIVER, model, wid), session_ref=session).model_copy(
        update={"model_usage": model_usage, "provider_session_cost_usd": session_cost}
    )


def test_a_stage_that_ran_two_models_writes_a_row_per_model(tmp_path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    opus, haiku = TokenUsage(output=1_000_000), TokenUsage(output=1_000_000)
    view = ledger.record(_result("wi-2", SONNET, {SONNET: TokenUsage(), OPUS: opus,
                                                  HAIKU: haiku}), duration_s=3.0)
    rows = _rows(ledger, "wi-2")

    # The routed row is always first, phase-less and owns the wall time, even at $0.
    assert [r["model"] for r in rows] == [SONNET, HAIKU, OPUS]
    assert rows[0]["cost_usd"] == 0.0 and rows[0]["duration_s"] == 3.0
    assert "secondary_model" not in rows[0]
    assert all(r["secondary_model"] is True and r["duration_s"] is None for r in rows[1:])
    assert all("phase" not in r for r in rows)
    assert view["cost_usd"] == pytest.approx(20.0 + 5.0)
    assert view["sub_calls"] == 3
    assert view["by_model"] == {SONNET: 0.0, HAIKU: pytest.approx(5.0), OPUS: pytest.approx(20.0)}


def test_replay_converges_on_per_model_rows(tmp_path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = _result("wi-r", OPUS, {OPUS: TokenUsage(output=1000), HAIKU: TokenUsage(output=1000)},
                     session_cost=0.025)
    first = ledger.record(result)
    again = ledger.record(result)  # a crash-replay of the same dispatch (#277)
    assert len(ledger.rows()) == 2
    assert again["cost_usd"] == first["cost_usd"]
    assert again["provider_cost_usd"] == first["provider_cost_usd"] == pytest.approx(0.025)


def test_session_rows_from_before_557_leave_provider_cost_unknown(tmp_path) -> None:
    # A session whose earlier stage was recorded by v2 code (no cumulative figure): the
    # token delta still self-heals, but there is no baseline for the provider delta, so the
    # cross-check stays quiet instead of crying wolf.
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(make_result(_wi(Stage.IMPLEMENT, OPUS, "wi-old"), session_ref="s1",
                              tokens=TokenUsage(output=100)))
    view = ledger.record(_result("wi-new", OPUS, {OPUS: TokenUsage(output=1100)},
                                 session_cost=9.0))
    assert view["output_tokens"] == 1000
    assert view["provider_cost_usd"] is None
    assert cost_mismatch(view) is None


def test_a_reset_session_counter_is_taken_whole(tmp_path) -> None:
    # A cumulative cost LOWER than the last one recorded means the CLI did not carry the
    # session's counters across the resume; the report is this call's own.
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(_result("wi-a", OPUS, {OPUS: TokenUsage(output=100_000)}, session_cost=2.0))
    view = ledger.record(_result("wi-b", OPUS, {OPUS: TokenUsage(output=50_000)},
                                 session_cost=1.0))
    assert view["output_tokens"] == 50_000
    assert view["provider_cost_usd"] == pytest.approx(1.0)


def test_codex_session_de_cumulation_is_unchanged(tmp_path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    codex = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    for wid, out in (("wi-c1", 100), ("wi-c2", 250)):
        work = WorkItem.create(
            id=wid, run_id="r1", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
            schema_ref="implement", model="gpt-5.6-sol", lane_policy=codex,
            created_at="2026-10-12T00:00:00Z", timeout_s=60,
        )
        ledger.record(make_result(work, session_ref="cx", tokens=TokenUsage(output=out)))
    assert [r["output_tokens"] for r in ledger.rows()] == [100, 150]
    assert all("provider_cost_usd" not in r for r in ledger.rows())


# --- the engine-vs-provider cross-check ----------------------------------------------------

def test_cost_mismatch_tolerance() -> None:
    assert cost_mismatch({"cost_usd": 1.00, "provider_cost_usd": 1.04}) is None  # < 5%
    assert cost_mismatch({"cost_usd": 0.01, "provider_cost_usd": 0.025}) is None  # < $0.02
    assert cost_mismatch({"cost_usd": 0.38, "provider_cost_usd": None}) is None
    assert cost_mismatch({"cost_usd": 0.0, "provider_cost_usd": 1.0, "priced": False}) is None
    gap = cost_mismatch({"model": "m", "cost_usd": 0.38, "provider_cost_usd": 0.50})
    assert gap == {"engine_cost_usd": 0.38, "provider_cost_usd": 0.5, "delta_usd": -0.12,
                   "by_model": {"m": 0.38}}
    # An aggregate view over several models passes its own breakdown through.
    split = {"a": 0.30, "b": 0.08}
    gap = cost_mismatch({"model": "a", "cost_usd": 0.38, "provider_cost_usd": 0.50,
                         "by_model": split})
    assert gap is not None and gap["by_model"] == split


def _engine_scope(tmp_path) -> tuple[Engine, WorkItem]:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject())
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake, deterministic
    scope = eng.next_work("r1", "t1")
    assert scope is not None and scope.stage is Stage.SCOPE
    return eng, scope


@pytest.mark.parametrize(("provider_cost", "fires"), [(2.0, True), (1.0, False)])
def test_engine_emits_a_warning_when_its_price_disagrees(tmp_path, provider_cost, fires) -> None:
    eng, scope = _engine_scope(tmp_path)
    # 100k fresh input on the scope model prices at a known figure; the provider says
    # either the same or double.
    usage = TokenUsage(input=100_000)
    price = DEFAULT_MODEL_TABLE.cost_usd(scope.model, usage)
    result = make_result(scope, session_ref="s1").model_copy(update={
        "model_usage": {scope.model: usage},
        "provider_session_cost_usd": price * provider_cost,
    })
    eng.record("r1", result)

    gaps = [e for e in eng.store.read_events("r1") if e["type"] == "stage_cost_mismatch"]
    if not fires:
        assert gaps == []
        return
    (gap,) = gaps
    assert gap["level"] == "warning"
    assert gap["stage"] == "scope" and gap["work_item_id"] == scope.id
    assert gap["engine_cost_usd"] == pytest.approx(price)
    assert gap["provider_cost_usd"] == pytest.approx(price * 2)
    assert gap["by_model"] == {scope.model: pytest.approx(price)}


def test_raw_result_fields_reach_the_stage_result(tmp_path, monkeypatch) -> None:
    result = _dispatch(tmp_path, monkeypatch, _events("309", "deliver"),
                       _wi(Stage.DELIVER, SONNET, "wi-d"))
    assert result.model_usage is not None and set(result.model_usage) == {OPUS, HAIKU, SONNET}
    assert result.provider_session_cost_usd == pytest.approx(1.9058181)
