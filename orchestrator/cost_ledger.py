"""Append-only cost ledger (target.md §6.4, closes as-built D6).

EVERY model call gets exactly one row. ``record`` is the only entry point and it
always writes, so there is no code path that runs a model without a ledger row —
the as-built bug was that the one-shot path bypassed ``record_stage_invocation``.

Pricing is authoritative: the cost written is computed from the engine's single
``model_table`` (the same table used everywhere), NOT from the runner-supplied
``StageResult.cost_usd``. A runner cannot under- or over-report spend; the
engine's table is the single source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model_table import DEFAULT_MODEL_TABLE, ModelTable
from .schemas.work import StageResult


class CostLedger:
    """A ``stage-costs.jsonl`` file: one row per model invocation."""

    def __init__(self, path: Path, model_table: ModelTable = DEFAULT_MODEL_TABLE) -> None:
        self.path = Path(path)
        self.model_table = model_table

    def record(self, result: StageResult) -> dict:
        """Append exactly one JSONL row for this invocation and return it.

        Cost is recomputed from ``model_table`` (authoritative) — the runner's
        ``result.cost_usd`` is deliberately ignored.
        """
        usage = result.token_usage
        row = {
            "ts": result.completed_at,
            "run_id": result.run_id,
            "task_id": result.task_id,
            "stage": result.stage.value,
            "attempt": result.attempt,
            "model": result.model,
            "provider": result.lane_used.provider.value,
            "lane": result.lane_used.execution_mode.value,
            "input_tokens": usage.input,
            "output_tokens": usage.output,
            "cache_read_tokens": usage.cache_read,
            "cache_write_tokens": usage.cache_write,
            "cost_usd": self.model_table.cost_usd(result.model, usage),
            "status": result.status.value,
            "work_item_id": result.work_item_id,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def rows(self) -> list[dict]:
        """Read back every recorded row."""
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def summary(self, rows: list[dict] | None = None) -> dict:
        """Aggregate the ledger: totals plus a per-model breakdown.

        Accepts pre-read ``rows`` so a caller (engine.status) reads the JSONL once
        and shares it. Tolerant of a malformed/partial row via ``.get`` defaults."""
        by_model: dict[str, dict] = {}
        total_cost = 0.0
        total_invocations = 0
        for row in (self.rows() if rows is None else rows):
            total_invocations += 1
            cost = row.get("cost_usd") or 0.0
            total_cost += cost
            bucket = by_model.setdefault(
                row.get("model", "unknown"),
                {
                    "invocations": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            bucket["invocations"] += 1
            bucket["input_tokens"] += row.get("input_tokens", 0) or 0
            bucket["output_tokens"] += row.get("output_tokens", 0) or 0
            bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)
        return {
            "total_cost_usd": round(total_cost, 6),
            "total_invocations": total_invocations,
            "by_model": by_model,
        }

    def total_attributed(self) -> float:
        """Convenience: total cost attributed across the whole ledger."""
        return self.summary()["total_cost_usd"]
