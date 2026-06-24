"""Human-readable Markdown renderers (target.md §6.4 cost-summary + observability).

The engine persists structured JSON as the contract; these render the same data
into the Markdown artifacts the as-built system produced (cost-summary.md, a
per-task stage index, per-stage prose). Pure functions — text in, Markdown out —
so they are trivially testable and the engine just writes their output.
"""

from __future__ import annotations

import json

from .schemas.enums import STAGE_ORDER
from .schemas.status import Task


def render_cost_summary(run_id: str, summary: dict) -> str:
    """Render `ledger.summary()` into cost-summary.md."""
    # Defensive: a present-but-None value or a partial by_model bucket must not crash
    # the render (it runs at run finalization).
    total_cost = summary.get("total_cost_usd") or 0.0
    lines = [
        f"# Cost summary — {run_id}",
        "",
        f"- Invocations: **{summary.get('total_invocations') or 0}**",
        f"- Total cost: **${total_cost:.4f}**",
        "",
        "| Model | Invocations | Input tok | Output tok | Cost (USD) |",
        "|---|---:|---:|---:|---:|",
    ]
    for model, m in sorted(summary.get("by_model", {}).items()):
        lines.append(
            f"| `{model}` | {m.get('invocations', 0)} | {m.get('input_tokens', 0)} | "
            f"{m.get('output_tokens', 0)} | ${(m.get('cost_usd') or 0.0):.4f} |"
        )
    lines += ["", "_Priced from the single model table; raw rows in `stage-costs.jsonl`._", ""]
    return "\n".join(lines)


def render_cost_report(run_id: str, analysis: dict) -> str:
    """Render `ledger.analysis()` into cost-report.md (per-stage/-task + reuse win)."""
    reuse = analysis.get("session_reuse", {})
    total = analysis.get("total_cost_usd") or 0.0
    uncached = reuse.get("uncached_cost_usd") or 0.0
    net = reuse.get("net_win_usd") or 0.0
    lines = [
        f"# Cost report — {run_id}",
        "",
        f"- Total cost: **${total:.4f}**",
        "",
        "## Session-reuse win",
        "",
        "_The collapsed-stage thesis: chaining stages in one session serves input from "
        "the prompt cache. This is what that saved vs. an uncached run._",
        "",
        f"- Cache hit ratio (input-side): **{(reuse.get('cache_hit_ratio') or 0.0) * 100:.1f}%**",
        f"- Uncached counterfactual: **${uncached:.4f}**",
        f"- Net session-reuse win: **${net:.4f}** (**{reuse.get('win_pct') or 0.0:.1f}%** of uncached)",
        f"  - cache-read savings: ${reuse.get('cache_read_savings_usd') or 0.0:.4f}",
        f"  - cache-write premium: −${reuse.get('cache_write_premium_usd') or 0.0:.4f}",
        f"- Tokens: {reuse.get('fresh_input_tokens', 0)} fresh in · "
        f"{reuse.get('cache_read_tokens', 0)} cache-read · "
        f"{reuse.get('cache_write_tokens', 0)} cache-write · "
        f"{reuse.get('output_tokens', 0)} out",
    ]
    if reuse.get("unpriced_models"):
        lines.append(
            f"- ⚠️ Excluded from the counterfactual (no price in the model table): "
            f"{', '.join(f'`{m}`' for m in reuse['unpriced_models'])}"
        )
    lines += _cost_breakdown_table("By stage", "Stage", analysis.get("by_stage", {}))
    lines += _cost_breakdown_table("By task", "Task", analysis.get("by_task", {}))
    lines += ["", "_Priced from the single model table; raw rows in `stage-costs.jsonl`._", ""]
    return "\n".join(lines)


def _cost_breakdown_table(heading: str, col: str, buckets: dict) -> list[str]:
    """Shared per-stage / per-task table body, ordered by descending cost."""
    lines = [
        "",
        f"## {heading}",
        "",
        f"| {col} | Calls | Fresh in | Cache read | Cache write | Out | Cost (USD) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    ordered = sorted(buckets.items(), key=lambda kv: kv[1].get("cost_usd") or 0.0, reverse=True)
    for key, b in ordered:
        lines.append(
            f"| `{key}` | {b.get('invocations', 0)} | {b.get('input_tokens', 0)} | "
            f"{b.get('cache_read_tokens', 0)} | {b.get('cache_write_tokens', 0)} | "
            f"{b.get('output_tokens', 0)} | ${(b.get('cost_usd') or 0.0):.4f} |"
        )
    return lines


def render_stage(payload: dict) -> str:
    """Render one stage's durable record (the write_stage_log payload) to Markdown."""
    lane = payload.get("lane_used") or {}
    lane_str = f"{lane.get('execution_mode', '?')}:{lane.get('provider', '?')}"
    cost = payload.get("cost_usd")
    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
    lines = [
        f"# {payload.get('stage', '?')} — {payload.get('task_id', '?')} "
        f"(attempt {payload.get('attempt', 0)})",
        "",
        f"- Status: **{payload.get('status', '?')}** ({payload.get('outcome', '')})",
        f"- Model: `{payload.get('model', '?')}` ({lane_str})",
        f"- Cost: {cost_str}",
        f"- Completed: {payload.get('completed_at', '')}",
    ]
    if payload.get("error"):
        lines += ["", f"**Error:** {payload['error']}"]
    if payload.get("structured_output") is not None:
        lines += ["", "## Structured output", "", "```json",
                  json.dumps(payload["structured_output"], indent=2), "```"]
    if payload.get("raw_output"):
        lines += ["", "## Output (raw)", "", str(payload["raw_output"])]
    lines.append("")
    return "\n".join(lines)


def render_task_index(task: Task) -> str:
    """Render a per-task stage index (stages/<task>/index.md)."""
    pr = f" — PR: {task.pr_url}" if task.pr_url else ""
    lines = [
        f"# Task {task.task_id} — {task.title or '(no title)'}",
        "",
        f"State: **{task.state.value}**{pr}",
        "",
        "| # | Stage | Status | Model | Cost |",
        "|---:|---|---|---|---:|",
    ]
    for seq, stage in enumerate(STAGE_ORDER, start=1):
        rec = task.stages[stage]
        cost = f"${rec.cost_usd:.4f}" if isinstance(rec.cost_usd, (int, float)) else "—"
        model = f"`{rec.model}`" if rec.model else "—"
        lines.append(f"| {seq:02d} | {stage.value} | {rec.status.value} | {model} | {cost} |")
    lines.append("")
    return "\n".join(lines)
