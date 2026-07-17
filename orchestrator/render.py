"""Human-readable Markdown renderers (target.md §6.4 cost-summary + observability).

The engine persists structured JSON as the contract; these render the same data
into the Markdown artifacts the as-built system produced (cost-summary.md, a
per-task stage index, per-stage prose). Pure functions — text in, Markdown out —
so they are trivially testable and the engine just writes their output.
"""

from __future__ import annotations

from .schemas.enums import STAGE_ORDER, ExecutionMode, StageStatus
from .schemas.status import StageRecord, Task
from .stream_probe import looks_like_event_stream, readable_text_from_stream

# Effort rows read high→low (then the effort-less '(default)' bucket, then any unknown
# label) rather than alphabetically, so the per-effort spend table scans in intensity order.
_EFFORT_RANK = {"high": 0, "medium": 1, "low": 2, "(default)": 3}


def _effort_sort_key(effort: str) -> tuple[int, str]:
    return (_EFFORT_RANK.get(effort, 4), effort)


def _cost_cell(rec: StageRecord) -> str:
    """Cost column for a stage row. The deterministic ENGINE lane runs no model, so its
    genuine $0 is tagged ``$0 (engine)`` (#68/#120) — a visible deterministic-stage win, not
    a bare $0 that reads as free-because-unmetered. The interactive lane can't meter
    per-stage cost in-session, so it records 0.0 — render that as ``n/a`` rather than
    ``$0.0000``. Metered lanes (headless/codex) show the real figure."""
    if rec.lane is ExecutionMode.ENGINE:
        return "$0 (engine)"
    if rec.lane is ExecutionMode.INTERACTIVE:
        return "n/a"
    return f"${rec.cost_usd:.4f}" if isinstance(rec.cost_usd, (int, float)) else "—"


def _effort_cell(rec: StageRecord) -> str:
    """Effort column for a stage row (#159): the reasoning effort the stage ran at, the
    sibling of ``model``. Shows high vs medium after any capacity downshift; ``—`` on
    effort-less rows (deterministic ENGINE-lane stages, specs without a default)."""
    return rec.effort.value if rec.effort is not None else "—"


def render_cost_summary(run_id: str, summary: dict, budget: dict | None = None) -> str:
    """Render `ledger.summary()` into cost-summary.md.

    ``budget`` (the engine's per-run budget block, #34) adds a spent/budget/remaining line
    when a budget is set — so the cost artifact surfaces the cap, not just the raw total."""
    # Defensive: a present-but-None value or a partial by_model bucket must not crash
    # the render (it runs at run finalization).
    total_cost = summary.get("total_cost_usd") or 0.0
    unmetered = summary.get("unmetered_calls") or 0
    invocations = summary.get("total_invocations") or 0
    # HONESTY: unmetered interactive calls have UNKNOWN cost — a bare $0.0000 total
    # would read as "this run was free". Say what is metered and what isn't.
    if unmetered and unmetered == invocations:
        cost_line = (f"- Total cost: **n/a — all {unmetered} call(s) ran on the "
                     f"interactive lane, which cannot meter per-call usage** (billed "
                     f"to the session's subscription, not $0)")
    elif unmetered:
        cost_line = (f"- Total cost: **${total_cost:.4f}** (metered lanes only — "
                     f"⚠️ {unmetered} interactive call(s) are unmetered and NOT included)")
    else:
        cost_line = f"- Total cost: **${total_cost:.4f}**"
    lines = [
        f"# Cost summary — {run_id}",
        "",
        f"- Invocations: **{invocations}**",
        cost_line,
    ]
    if budget:
        b = budget.get("budget_usd") or 0.0
        spent = budget.get("spent_usd") or 0.0
        frac = budget.get("fraction") or 0.0
        state = "⛔ EXHAUSTED" if budget.get("exhausted") else "within budget"
        lines.append(
            f"- Budget (metered): **${spent:.4f} / ${b:.4f}** "
            f"({frac * 100:.0f}% used — {state}; ${budget.get('remaining_usd') or 0.0:.4f} remaining)"
        )
    wall_s = summary.get("total_wall_s") or 0.0
    if wall_s:
        lines.append(f"- Wall time (in model calls): **{wall_s / 60.0:.1f} min**")
    # Deterministic ENGINE-lane line item (#68/#120): make the $0 deterministic-stage win a
    # visible figure — invocations the engine ran itself, at no model cost — rather than a
    # saving an operator has to reconstruct by scanning stage-costs.jsonl by hand.
    engine_lane = summary.get("engine_lane") or {}
    if engine_lane.get("invocations"):
        lines.append(
            f"- Deterministic (engine) lane: **{engine_lane['invocations']} invocation(s) "
            f"at $0 (engine)** — ran in-process, no model call"
        )
    lines += [
        "",
        "| Model | Invocations | Input tok | Output tok | Cost (USD) |",
        "|---|---:|---:|---:|---:|",
    ]
    for model, m in sorted(summary.get("by_model", {}).items()):
        lines.append(
            f"| `{model}` | {m.get('invocations', 0)} | {m.get('input_tokens', 0)} | "
            f"{m.get('output_tokens', 0)} | ${(m.get('cost_usd') or 0.0):.4f} |"
        )
    # Per-effort spend (#145/#152): spend split by reasoning effort alongside per-model, so
    # an operator sees how much high-effort stages consume vs medium/low when tuning defaults.
    by_effort_spend = summary.get("by_effort_spend") or {}
    if by_effort_spend:
        lines += [
            "",
            "| Effort | Invocations | Cost (USD) |",
            "|---|---:|---:|",
        ]
        for effort in sorted(by_effort_spend, key=_effort_sort_key):
            e = by_effort_spend[effort]
            lines.append(
                f"| `{effort}` | {e.get('invocations', 0)} | "
                f"${(e.get('cost_usd') or 0.0):.4f} |"
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


def render_by_effort(run_id: str, agg: list[dict]) -> str:
    """Render `ledger.by_effort()` into an effort-tuning table keyed by stage/effort/model (#141).

    One row per (stage, effort, model) group with calls, cost, avg duration, retry-rate and
    failure-rate — the empirical evidence for validating or revising the per-stage effort
    defaults from #96. Groups arrive pre-ordered (stage, then effort, then model)."""
    lines = [
        f"# Cost by effort — {run_id}",
        "",
        "_Per-stage reasoning effort (#96) validated against real spend: does a given "
        "stage/effort actually cost, retry, or fail more? Evidence to tune the #96 defaults._",
        "",
        "| Stage | Effort | Model | Calls | Cost (USD) | Avg dur (s) | Retry rate | Failure rate |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for g in agg:
        lines.append(
            f"| `{g.get('stage', 'unknown')}` | `{g.get('effort', '(default)')}` | "
            f"`{g.get('model', 'unknown')}` | {g.get('invocations', 0)} | "
            f"${(g.get('cost_usd') or 0.0):.4f} | {(g.get('avg_duration_s') or 0.0):.1f} | "
            f"{(g.get('retry_rate') or 0.0) * 100:.0f}% | {(g.get('failure_rate') or 0.0) * 100:.0f}% |"
        )
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
        f"{t.get('cascade_blocked', 0)} cascade-blocked · "
        f"{t.get('closed_infeasible', 0)} closed-infeasible of {t.get('total', 0)} tasks.",
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

    # #67: deliberately-closed tasks are NOT failures — surface them separately (with the
    # human's reason) so a mixed run's retrospective doesn't silently drop them.
    rejected = retro.get("rejected_tasks", [])
    if rejected:
        lines += ["## Closed as infeasible (human-rejected, not failures)", ""]
        for r in rejected:
            reason = r.get("reason") or "(no reason recorded)"
            lines.append(f"- `{r['task_id']}` — {r.get('title') or '(no title)'}: _{reason}_")
        lines.append("")

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


def format_review_issue(issue: object) -> str:
    """One blocking review issue → a compact one-line string, for learnings and the
    completion note. Tolerates both contract shapes: a plain string, or the structured
    ``{severity, file, line, description, suggested_fix}`` object (any subset)."""
    if not isinstance(issue, dict):
        return str(issue).strip()
    where = str(issue.get("file") or "").strip()
    if where and issue.get("line") is not None:
        where += f":{issue['line']}"
    parts = [
        p for p in (
            str(issue.get("severity") or "").strip(),
            where,
            str(issue.get("description") or "").strip(),
        ) if p
    ]
    text = " — ".join(parts) if parts else str(issue)
    fix = str(issue.get("suggested_fix") or "").strip()
    if fix:
        text += f" (suggested fix: {fix})"
    return text


def _md_scalar(v: object) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if v is None or v == [] or v == {}:
        return "—"
    return str(v)


# A dict's natural heading field: when present, the object renders as a titled block
# (bold lead + its other fields nested) rather than a flat `key: value` bullet list —
# so a list of {title, detail} findings reads as titled notes, not an undifferentiated run.
_LEAD_KEYS = ("title", "name")


def _lead_key(d: dict) -> str | None:
    return next((k for k in _LEAD_KEYS if isinstance(d.get(k), str) and d[k].strip()), None)


def _render_struct(obj: object, depth: int = 0) -> list[str]:
    """A stage's structured output as readable Markdown — not a JSON dump.

    The machine-exact copy lives in the sibling ``NN-<stage>.json``; this is the human
    view. Scalars become ``**key:** value`` bullets; a dict carrying a ``title``/``name``
    leads with that as a bold heading and renders its prose fields as continuation
    paragraphs; object items in a list are separated by a blank line so they don't blur."""
    pad = "  " * depth
    out: list[str] = []
    if isinstance(obj, dict):
        lead = _lead_key(obj)
        if lead is not None:
            out.append(f"{pad}- **{obj[lead]}**")
            for k, v in obj.items():
                if k == lead:
                    continue
                if isinstance(v, str) and v.strip():
                    out.append(f"{pad}  {v}")  # prose continuation under the heading
                elif isinstance(v, (dict, list)) and v:
                    out.append(f"{pad}  - **{k}:**")
                    out += _render_struct(v, depth + 2)
                else:
                    out.append(f"{pad}  - **{k}:** {_md_scalar(v)}")
        else:
            for k, v in obj.items():
                if isinstance(v, (dict, list)) and v:
                    out.append(f"{pad}- **{k}:**")
                    out += _render_struct(v, depth + 1)
                else:
                    out.append(f"{pad}- **{k}:** {_md_scalar(v)}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)) and item:
                if i and isinstance(item, dict):
                    out.append("")  # separate object items so titled blocks don't merge
                out += _render_struct(item, depth)
            else:
                out.append(f"{pad}- {_md_scalar(item)}")
    else:
        out.append(f"{pad}- {_md_scalar(obj)}")
    return out


def render_stage(payload: dict) -> str:
    """Render one stage's durable record (the write_stage_log payload) to Markdown.

    The Markdown is the *human* view (readable header + result bullets + any narrative);
    the full, machine-exact record is the sibling ``NN-<stage>.json``. No JSON is embedded
    here on purpose."""
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
        lines += ["", "## Result", "", *_render_struct(payload["structured_output"])]
    if payload.get("raw_output"):
        lines += ["", "## Commentary", "", *_commentary(str(payload["raw_output"]), payload)]
    lines.append("")
    return "\n".join(lines)


def _stream_pointer(payload: dict) -> str | None:
    """The relpath of the stage's retained full provider stream (``stream_files["stream"]``),
    for the Commentary pointer line — or None when no stream was teed."""
    sf = payload.get("stream_files")
    if isinstance(sf, dict) and isinstance(sf.get("stream"), str) and sf["stream"]:
        return sf["stream"]
    return None


def _commentary(raw: str, payload: dict) -> list[str]:
    """The ``## Commentary`` body for a stage's raw_output. Normally the raw_output IS the
    model's readable final text (transport puts it there since #93), rendered verbatim. But an
    OLD-style (pre-#93) or replayed payload may still carry a whole JSONL event stream — dumping
    that into the human view is the #93 regression. So if it still LOOKS like an event stream,
    extract the readable text instead and add a pointer to the retained full stream."""
    if not looks_like_event_stream(raw):
        return [raw]
    extracted = readable_text_from_stream(raw)
    pointer = _stream_pointer(payload)
    body = [extracted] if extracted else [
        "_(the model produced no final text; the raw provider event stream is elided here)_"
    ]
    if pointer:
        body += ["", f"_Full provider event stream: `{pointer}`_"]
    return body


def render_task_index(task: Task, rejection_reason: str | None = None) -> str:
    """Render a per-task stage index (stages/<task>/index.md).

    ``rejection_reason`` (set only for a CLOSED_INFEASIBLE task) adds a line stating why a
    human closed the task as infeasible — so the human-readable index explains the close,
    not just the terminal state."""
    pr = f" — PR: {task.pr_url}" if task.pr_url else ""
    lines = [
        f"# Task {task.task_id} — {task.title or '(no title)'}",
        "",
        f"State: **{task.state.value}**{pr}",
    ]
    if rejection_reason:
        lines.append(f"Closed as infeasible: _{rejection_reason}_")
    lines += [
        "",
        "| # | Stage | Status | Model | Effort | Cost |",
        "|---:|---|---|---|---|---:|",
    ]
    for seq, stage in enumerate(STAGE_ORDER, start=1):
        rec = task.stages[stage]
        model = f"`{rec.model}`" if rec.model else "—"
        lines.append(
            f"| {seq:02d} | {stage.value} | {rec.status.value} | {model} | "
            f"{_effort_cell(rec)} | {_cost_cell(rec)} |"
        )
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
    engine filed from the review's improvement idea (None if unfiled).

    #188 — nothing silently dropped: non-blocking findings the engine did NOT file
    (dispositioned ``fix_now``/``drop``, or ``file`` findings past the per-task cap) are
    surfaced in a "Noted, not filed" section with a short reason so the drop bucket is
    durable in the PR/issue note rather than vanishing."""
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
        "| # | Stage | Status | Model | Effort | Cost |",
        "|---:|---|---|---|---|---:|",
    ]
    for seq, stage in enumerate(STAGE_ORDER, start=1):
        rec = task.stages[stage]
        model = f"`{rec.model}`" if rec.model else "—"
        lines.append(
            f"| {seq:02d} | {stage.value} | {rec.status.value} | {model} | "
            f"{_effort_cell(rec)} | {_cost_cell(rec)} |"
        )

    blocking = [format_review_issue(i) for i in (review.get("issues") or [])]
    blocking = [i for i in blocking if i]
    if blocking:
        lines += ["", "### Outstanding review issues"]
        lines += [f"- {i}" for i in blocking]

    if followups:
        lines += ["", "### Follow-ups filed (non-blocking findings)"]
        for f in followups:
            ref = f.get("ref")
            suffix = f" → {ref}" if ref else " → (filing failed)"
            lines.append(f"- {f.get('title', '(untitled)')}{suffix}")

    # #188: the "noted, moving on" destination. A non-blocking finding the engine did NOT
    # file — dispositioned `fix_now`/`drop`, or a `file` finding past the per-task cap — is
    # surfaced here so the drop bucket is durable in the PR/issue note (nothing silently
    # dropped). Derived from the review's findings minus the titles that got filed above.
    filed_titles = {str(f.get("title") or "").strip() for f in (followups or [])}
    _noted_reason = {"fix_now": "fixed in place (boy-scout)", "drop": "noted, not tracked"}
    noted: list[str] = []
    for finding in review.get("non_blocking") or []:
        if not isinstance(finding, dict):
            continue
        title = str(finding.get("title") or "").strip()
        if not title or title in filed_titles:
            continue
        disposition = str(finding.get("disposition") or "").strip().casefold()
        reason = _noted_reason.get(disposition, "over per-task cap")
        noted.append(f"- {title} — {reason}")
    if noted:
        lines += ["", "### Noted, not filed"] + noted

    # Self-improvement loop (heysoo parity): the run's own forward-looking idea + a
    # process lesson, so a completed run improves the project/process, not just ships a fix.
    improvement = review.get("improvement") if isinstance(review.get("improvement"), dict) else None
    if improvement and str(improvement.get("title", "")).strip():
        head = f"💡 **Improvement idea:** {improvement['title']}" + (
            f" → {improvement_ref}" if improvement_ref else "")
        lines += ["", "### Self-improvement", head]
        if str(improvement.get("detail", "")).strip():
            lines.append(str(improvement["detail"]).strip())
    retro = review.get("retrospective") if isinstance(review.get("retrospective"), dict) else None
    if retro and str(retro.get("title", "")).strip():
        if not (improvement and str(improvement.get("title", "")).strip()):
            lines += ["", "### Self-improvement"]
        lines += ["", f"🔍 **Process retrospective:** {retro['title']}"]
        if str(retro.get("detail", "")).strip():
            lines.append(str(retro["detail"]).strip())

    lines += ["", "_Produced by the orchestration harness — nothing dropped: findings that "
              "clear the filing bar are tracked as follow-up issues, the rest are noted "
              "above; the improvement idea is filed as an enhancement._", ""]
    return "\n".join(lines)


_STAGE_GLYPH = {
    StageStatus.COMPLETED: "✅",
    StageStatus.RUNNING: "▶️",
    StageStatus.FAILED: "❌",
    StageStatus.PENDING: "…",
    StageStatus.SKIPPED: "⏭️",
}


def render_progress(task: Task, *, now: str | None = None) -> str:
    """Render a compact MID-RUN progress body (#64) — the living status the engine upserts
    onto the driving issue/PR while a long run is in flight (distinct from
    ``render_completion_note``, which is the once-at-finalize evidence).

    Derived purely from the task's recorded stages, so it is reconstructible on replay:
    a stage table over the task's own pipeline (done ✅ / running ▶️ / next … / failed ❌),
    per-stage attempt counts, the review-cycle / salvage / infra-reset budgets when non-zero,
    elapsed wall time, and metered cost-to-date summed across the recorded stages.
    ``now`` (ISO) overrides the elapsed clock for deterministic tests."""
    state = task.state.value.replace("_", " ")
    lines = [
        f"## Run progress — {task.task_id}",
        "",
        f"- **Task:** {task.title or '(no title)'}",
        f"- **State:** {state}",
    ]
    if task.pr_url:
        lines.append(f"- **PR:** {task.pr_url}")

    # Metered cost-to-date: sum the recorded per-stage costs. Interactive stages record
    # 0.0 (they cannot meter in-session), so note them rather than implying "free".
    total = 0.0
    metered = False
    unmetered_stages = 0
    for stage in task.pipeline:
        rec = task.stages[stage]
        if rec.status is StageStatus.COMPLETED and rec.lane is ExecutionMode.INTERACTIVE:
            unmetered_stages += 1
        elif isinstance(rec.cost_usd, (int, float)):
            total += rec.cost_usd
            metered = True
    if metered and unmetered_stages:
        cost_str = f"${total:.4f} (+{unmetered_stages} unmetered interactive stage(s))"
    elif metered:
        cost_str = f"${total:.4f}"
    elif unmetered_stages:
        cost_str = "n/a (interactive lane — unmetered)"
    else:
        cost_str = "—"
    lines.append(f"- **Cost to date:** {cost_str}")

    elapsed = _elapsed_min(task.created_at, now)
    if elapsed is not None:
        lines.append(f"- **Elapsed:** {elapsed:.1f} min")

    notes = []
    if task.review_cycles:
        notes.append(f"{task.review_cycles} review cycle(s)")
    if task.salvage_count:
        notes.append(f"{task.salvage_count} salvage-keep(s)")
    if task.infra_resets:
        notes.append(f"{task.infra_resets} infra-reset(s)")
    if task.rate_limit_waits:
        notes.append(f"{task.rate_limit_waits} rate-limit wait(s)")
    if notes:
        lines.append(f"- **Recovery:** {', '.join(notes)}")

    lines += [
        "",
        "| Stage | Status | Attempts | Effort | Cost |",
        "|---|---|---:|---|---:|",
    ]
    next_marked = False
    for stage in task.pipeline:
        rec = task.stages[stage]
        glyph = _STAGE_GLYPH.get(rec.status, "")
        label = f"{glyph} {rec.status.value}".strip()
        # Flag the first not-yet-started stage as the upcoming one.
        if rec.status is StageStatus.PENDING and not next_marked:
            label += " (next)"
            next_marked = True
        lines.append(
            f"| {stage.value} | {label} | {rec.attempt} | {_effort_cell(rec)} | {_cost_cell(rec)} |"
        )
    lines += ["", "_Live progress from the orchestration harness; updated as stages land._", ""]
    return "\n".join(lines)


def _elapsed_min(created_at: str, now: str | None) -> float | None:
    from datetime import UTC, datetime

    try:
        start = datetime.fromisoformat(created_at)
        end = datetime.fromisoformat(now) if now else datetime.now(UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return max(0.0, (end - start).total_seconds()) / 60.0
    except ValueError:
        return None


def render_rejection_note(
    task: Task, reason: str, *, rejected_by: str | None = None
) -> str:
    """Render the note the engine publishes back to the task source when a human closes a
    held task as infeasible (Engine.reject → CLOSED_INFEASIBLE, #53). A deliberate close,
    NOT an execution failure — the note says so, and carries the reason so the decision
    outlives the run logs."""
    by = f" by **{rejected_by}**" if rejected_by else ""
    return "\n".join([
        f"## Orchestration run closed — {task.task_id} (infeasible)",
        "",
        f"- **Task:** {task.title or '(no title)'}",
        f"- **Outcome:** ❌ closed as infeasible{by}",
        f"- **Reason:** {reason or '(none given)'}",
        "",
        "_A human reviewed this task at the approval gate and confirmed it should not be "
        "done. This is a deliberate close, not an execution failure._",
        "",
    ])
