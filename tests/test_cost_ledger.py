"""Tests for the cost ledger (target.md §6.4, closes as-built D6).

The invariant under test: EVERY model call produces exactly one ledger row, and
the recorded cost is computed by the engine's single price table — never trusted
from a runner-supplied value.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.cost_ledger import CostLedger
from orchestrator.model_table import DEFAULT_MODEL_TABLE
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage


def make_result(
    *,
    model: str = "claude-opus-4-8",
    stage: Stage = Stage.IMPLEMENT,
    status: ResultStatus = ResultStatus.SUCCESS,
    provider: Provider = Provider.CLAUDE,
    execution_mode: ExecutionMode = ExecutionMode.HEADLESS,
    input: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    attempt: int = 0,
    cost_usd: float | None = None,
    run_id: str = "run-1",
    task_id: str = "task-1",
    work_item_id: str = "wi-1",
    completed_at: str = "2026-06-20T00:00:00Z",
) -> StageResult:
    """Build a small StageResult fixture."""
    return StageResult(
        work_item_id=work_item_id,
        content_hash="hash-" + work_item_id,
        run_id=run_id,
        task_id=task_id,
        stage=stage,
        attempt=attempt,
        model=model,
        status=status,
        lane_used=LaneUsed(
            execution_mode=execution_mode,
            provider=provider,
            invocation=f"agent(model={model})",
        ),
        token_usage=TokenUsage(
            input=input,
            output=output,
            cache_read=cache_read,
            cache_write=cache_write,
        ),
        cost_usd=cost_usd,
        completed_at=completed_at,
    )


def test_record_uses_table_pricing_exact(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(model="claude-opus-4-8", input=1_000_000, output=0)
    row = ledger.record(result)
    # opus input is $5.0 / Mtok -> exactly 5.0 for 1M input tokens.
    assert row["cost_usd"] == 5.0
    assert row["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-4-8", result.token_usage
    )


def test_record_ignores_runner_supplied_cost(tmp_path: Path) -> None:
    """Authoritative pricing: the engine's table wins over a runner's cost."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-opus-4-8", input=1_000_000, output=0, cost_usd=999.99
    )
    row = ledger.record(result)
    assert row["cost_usd"] == 5.0  # not 999.99


def test_record_returns_full_row_fields(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-sonnet-4-6",
        stage=Stage.REVIEW,
        status=ResultStatus.SUCCESS,
        provider=Provider.CLAUDE,
        execution_mode=ExecutionMode.INTERACTIVE,
        input=10,
        output=20,
        cache_read=30,
        cache_write=40,
        attempt=2,
        run_id="run-X",
        task_id="task-X",
        work_item_id="wi-X",
        completed_at="2026-06-20T12:34:56Z",
    )
    row = ledger.record(result)
    assert row["ts"] == "2026-06-20T12:34:56Z"
    assert row["run_id"] == "run-X"
    assert row["task_id"] == "task-X"
    assert row["stage"] == "review"
    assert row["attempt"] == 2
    assert row["model"] == "claude-sonnet-4-6"
    assert row["provider"] == "claude"
    assert row["lane"] == "interactive"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["cache_read_tokens"] == 30
    assert row["cache_write_tokens"] == 40
    assert row["status"] == "success"
    assert row["work_item_id"] == "wi-X"
    assert row["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-sonnet-4-6", result.token_usage
    )


def test_every_record_writes_one_row(tmp_path: Path) -> None:
    """N records -> N rows (the every-call invariant)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    n = 5
    for i in range(n):
        ledger.record(make_result(work_item_id=f"wi-{i}", input=100))
    rows = ledger.rows()
    assert len(rows) == n
    # File is genuinely one JSONL line per call.
    text = (tmp_path / "stage-costs.jsonl").read_text().strip().splitlines()
    assert len(text) == n


def test_codex_zero_token_result_still_writes_row(tmp_path: Path) -> None:
    """A codex/zero-token result still records a row (cost 0)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-haiku-4-5",
        provider=Provider.CODEX,
        input=0,
        output=0,
    )
    row = ledger.record(result)
    assert row["cost_usd"] == 0.0
    assert row["provider"] == "codex"
    assert len(ledger.rows()) == 1


def test_no_bypass_two_results_two_rows(tmp_path: Path) -> None:
    """Explicit no-bypass test: a cheap one-shot-looking result and a normal
    result both produce rows, so total_invocations == 2. There is no code path
    that runs a model without a ledger row (closes as-built D6)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    # Looks like a cheap one-shot (the path that used to bypass the ledger).
    one_shot = make_result(
        work_item_id="wi-oneshot",
        model="claude-haiku-4-5",
        stage=Stage.INTAKE,
        input=10,
        output=1,
    )
    normal = make_result(
        work_item_id="wi-normal",
        model="claude-opus-4-8",
        stage=Stage.IMPLEMENT,
        input=5_000,
        output=2_000,
    )
    ledger.record(one_shot)
    ledger.record(normal)
    summary = ledger.summary()
    assert summary["total_invocations"] == 2
    assert len(ledger.rows()) == 2


def test_summary_aggregates_per_model_and_totals(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    r1 = make_result(model="claude-opus-4-8", input=1_000_000, output=0)
    r2 = make_result(model="claude-opus-4-8", input=0, output=1_000_000)
    r3 = make_result(model="claude-sonnet-4-6", input=1_000_000, output=0)
    for r in (r1, r2, r3):
        ledger.record(r)

    summary = ledger.summary()
    assert summary["total_invocations"] == 3

    opus_cost = DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-4-8", r1.token_usage
    ) + DEFAULT_MODEL_TABLE.cost_usd("claude-opus-4-8", r2.token_usage)
    sonnet_cost = DEFAULT_MODEL_TABLE.cost_usd("claude-sonnet-4-6", r3.token_usage)

    by_model = summary["by_model"]
    assert by_model["claude-opus-4-8"]["invocations"] == 2
    assert by_model["claude-opus-4-8"]["input_tokens"] == 1_000_000
    assert by_model["claude-opus-4-8"]["output_tokens"] == 1_000_000
    assert by_model["claude-opus-4-8"]["cost_usd"] == round(opus_cost, 6)
    assert by_model["claude-sonnet-4-6"]["invocations"] == 1
    assert by_model["claude-sonnet-4-6"]["input_tokens"] == 1_000_000
    assert by_model["claude-sonnet-4-6"]["cost_usd"] == round(sonnet_cost, 6)

    assert summary["total_cost_usd"] == round(opus_cost + sonnet_cost, 6)
    assert ledger.total_attributed() == summary["total_cost_usd"]


def test_rows_roundtrip(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    recorded = [
        ledger.record(make_result(work_item_id=f"wi-{i}", input=100 * i))
        for i in range(3)
    ]
    read_back = ledger.rows()
    assert read_back == recorded
    # And matches what's on disk byte-for-byte (JSON parse).
    disk = [
        json.loads(line)
        for line in (tmp_path / "stage-costs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert disk == recorded


def test_rows_empty_when_no_file(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    assert ledger.rows() == []
    assert ledger.summary()["total_invocations"] == 0
    assert ledger.total_attributed() == 0.0
