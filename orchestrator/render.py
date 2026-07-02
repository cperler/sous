"""Human-readable Markdown renderers (target.md §6.4 cost-summary + observability).

The engine persists structured JSON as the contract; these render the same data
into the Markdown artifacts the as-built system produced (cost-summary.md, a
per-task stage index, per-stage prose). Pure functions — text in, Markdown out —
so they are trivially testable and the engine just writes their output.
"""

from __future__ import annotations

import json

from .schemas.enums import STAGE_ORDER, ExecutionMode
from .schemas.status import StageRecord, Task


def _cost_cell(rec: StageRecord) -> str:
    """Cost column for a stage row. The interactive lane can't meter per-stage cost
    in-session, so it records 0.0 — render that as ``n/a`` rather than ``$0.0000``, which
    reads as 'this stage was free'. Metered lanes (headless/codex) show the real figure."""
    if rec.lane is ExecutionMode.INTERACTIVE:
        return "n/a"
    return f"${rec.cost_usd:.4f}" if isinstance(rec.cost_usd, (int, float)) else "—"


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


def render_retrospective(retro: dict) -> str:
    """Render `build_retrospective()` into retrospective.md."""
    t = retro.get("totals", {})
    lines = [
        f"# Failure retrospective — {retro.get('run_id', '?')}",
        "",
        f"Run state: **{retro.get('run_state', '?')}** — "
        f"{t.get('completed', 0)} completed · {t.get('failed', 0)} failed · "
        f"{t.get('cascade_blocked', 0)} cascade-blocked of {t.get('total', 0)} tasks.",
    ]

    failed = retro.get("failed_tasks", [])
    if failed:
        lines += ["", "## Failed tasks", ""]
        for f in failed:
            reason = (f.get("terminal_reason") or "failed").replace("_", " ")
            lines.append(
                f"### `{f['task_id']}` — {f.get('title') or '(no title)'}"
            )
            lines.append(
                f"- Failed at **{f.get('failing_stage') or '?'}** after "
                f"**{f.get('attempts', 0)}** attempt(s) — _{reason}_"
            )
            if f.get("final_error"):
                lines.append(f"- Final error: `{str(f['final_error'])[:300]}`")
            if f.get("blocked_dependents"):
                lines.append(
                    f"- Blocked dependents: {', '.join(f'`{d}`' for d in f['blocked_dependents'])}"
                )
            if f.get("learnings"):
                lines += ["- What the retries learned:"]
                lines += [f"  {i + 1}. {ln}" for i, ln in enumerate(f["learnings"])]
            lines.append("")

    if retro.get("cascade_blocked_tasks"):
        blocked = ", ".join(f"`{t}`" for t in retro["cascade_blocked_tasks"])
        lines += [f"## Cascade-blocked (never ran): {blocked}", ""]

    patterns = retro.get("patterns", [])
    if patterns:
        lines += [
            "## Recurring failure patterns",
            "",
            "| Sig | Stage | Occurrences | Tasks | Plateau | Cross-task | Sample |",
            "|---|---|---:|---|:---:|:---:|---|",
        ]
        for p in patterns:
            sample = (p.get("sample_error") or "").replace("|", "\\|").replace("\n", " ")[:80]
            lines.append(
                f"| `{p['signature']}` | {p['stage']} | {p['occurrences']} | "
                f"{', '.join(p['tasks'])} | {'✓' if p['within_task_plateau'] else ''} | "
                f"{'✓' if p['cross_task'] else ''} | {sample} |"
            )
        lines.append("")
    elif not failed:
        lines += ["", "_No failures recorded._", ""]

    return "\n".join(lines)


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
        model = f"`{rec.model}`" if rec.model else "—"
        lines.append(f"| {seq:02d} | {stage.value} | {rec.status.value} | {model} | {_cost_cell(rec)} |")
    lines.append("")
    return "\n".join(lines)


def render_completion_note(
    task: Task, followups: list[dict] | None = None, improvement_ref: str | None = None
) -> str:
    """Render a run's completion evidence as Markdown — the note the engine publishes
    back to the task source (PR/issue comment) so the pipeline's reasoning outlives the
    run logs. Derived purely from the task's recorded stages + the follow-ups the engine
    filed; ``followups`` items are ``{"title", "ref"}`` (ref = the new issue URL/id, or
    None if filing failed). ``improvement_ref`` is the URL of the enhancement issue the
    engine filed from the review's improvement idea (None if unfiled)."""
    from .schemas.enums import Stage  # local: avoid widening the module import surface

    review = (task.stages[Stage.REVIEW].output or {}) if Stage.REVIEW in task.stages else {}
    approved = review.get("approved")
    verdict = "✅ approved" if approved else ("❌ changes requested" if approved is False else "—")
    lines = [
        f"## Orchestration run complete — {task.task_id}",
        "",
        f"- **Task:** {task.title or '(no title)'}",
        f"- **PR:** {task.pr_url or '(none)'}",
        f"- **Review:** {verdict}",
        "",
        "| # | Stage | Status | Model | Cost |",
        "|---:|---|---|---|---:|",
    ]
    for seq, stage in enumerate(STAGE_ORDER, start=1):
        rec = task.stages[stage]
        model = f"`{rec.model}`" if rec.model else "—"
        lines.append(f"| {seq:02d} | {stage.value} | {rec.status.value} | {model} | {_cost_cell(rec)} |")

    blocking = [i for i in (review.get("issues") or []) if str(i).strip()]
    if blocking:
        lines += ["", "### Outstanding review issues"]
        lines += [f"- {i}" for i in blocking]

    if followups:
        lines += ["", "### Follow-ups filed (non-blocking findings)"]
        for f in followups:
            ref = f.get("ref")
            suffix = f" → {ref}" if ref else " → (filing failed)"
            lines.append(f"- {f.get('title', '(untitled)')}{suffix}")

    # Self-improvement loop (heysoo parity): the run's own forward-looking idea + a
    # process lesson, so a completed run improves the project/process, not just ships a fix.
    improvement = review.get("improvement") if isinstance(review.get("improvement"), dict) else None
    if improvement and str(improvement.get("title", "")).strip():
        head = f"💡 **Improvement idea:** {improvement['title']}" + (
            f" → {improvement_ref}" if improvement_ref else "")
        lines += ["", "### Self-improvement", head]
        if str(improvement.get("detail", "")).strip():
            lines.append(improvement["detail"].strip())
    retro = review.get("retrospective") if isinstance(review.get("retrospective"), dict) else None
    if retro and str(retro.get("title", "")).strip():
        if not (improvement and str(improvement.get("title", "")).strip()):
            lines += ["", "### Self-improvement"]
        lines += ["", f"🔍 **Process retrospective:** {retro['title']}"]
        if str(retro.get("detail", "")).strip():
            lines.append(retro["detail"].strip())

    lines += ["", "_Produced by the orchestration harness — nothing dropped: non-blocking "
              "findings are tracked as follow-up issues; the improvement idea is filed as an "
              "enhancement._", ""]
    return "\n".join(lines)
