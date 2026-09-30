"""Console rendering of the dashboard snapshot (#525).

The board answers an operator's questions in the order they ask them: does anything need
me (with the command that resolves it printed under each item), what is running right now,
how far along is every task, what has it cost, and what just happened. Every status is a
short word ("done", "needs you", "stream stalled 6m") rather than a code or an icon; the one
symbol that remains, ``-`` for a stage not started, gets a legend line.

Two layouts, chosen by the terminal ``width``: COMPACT below ``WIDE_MIN_COLUMNS`` (one line
per run, running tasks only, per-run cost totals without the per-stage table) and WIDE at or
above it (every task with its stage strip, and cost broken down per task and per stage).

Pure: a snapshot dict in, text out. No I/O and no clock — ages are measured against the
snapshot's own ``generated_at``, so the same snapshot always renders the same board. The
public entry point is ``dashboard.render_dashboard``, which imports this module lazily
(this module reads ``dashboard``'s state markers, so the import runs one way only).
"""

from __future__ import annotations

from .dashboard import (
    _TERMINAL_TASK,
    ADAPTER_UNRESOLVED_STATE,
    UNREADABLE_STATE,
    _error_text,
    _iso_epoch,
    _progress_str,
)
from .render import aggregate_cost_cell

#: Terminals at least this wide get the WIDE board; narrower ones the COMPACT one.
WIDE_MIN_COLUMNS = 100

#: The width a caller that gives none is rendered at (the wide board, wrapped here).
_DEFAULT_WIDTH = 120

#: How many of the board's newest events the "recent" section prints, per layout.
_RECENT_WIDE = 10
_RECENT_COMPACT = 5

#: The legend's one symbol: a stage that has not started and is not up next.
_NOT_STARTED = "-"

_RUN_STATE_LABELS = {
    "running": "running",
    "pending": "not started",
    "paused": "paused",
    "parked": "parked",
    "failed": "failed",
    "completed": "done",
    "completed_with_rejections": "done with rejections",
    "superseded": "superseded",
    UNREADABLE_STATE: "unreadable",
    ADAPTER_UNRESOLVED_STATE: "adapter unresolved",
}

_TASK_STATE_LABELS = {
    "pending": "queued",
    "running": "running",
    "retrying": "retrying",
    "blocked": "blocked",
    "cascade_blocked": "dep failed",
    "blocked_on_human": "needs you",
    "completed": "done",
    "failed": "failed",
    "closed_infeasible": "closed",
    "superseded": "superseded",
}

_STAGE_STATUS_LABELS = {
    "completed": "done",
    "running": "running",
    "failed": "failed",
    "skipped": "skipped",
}


def console_width(columns: int, *, wide: bool = False, compact: bool = False) -> int:
    """The ``width`` to render at, given the terminal's real ``columns`` and the CLI's
    ``--wide`` / ``--compact`` overrides: an override forces its layout without pretending
    the terminal is a different size when it already qualifies."""
    if wide:
        return max(columns, WIDE_MIN_COLUMNS)
    if compact:
        return min(columns, WIDE_MIN_COLUMNS - 1)
    return columns


# --- small formatters ---------------------------------------------------------------------


def _fmt_age(secs: float | None) -> str:
    if secs is None:
        return "?"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _short_model(model: str | None) -> str | None:
    """``claude-opus-5-5`` → ``opus-5-5``: the vendor prefix is on every model we run."""
    if not model:
        return None
    return model.removeprefix("claude-")


def _tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _wrap_cells(prefix: str, cells: list[str], *, indent: int, width: int) -> list[str]:
    """``prefix`` followed by ``cells`` joined with `` · ``, wrapped onto continuation
    lines indented by ``indent`` whenever the next cell would pass ``width``."""
    lines: list[str] = []
    line = prefix
    for i, cell in enumerate(cells):
        piece = cell if i == 0 else " · " + cell
        if i and len(line) + len(piece) > width:
            lines.append(line)
            line = " " * indent + cell
        else:
            line += piece
    lines.append(line)
    return lines


def _section(title: str, body: list[str]) -> list[str]:
    return ["", f"── {title} ──", *body]


# --- headline -----------------------------------------------------------------------------

_RUN_STATE_ORDER = list(_RUN_STATE_LABELS)


def _run_counts(counts: dict[str, int], shown: int) -> str:
    """``2 runs: 1 running, 1 done`` — the header's per-state counts in words."""
    if not shown:
        return "no runs"
    states = _RUN_STATE_ORDER + sorted(s for s in counts if s not in _RUN_STATE_LABELS)
    bits = [f"{counts[s]} {_RUN_STATE_LABELS.get(s, s)}" for s in states if counts.get(s)]
    return f"{_plural(shown, 'run')}: {', '.join(bits)}"


def _headline(header: dict) -> str:
    summary = _run_counts(header.get("counts") or {}, header.get("shown") or 0)
    if header.get("all_quiet"):
        return f"ALL QUIET — {summary}"
    n = header.get("attention_count") or 0
    needs = "item needs you" if n == 1 else "items need you"
    return f"ATTENTION — {n} {needs}  ·  {summary}"


# --- needs you ----------------------------------------------------------------------------


def _item_headline(item: dict, task: dict | None) -> str:
    """What one attention item is, in words, with the real cause where there is one."""
    kind = item["kind"]
    stage = item.get("stage") or (task or {}).get("current_stage")
    at = f" at {str(stage).upper()}" if stage else ""
    reason = item.get("reason")
    if kind == "blocked_on_human":
        return f"BLOCKED{at}, needs your decision — {reason or 'held at the human gate'}"
    if kind == "paused":
        return f"PAUSED — {reason or 'paused'}"
    if kind == "parked":
        return f"PARKED — needs a fresh supervisor ({reason or 'supervisor context exhausted'})"
    if kind == "failed":
        return f"FAILED{at} — {reason}"
    if kind == "budget_exhausted":
        frac = item.get("fraction")
        pct = f"{frac * 100:.0f}%" if isinstance(frac, (int, float)) else "?"
        return f"BUDGET SPENT — metered spend is at {pct} of the budget"
    if kind == "stale":
        return f"STALE{at} — no update for {_fmt_age(item.get('seconds_since_update'))}"
    if kind == "unreadable":
        # #498: name the cause. "Inspect by hand" alone sent the operator to status docs
        # that were usually fine; the caught exception is what says what actually failed.
        cause = _error_text(item.get("error")) or reason
        return f"UNREADABLE status — {cause or 'inspect the run dir by hand'}"
    if kind == "adapter_unresolved":
        return f"PROJECT ADAPTER UNRESOLVED — {reason}"
    return str(kind).replace("_", " ").upper()  # pragma: no cover - defensive


def _needs_you(snapshot: dict, *, compact: bool) -> list[str]:
    runs = snapshot["runs"]
    tasks = {(r["run_id"], t["task_id"]): t for r in runs for t in r.get("tasks") or []}
    roots = {r["run_id"]: r.get("root") for r in runs}
    lines: list[str] = []
    for item in snapshot["attention"]:
        run, tid = item["run_id"], item.get("task_id")
        who = f"{run} {tid}" if tid else run
        lines.append(f"  ! {who}  {_item_headline(item, tasks.get((run, tid)))}")
        commands = item.get("commands") or []
        if item["kind"] in ("unreadable", "adapter_unresolved") and roots.get(run):
            lines.append(f"      look in: {roots[run]}")
        for cmd in commands:
            if not compact:
                lines.append(f"      {cmd['label']}")
            # Never clipped: a command is only useful if it can be pasted whole.
            lines.append(f"        {cmd['command']}" if not compact else f"      {cmd['command']}")
    return lines


# --- running now --------------------------------------------------------------------------


def _stream_label(activity: dict | None, *, compact: bool) -> str:
    activity = activity or {}
    state = activity.get("state")
    if state == "active":
        line = activity.get("line")
        return f"stream active: {line}" if line and not compact else "stream active"
    if state == "stalled":
        return f"stream stalled {_fmt_age(activity.get('seconds_since_event'))}"
    return "no stream"


def _running_line(task: dict, *, width: int, compact: bool) -> str:
    stage = str(task["current_stage"]).upper()
    parts = [f"{stage} for {_fmt_age(task.get('stage_age_s'))}"]
    if model := _short_model(task.get("model")):
        parts.append(model)
    parts.append(_stream_label(task.get("activity"), compact=compact))
    prefix = f"    {task['task_id']}  "
    tail = "  —  " + " · ".join(parts)
    room = max(width - len(prefix) - len(tail), 12)
    return _clip(prefix + _clip(task.get("title") or "", room) + tail, width)


def _in_flight(task: dict) -> bool:
    """A stage is executing right now. A task can be ``running`` BETWEEN stages (its last
    stage recorded, the next not yet dispatched); that is progress, not activity."""
    return task["state"] == "running" and any(
        s["stage"] == task["current_stage"] and s["status"] == "running"
        for s in task.get("stages") or []
    )


def _running_now(runs: list[dict], *, width: int, compact: bool) -> list[str]:
    """One block per non-terminal run that has something in flight or a driver worth
    naming: the driver in plain words, then one line per running task."""
    lines: list[str] = []
    for row in runs:
        if row.get("degraded") or row.get("terminal"):
            continue
        active = [t for t in row.get("tasks") or [] if _in_flight(t)]
        driver = row.get("driver") or {}
        if not active and driver.get("state") in (None, "none", "finished"):
            continue
        summary = driver.get("summary") or "unknown"
        lines.append(_clip(f"  {row['run_id']} ({row.get('project') or '?'}) — driver: {summary}",
                           width))
        lines.extend(_running_line(t, width=width, compact=compact) for t in active)
    return lines or ["  nothing running"]


# --- progress -----------------------------------------------------------------------------


def _degraded_marker(row: dict) -> str:
    """The state cell of a degraded row (#498): what went wrong, not a blanket
    "unreadable" — an adapter that will not resolve says so, and a status read that raised
    names its exception."""
    if row.get("state") == ADAPTER_UNRESOLVED_STATE:
        return "<adapter unresolved>"
    cause = _error_text(row.get("error"))
    return f"<unreadable status: {cause}>" if cause else "<unreadable status>"


def _run_summary(row: dict) -> str:
    bits = [
        _RUN_STATE_LABELS.get(row["state"], row["state"]),
        f"{_progress_str(row.get('progress') or {})} done",
    ]
    needs = len(row.get("attention_items") or [])
    if needs:
        bits.append(f"{needs} need{'s' if needs == 1 else ''} you")
    budget = row.get("budget") or {}
    if isinstance(budget.get("fraction"), (int, float)):
        bits.append(f"budget {budget['fraction'] * 100:.0f}% used")
    bits.append(f"last event {_fmt_age(row.get('last_event_age_s'))} ago")
    return " · ".join(bits)


def _stage_strip(task: dict) -> tuple[list[str], bool]:
    """The task's pipeline as ``STAGE status`` cells, plus whether the not-started symbol
    appears. INTAKE is bookkeeping, shown only when it is running or failed. The first
    stage not yet started on a live task is ``next``; any later one is ``-``."""
    terminal = task["state"] in _TERMINAL_TASK
    cells: list[str] = []
    next_marked = False
    used_symbol = False
    for stage in task.get("stages") or []:
        status = stage["status"]
        if stage["stage"] == "intake" and status not in ("running", "failed"):
            continue
        if status == "pending":
            if not terminal and not next_marked:
                label, next_marked = "next", True
            else:
                label, used_symbol = _NOT_STARTED, True
        else:
            label = _STAGE_STATUS_LABELS.get(status, status)
            if status in ("running", "failed") and stage.get("attempt"):
                label += f" (retry {stage['attempt']})"
        cells.append(f"{stage['stage'].upper()} {label}")
    return cells, used_symbol


def _waiting(task: dict, row: dict) -> str | None:
    """What a task is waiting on, in words, or None when it is not waiting."""
    if task.get("waiting_on"):
        return "waiting on " + ", ".join(task["waiting_on"])
    state = task["state"]
    if state == "blocked_on_human":
        return "waiting on your decision"
    if state == "cascade_blocked":
        return "a dependency failed"
    if row.get("state") == "paused" and state not in _TERMINAL_TASK:
        return "waiting on the run to be unpaused"
    return None


def _task_lines(task: dict, row: dict, *, width: int, id_w: int) -> tuple[list[str], bool]:
    state = _TASK_STATE_LABELS.get(task["state"], str(task["state"]))
    pr = f"PR {task['pr_url']}" if task.get("pr_url") else None
    tail = "".join(f" — {bit}" for bit in (_waiting(task, row), pr) if bit)
    prefix = f"      {task['task_id']:<{id_w}}  {state:<10} "
    room = max(width - len(prefix) - len(tail), 12)
    lines = [prefix + _clip(task.get("title") or "", room) + tail]
    cells, used_symbol = _stage_strip(task)
    if cells:
        indent = 8 + id_w
        lines.extend(_wrap_cells(" " * indent, cells, indent=indent, width=width))
    return lines, used_symbol


def _by_project(runs: list[dict]) -> dict[str, list[dict]]:
    """Rows grouped by project label, in board order (so the most urgent group leads) —
    the same grouping as the snapshot header's ``projects``."""
    groups: dict[str, list[dict]] = {}
    for row in runs:
        groups.setdefault(str(row.get("project") or "?"), []).append(row)
    return groups


def _progress(runs: list[dict], *, width: int, compact: bool) -> list[str]:
    if not runs:
        return ["  (no runs found)"]
    lines: list[str] = []
    used_symbol = False
    run_w = max(len(r["run_id"]) for r in runs)
    for project, rows in _by_project(runs).items():
        if not compact:
            lines.append(f"  {project}")
        for row in rows:
            lead = f"  {project} · {row['run_id']:<{run_w}}" if compact else (
                f"    {row['run_id']:<{run_w}}"
            )
            if row.get("degraded") or row.get("unreadable"):
                lines.append(f"{lead}  {_degraded_marker(row)}")
                continue
            lines.append(_clip(f"{lead}  {_run_summary(row)}", width))
            if compact:
                continue
            tasks = row.get("tasks") or []
            id_w = max((len(t["task_id"]) for t in tasks), default=0)
            for task in tasks:
                task_lines, symbol = _task_lines(task, row, width=width, id_w=id_w)
                lines.extend(task_lines)
                used_symbol = used_symbol or symbol
    if used_symbol:
        lines.append(f"  legend: {_NOT_STARTED} not started")
    return lines


# --- cost ---------------------------------------------------------------------------------


def _spend(header: dict) -> str:
    """The board-wide spend, qualified (#331): unmetered calls sum in at $0, so a bare
    figure would understate the real spend while looking exact."""
    unmetered = header.get("unmetered_calls") or 0
    invocations = header.get("total_invocations") or 0
    total = header.get("total_spend_usd") or 0.0
    if unmetered and invocations and unmetered >= invocations:
        figure = f"n/a — all {unmetered} call(s) unmetered"
    elif unmetered:
        figure = f"≥${total:.4f} ({unmetered} unmetered call(s) of unknown cost excluded)"
    else:
        figure = f"${total:.4f}"
    return f"spend: {figure} across {header.get('shown') or 0} run(s)"


def _usage(usage: dict | None, *, compact: bool) -> str:
    """The account's utilization windows and the headroom left in each (#386: the probe
    reads the ACCOUNT, which every project on the board shares — hence the label)."""
    if not usage or usage.get("five_hour_pct") is None:
        return "usage (account): unavailable"
    five = float(usage["five_hour_pct"])
    seven = float(usage.get("seven_day_pct") or 0)
    if compact:
        return f"usage (account): 5h {five:.0f}% used · 7d {seven:.0f}% used"
    return (
        f"usage (account): 5h window {five:.0f}% used ({max(0.0, 100 - five):.0f}% left) · "
        f"7d window {seven:.0f}% used ({max(0.0, 100 - seven):.0f}% left)"
    )


def _stage_cost(stage: dict) -> str | None:
    """One stage's cost cell, unmetered-aware; None when the stage has no cost at all."""
    if stage.get("calls"):
        return aggregate_cost_cell(
            stage.get("cost_usd"), stage.get("unmetered_calls") or 0, stage["calls"]
        )
    cost = stage.get("cost_usd")
    if not isinstance(cost, (int, float)):
        return None
    return "n/a (unmetered)" if stage.get("metered") is False else f"${cost:.4f}"


def _run_cost_line(row: dict, *, run_w: int, compact: bool) -> str:
    cost = row.get("cost_usd")
    unmetered = row.get("unmetered_calls") or 0
    calls = row.get("total_invocations") or 0
    # #331: `≥$X` / `n/a (unmetered)` rather than a bare figure when this run's spend is
    # partly or wholly unknown; `$?` stays the marker for "no cost data at all".
    cell = (
        aggregate_cost_cell(cost, unmetered, calls) if isinstance(cost, (int, float)) else "$?"
    )
    line = f"  {row['run_id']:<{run_w}}  {cell}"
    if compact:
        return line
    detail = _plural(calls, "call")
    if unmetered:
        detail += f", {unmetered} unmetered (cost unknown)"
    breakdown = row.get("cost_breakdown") or {}
    if breakdown.get("calls"):
        detail += (
            f" · {_tokens(breakdown.get('input_tokens') or 0)} tokens in, "
            f"{_tokens(breakdown.get('output_tokens') or 0)} out"
        )
    return f"{line} · {detail}"


def _task_cost_lines(row: dict, *, width: int) -> list[str]:
    by_task = (row.get("cost_breakdown") or {}).get("by_task") or {}
    stages_of = {t["task_id"]: t.get("stages") or [] for t in row.get("tasks") or []}
    id_w = max((len(t) for t in by_task), default=0)
    lines: list[str] = []
    for tid in sorted(by_task):
        bucket = by_task[tid]
        cell = aggregate_cost_cell(bucket["cost_usd"], bucket["unmetered_calls"], bucket["calls"])
        cells = [
            f"{s['stage'].upper()} {c}" for s in stages_of.get(tid, []) if (c := _stage_cost(s))
        ]
        prefix = f"    {tid:<{id_w}}  {cell:<16}"
        if not cells:
            lines.append(prefix.rstrip())
            continue
        lines.extend(_wrap_cells(prefix, cells, indent=len(prefix), width=width))
    return lines


def _cost(header: dict, runs: list[dict], *, width: int, compact: bool) -> list[str]:
    lines = [f"  {_spend(header)}", f"  {_usage(header.get('usage'), compact=compact)}"]
    # The board is the one place two pricing regimes get added together. Say it rather
    # than letting a ~20x-overstated legacy run inflate a total silently.
    legacy_runs = header.get("legacy_accounting_runs") or 0
    if legacy_runs:
        lines.append(
            f"  warning: {legacy_runs} run(s) priced under the pre-#350 regime "
            f"({header.get('legacy_accounting_rows') or 0} row(s)) — overstated ~20x, "
            f"not comparable with the rest; the total above is inflated by them"
        )
    readable = [r for r in runs if not (r.get("degraded") or r.get("unreadable"))]
    run_w = max((len(r["run_id"]) for r in readable), default=0)
    for row in readable:
        lines.append(_run_cost_line(row, run_w=run_w, compact=compact))
        if not compact:
            lines.extend(_task_cost_lines(row, width=width))
    return lines


# --- recent -------------------------------------------------------------------------------


def _recent(header: dict, runs: list[dict], *, width: int, compact: bool) -> list[str]:
    """The board's newest events across every shown run, oldest first, as sentences."""
    events = [
        (ev.get("ts") or "", row["run_id"], ev)
        for row in runs
        for ev in row.get("recent_events") or []
    ]
    events.sort(key=lambda e: e[0])
    picked = events[-(_RECENT_COMPACT if compact else _RECENT_WIDE):]
    if not picked:
        return ["  (no events yet)"]
    now = _iso_epoch(header.get("generated_at"))
    run_w = max(len(run) for _, run, _ in picked)
    lines: list[str] = []
    for ts, run, ev in picked:
        then = _iso_epoch(ts)
        age = f"{_fmt_age(max(0.0, now - then))} ago" if now and then else "?"
        lines.append(_clip(f"  {age:>7}  {run:<{run_w}}  {ev.get('sentence') or ''}", width))
    return lines


# --- the board ----------------------------------------------------------------------------


def render_board(snapshot: dict, *, width: int | None = None) -> str:
    """The console board for ``snapshot`` at ``width`` columns (None → the wide board)."""
    compact = width is not None and width < WIDE_MIN_COLUMNS
    cols = width or _DEFAULT_WIDTH
    header, runs = snapshot["header"], snapshot["runs"]
    lines = [_headline(header)]
    if snapshot["attention"]:
        lines += _section("needs you", _needs_you(snapshot, compact=compact))
    lines += _section("running now", _running_now(runs, width=cols, compact=compact))
    lines += _section("progress", _progress(runs, width=cols, compact=compact))
    lines += _section("cost", _cost(header, runs, width=cols, compact=compact))
    lines += _section("recent", _recent(header, runs, width=cols, compact=compact))
    return "\n".join(lines)
