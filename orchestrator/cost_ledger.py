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

    def record(self, result: StageResult, *, duration_s: float | None = None) -> dict:
        """Append exactly one JSONL row for this invocation and return it.

        Cost is recomputed from ``model_table`` (authoritative) — the runner's
        ``result.cost_usd`` is deliberately ignored. ``duration_s`` is the engine-
        measured wall time of the dispatch (dispatch->record).
        """
        usage = result.token_usage
        # Tolerant pricing: an unknown model id (e.g. a new provider model not yet in the
        # table) must NOT raise — every call still gets exactly one row. An unpriced call
        # is flagged (priced=False) and costed at 0.0, the same tolerance analysis() has.
        cost, priced = self.model_table.try_cost_usd(result.model, usage)
        # HONESTY flag: the interactive lane cannot meter per-call usage in-session, so
        # its zero-token rows are UNMETERED (cost unknown), not free. Metered lanes and
        # the deterministic engine lane (genuinely $0) stay metered=True. Renderers use
        # this to say "n/a / unmetered" instead of a confident $0.0000.
        tokens_seen = usage.input + usage.output + usage.cache_read + usage.cache_write
        metered = not (
            result.lane_used.execution_mode.value == "interactive" and tokens_seen == 0
        )
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
            "cost_usd": cost,
            "priced": priced,
            "metered": metered,
            "duration_s": round(duration_s, 3) if duration_s is not None else None,
            "status": result.status.value,
            "work_item_id": result.work_item_id,
            # Corrective schema-retries the transport spent salvaging this call's output (#32).
            # Almost always 0; a non-zero value flags an invocation that cost extra model turns.
            "schema_retries": result.schema_retries,
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
        unmetered_calls = 0
        total_wall_s = 0.0
        for row in (self.rows() if rows is None else rows):
            total_invocations += 1
            cost = row.get("cost_usd") or 0.0
            total_cost += cost
            if row.get("metered") is False:
                unmetered_calls += 1
            total_wall_s += row.get("duration_s") or 0.0
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
            "unmetered_calls": unmetered_calls,
            "total_wall_s": round(total_wall_s, 1),
            "by_model": by_model,
        }

    def total_attributed(self) -> float:
        """Convenience: total cost attributed across the whole ledger."""
        return self.summary()["total_cost_usd"]

    def metered_spend(self, rows: list[dict] | None = None) -> float:
        """USD spent on METERED rows only — the honest figure a budget gate checks (#34).

        Unmetered interactive rows record $0 (they carry no per-call usage), so they add
        nothing anyway; excluding them explicitly keeps the budget semantics honest — a
        run billed to a subscription can't accidentally count as spend. Accepts pre-read
        ``rows`` so a caller (status) reads the JSONL once and shares it."""
        total = 0.0
        for row in (self.rows() if rows is None else rows):
            if row.get("metered") is False:
                continue
            total += row.get("cost_usd") or 0.0
        return round(total, 6)

    def analysis(self, rows: list[dict] | None = None) -> dict:
        """Rich cost report: per-stage + per-task breakdowns and the session-reuse win.

        The rebuild's thesis is that chaining the collapsed stages in ONE session
        reuses the prompt cache, so most input-side tokens come back as cheap
        ``cache_read`` (billed at ``cache_read_mult`` of input) instead of fresh input.
        This quantifies that: the win is what those reads saved vs. an uncached
        counterfactual, net of the ``cache_write`` premium paid to establish the cache.

        Accepts pre-read ``rows`` so a caller (engine.status) reads the JSONL once.
        Tolerant of malformed rows (``.get`` defaults) and of models absent from the
        price table — those still count toward spend but are excluded from the
        counterfactual (and named in ``unpriced_models``)."""
        rows = self.rows() if rows is None else rows

        def _bump(d: dict, key: str, cost: float, ci: int, co: int, cr: int, cw: int) -> None:
            b = d.setdefault(key, {"invocations": 0, "input_tokens": 0, "output_tokens": 0,
                                   "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0})
            b["invocations"] += 1
            b["input_tokens"] += ci
            b["output_tokens"] += co
            b["cache_read_tokens"] += cr
            b["cache_write_tokens"] += cw
            b["cost_usd"] = round(b["cost_usd"] + cost, 6)

        by_stage: dict[str, dict] = {}
        by_task: dict[str, dict] = {}
        total_cost = 0.0
        fresh_input = output = cache_read = cache_write = 0
        cache_read_savings = 0.0  # money saved: reads billed at read_mult, not full input
        cache_write_premium = 0.0  # money spent: writes billed above plain input
        unpriced: set[str] = set()

        for row in rows:
            cost = row.get("cost_usd") or 0.0
            model = row.get("model", "unknown")
            ci = row.get("input_tokens", 0) or 0
            co = row.get("output_tokens", 0) or 0
            cr = row.get("cache_read_tokens", 0) or 0
            cw = row.get("cache_write_tokens", 0) or 0
            total_cost += cost
            fresh_input += ci
            output += co
            cache_read += cr
            cache_write += cw
            _bump(by_stage, row.get("stage", "unknown"), cost, ci, co, cr, cw)
            _bump(by_task, row.get("task_id", "unknown"), cost, ci, co, cr, cw)
            try:
                info = self.model_table.info(model)
            except KeyError:
                unpriced.add(model)
                continue
            in_price = info.input_per_mtok
            cache_read_savings += cr * in_price * (1.0 - info.cache_read_mult) / 1_000_000
            cache_write_premium += cw * in_price * (info.cache_write_mult - 1.0) / 1_000_000

        # uncached counterfactual: every input-side token billed once at full input
        # price (no read discount, no write premium) => actual + net_win.
        net_win = cache_read_savings - cache_write_premium
        uncached_cost = total_cost + net_win
        input_side = fresh_input + cache_read + cache_write
        return {
            "total_cost_usd": round(total_cost, 6),
            "by_stage": by_stage,
            "by_task": by_task,
            "session_reuse": {
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "fresh_input_tokens": fresh_input,
                "output_tokens": output,
                # share of input-side tokens served from cache (the reuse rate)
                "cache_hit_ratio": round(cache_read / input_side, 4) if input_side else 0.0,
                "cache_read_savings_usd": round(cache_read_savings, 6),
                "cache_write_premium_usd": round(cache_write_premium, 6),
                "net_win_usd": round(net_win, 6),
                "uncached_cost_usd": round(uncached_cost, 6),
                "win_pct": round(100.0 * net_win / uncached_cost, 2) if uncached_cost else 0.0,
                "unpriced_models": sorted(unpriced),
            },
        }
