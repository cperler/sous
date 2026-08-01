"""Cross-session dashboard (#6): ONE attention-first surface over ALL runs.

The single-run ``orchestrator watch`` polls one run to terminal. The gap this closes is
the *cross-session* view the old bash ``monitor-orchestrator.sh`` had: a unified board of
every orchestrator — multiple concurrent schedulers plus historical runs — so a human can
answer "what needs me?" at a glance instead of digging per run.

Everything here is pure data assembly + plain-text rendering over data that already exists:
each run lives in its own ``runs/<run_id>/`` StatusStore root (``status-<run_id>.json`` +
``events.jsonl`` + ``stage-costs.jsonl`` + ``stages/``), and ``Engine.status`` already
computes per-task state / staleness / in-flight activity + the cost + budget blocks. This
module discovers the run dirs, folds each run's ``status()`` into a compact row, lifts the
"needs a human" items (blocked-on-human, supervisor-parked, paused, stale,
budget-exhausted, unreadable) into a
top ATTENTION band, and renders it.

The engine is never touched for model work: the dashboard only *reads*. The ``Engine`` used
per run is built by an injected ``engine_factory`` (the CLI supplies a real one; tests supply
a FakeProject one) so the assembly is drivable off tmp runs, and the clock / usage probe are
injected too so a snapshot is deterministic and a probe failure can never break the board.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, Unpack

from .alerting import _fmt_activity
from .render import aggregate_cost_cell
from .schemas.enums import TERMINAL_RUN_STATES
from .stream_probe import find_current_stream

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .engine import Engine

# Terminal run states as bare strings (this module works off the JSON snapshot, not enums).
_TERMINAL = {s.value for s in TERMINAL_RUN_STATES}

# Attention severity rank — lower sorts higher in the "needs you" band. Human-gated items
# (approve/reject, unpause) rank above the softer stall/unreadable signals.
_ATTENTION_RANK = {
    "blocked_on_human": 0,
    "parked": 1,
    "paused": 2,
    "budget_exhausted": 3,
    "unreadable": 4,
    "stale": 5,
}


# --- run discovery ----------------------------------------------------------------------


@dataclass(frozen=True)
class _RunLoc:
    """Where a run lives on disk: its id, its StatusStore root (the ``runs/<id>/`` subdir),
    the run-doc mtime (recency key), and whether its status doc is unreadable/corrupt."""

    run_id: str
    root: Path
    mtime: float
    unreadable: bool


def _discover(root: str | Path) -> list[_RunLoc]:
    """Every run directory under ``root`` (each a ``runs/<id>/`` StatusStore root), newest
    first by run-doc mtime. A subdir with status files but no readable ``document_type=="run"``
    doc is surfaced as an UNREADABLE run rather than dropped — a corrupt run must not vanish."""
    root = Path(root)
    locs: list[_RunLoc] = []
    if not root.is_dir():
        return locs
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        candidates = sorted(child.glob("status-*.json"))
        if not candidates:
            continue  # not a run dir
        run_id: str | None = None
        mtime = child.stat().st_mtime
        for c in candidates:
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("document_type") == "run":
                run_id = str(data.get("run_id") or child.name)
                with contextlib.suppress(OSError):  # pragma: no cover - defensive
                    mtime = c.stat().st_mtime
                break
        if run_id is not None:
            locs.append(_RunLoc(run_id, child, mtime, unreadable=False))
        else:
            # Status files present but no readable run doc → unreadable run (best-effort id
            # = the subdir name, which is the run id by the runs/<id>/ convention).
            locs.append(_RunLoc(child.name, child, mtime, unreadable=True))
    locs.sort(key=lambda loc: loc.mtime, reverse=True)
    return locs


def discover_runs(root: str | Path) -> list[str]:
    """All run ids under the runs ``root`` (active and terminal), most-recently-active first
    (run-doc status-file mtime). Unreadable run dirs are included."""
    return [loc.run_id for loc in _discover(root)]


# --- default engine factory (production) ------------------------------------------------


def default_engine_factory(
    project_spec: str | None,
    *,
    mode: str = "interactive",
    provider: str | None = None,
) -> Callable[[Path], Engine]:
    """A per-run ``Engine`` builder for the CLI: given a run's StatusStore root, wire an
    Engine on it exactly like ``orchestrator status`` would. The dashboard only calls
    ``engine.status``, so no interactive/model wiring runs — but the registry is built so the
    lane audit inside ``status`` still resolves. Imported lazily to avoid an import cycle."""
    from .cost_ledger import CostLedger
    from .engine import Engine
    from .lane_loader import build_registry
    from .project_loader import load_project
    from .routing import Router
    from .schemas.enums import ExecutionMode, Provider
    from .status_store import StatusStore

    if project_spec is None:
        raise SystemExit("dashboard needs --project to build the per-run lane registry")
    project = load_project(project_spec)
    exec_mode = ExecutionMode(mode)
    prov = Provider(provider) if provider else None
    schema_provider = getattr(project, "schema_for", None)

    def factory(run_root: Path) -> Engine:
        store = StatusStore(run_root)
        ledger = CostLedger(run_root / "stage-costs.jsonl")
        registry = build_registry(
            include_interactive=False,  # read-only board: never build interactive lanes
            headless_schema_provider=schema_provider,
            codex_schema_provider=schema_provider,
            setup_project=project,
            run_log_root=run_root,
        )
        router = Router(execution_mode=exec_mode, orchestrator_provider=prov)
        return Engine(store, ledger, project, router=router, registry=registry)

    return factory


# --- per-run row assembly ---------------------------------------------------------------


def _reasons_from_events(events: list[dict]) -> tuple[str | None, dict[str, str | None]]:
    """(last pause reason, {task_id: last blocked reason}) mined from a run's events.jsonl —
    the durable place ``run_paused`` / ``task_blocked`` reasons are recorded. Later rows win."""
    pause_reason: str | None = None
    blocked: dict[str, str | None] = {}
    for ev in events:
        etype = ev.get("type")
        kind = ev.get("kind")
        if etype == "run_paused" or kind == "run_paused":
            pause_reason = ev.get("reason") or pause_reason
        if etype == "task_blocked" or kind == "task_blocked":
            tid = ev.get("task_id")
            if tid:
                blocked[tid] = ev.get("reason") or ev.get("blocked_reason") or blocked.get(tid)
    return pause_reason, blocked


def _read_events(engine: Engine, run_id: str) -> list[dict]:
    """A run's events, best-effort: a corrupt/partial events.jsonl must never break the board."""
    try:
        return engine.store.read_events(run_id)
    except Exception:  # noqa: BLE001 - the dashboard tolerates a garbled sidecar
        return []


def _last_event_age_s(events: list[dict], *, now_epoch: float) -> float | None:
    """Seconds since the newest event's timestamp (chronological append order), or None."""
    for ev in reversed(events):
        ts = ev.get("ts")
        if not ts:
            continue
        try:
            return round(max(0.0, now_epoch - datetime.fromisoformat(ts).timestamp()), 1)
        except (ValueError, TypeError):
            return None
    return None


def _run_row(
    engine: Engine,
    loc: _RunLoc,
    *,
    stale_after_s: int,
    include_activity: bool,
    now_epoch: float,
) -> dict:
    """Fold one run's ``status()`` into a compact dashboard row. An unreadable run (or one
    whose ``status()`` raises on a partial doc) yields an ``<unreadable>`` row — never a crash."""
    base = {
        "run_id": loc.run_id,
        "root": str(loc.root),
        "mtime": loc.mtime,
        "unreadable": False,
        "attention": False,
        "flags": [],
        "inflight": [],
        "attention_items": [],
    }
    if loc.unreadable:
        return {
            **base,
            "unreadable": True,
            "state": "<unreadable>",
            "attention": True,
            "flags": ["unreadable status"],
            "progress": {},
            "cost_usd": None,
            "unmetered_calls": None,
            "total_invocations": None,
            "budget": None,
            "last_event_age_s": None,
            "attention_items": [{"kind": "unreadable", "run_id": loc.run_id}],
        }
    try:
        status = engine.status(
            loc.run_id, stale_after_s=stale_after_s, include_activity=include_activity
        )
    except Exception:  # noqa: BLE001 - a partial/corrupt task doc becomes an unreadable row
        return {
            **base,
            "unreadable": True,
            "state": "<unreadable>",
            "attention": True,
            "flags": ["unreadable status"],
            "progress": {},
            "cost_usd": None,
            "unmetered_calls": None,
            "total_invocations": None,
            "budget": None,
            "last_event_age_s": None,
            "attention_items": [{"kind": "unreadable", "run_id": loc.run_id}],
        }

    state = str(status.get("run_state"))
    progress = status.get("progress") or {}
    tasks: dict[str, dict] = status.get("tasks") or {}
    cost = status.get("cost") or {}
    budget = status.get("budget")
    events = _read_events(engine, loc.run_id)
    pause_reason, blocked_reasons = _reasons_from_events(events)

    flags: list[str] = []
    attention_items: list[dict] = []

    # In-flight: the running task(s) + current stage + a live activity line. The activity
    # detail only exists for a headless/codex stream; an interactive stage shows "working".
    inflight: list[dict] = []
    stale_tasks: list[str] = []
    blocked_tasks: list[str] = []
    for tid in sorted(tasks):
        ts = tasks[tid]
        tstate = ts.get("state")
        stage = ts.get("current_stage")
        if ts.get("stale"):
            stale_tasks.append(tid)
        if tstate == "blocked_on_human":
            blocked_tasks.append(tid)
        if tstate == "running" and stage:
            act = ts.get("activity")
            detail = _fmt_activity(act.get("current_activity")) if act else "working"
            # Whether a tailable provider stream exists for this in-flight stage RIGHT NOW.
            # False on the interactive×claude / ENGINE lanes (stages run in-session, nothing
            # tees `claude -p` / `codex exec` stdout to a `.stream.jsonl`) — so the web
            # dashboard can suppress its "live stream" affordance instead of advertising a
            # panel that can only ever render "(no live provider stream)" (#137). A cheap
            # dir-glob, run only for the handful of running tasks.
            stream_available = find_current_stream(engine.store.root, tid, stage) is not None
            inflight.append(
                {
                    "task_id": tid,
                    "stage": stage,
                    "line": f"{stage}: {detail}",
                    "seconds_since_update": ts.get("seconds_since_update"),
                    "stream_available": stream_available,
                }
            )

    for tid in blocked_tasks:
        reason = blocked_reasons.get(tid) or "held at human gate"
        flags.append(f"blocked_on_human:{tid}")
        attention_items.append(
            {"kind": "blocked_on_human", "run_id": loc.run_id, "task_id": tid, "reason": reason}
        )
    if state == "paused":
        reason = pause_reason or "paused"
        flags.append("paused")
        attention_items.append({"kind": "paused", "run_id": loc.run_id, "reason": reason})
    if state == "parked":
        parked = status.get("supervisor_parked") or {}
        reason = parked.get("reason") or "needs a fresh supervisor"
        flags.append("parked:needs-fresh-supervisor")
        attention_items.append(
            {
                "kind": "parked",
                "run_id": loc.run_id,
                "reason": reason,
                "resume_command": parked.get("resume_command"),
            }
        )
    if budget and budget.get("exhausted"):
        flags.append("budget-exhausted")
        attention_items.append(
            {"kind": "budget_exhausted", "run_id": loc.run_id, "fraction": budget.get("fraction")}
        )
    for tid in stale_tasks:
        secs = tasks[tid].get("seconds_since_update")
        stage = tasks[tid].get("current_stage")
        flags.append(f"stale:{tid}")
        attention_items.append(
            {
                "kind": "stale",
                "run_id": loc.run_id,
                "task_id": tid,
                "seconds_since_update": secs,
                "stage": stage,
            }
        )

    return {
        **base,
        "state": state,
        "terminal": state in _TERMINAL,
        "progress": progress,
        "inflight": inflight,
        "cost_usd": round(float(cost.get("total_cost_usd") or 0.0), 4),
        # #331: `cost` is `ledger.summary()`, whose total sums unmetered rows in at their
        # 0.0. Carry the unmetered/total call counts onto the row so every consumer (the
        # text board, the web skin) can qualify the figure instead of printing a confident
        # dollar amount for a run whose spend is partly or wholly unknown.
        "unmetered_calls": int(cost.get("unmetered_calls") or 0),
        "total_invocations": int(cost.get("total_invocations") or 0),
        "budget": budget,
        "last_event_age_s": _last_event_age_s(events, now_epoch=now_epoch),
        "flags": flags,
        "attention": bool(attention_items),
        "attention_items": attention_items,
    }


# --- the snapshot -----------------------------------------------------------------------


class DashboardSnapshotKwargs(TypedDict, total=False):
    """Optional keyword arguments accepted by :func:`dashboard_snapshot`."""

    stale_after_s: int
    include_activity: bool
    limit: int
    show_all: bool
    recent_terminal: int
    engine_factory: Callable[[Path], Engine] | None
    usage_reader: Callable[[], object] | None
    clock: Callable[[], float]


def _read_usage(usage_reader: Callable[[], object] | None) -> dict | None:
    """Best-effort usage header: never block/raise. Returns a small dict or None."""
    if usage_reader is None:
        return None
    try:
        usage = usage_reader()
    except Exception:  # noqa: BLE001 - a probe miss must never break the dashboard
        return None
    if usage is None:
        return None
    # Duck-typed: a usage_probe.Usage dataclass, or already a dict.
    if isinstance(usage, dict):
        return usage
    return {
        "five_hour_pct": getattr(usage, "five_hour_pct", None),
        "seven_day_pct": getattr(usage, "seven_day_pct", None),
        "five_hour_resets_at": getattr(usage, "five_hour_resets_at", ""),
    }


def dashboard_snapshot(
    root: str | Path,
    *,
    stale_after_s: int = 1800,
    include_activity: bool = True,
    limit: int = 20,
    show_all: bool = False,
    recent_terminal: int = 5,
    engine_factory: Callable[[Path], Engine] | None = None,
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Assemble the whole-board snapshot: a global header + a sorted ATTENTION band + the
    per-run rows, all attention-first.

    Selection: every discovered run is folded into a row (recency order). By default the board
    shows all non-terminal runs plus the ``recent_terminal`` most-recent terminal runs; with
    ``show_all`` it shows everything. The kept rows are then sorted attention-first (anything
    needing a human above healthy runs, ties by recency) and truncated to ``limit``.

    ``engine_factory(run_root) -> Engine`` builds the read-only engine per run (required —
    the CLI passes ``default_engine_factory``). ``usage_reader`` / ``clock`` are injected so a
    probe failure can't break the board and the snapshot is deterministic in tests.
    """
    if engine_factory is None:
        raise ValueError("dashboard_snapshot requires an engine_factory")
    now_epoch = clock()
    locs = _discover(root)
    rows: list[dict] = []
    for loc in locs:
        engine = engine_factory(loc.root)
        rows.append(
            _run_row(
                engine,
                loc,
                stale_after_s=stale_after_s,
                include_activity=include_activity,
                now_epoch=now_epoch,
            )
        )

    total_discovered = len(rows)

    # Selection: non-terminal always; terminal capped unless show_all. Unreadable rows are
    # non-terminal (state unknown → always shown — a corrupt run needs eyes).
    if show_all:
        kept = list(rows)
    else:
        kept = []
        terminal_seen = 0
        for row in rows:  # rows are already recency-sorted (newest first)
            if row.get("terminal") and not row.get("unreadable"):
                if terminal_seen < recent_terminal:
                    kept.append(row)
                    terminal_seen += 1
            else:
                kept.append(row)

    # Attention-first: needs-a-human rows above healthy ones, ties by recency (newest first).
    kept.sort(key=lambda r: (not r["attention"], -r["mtime"]))
    shown = kept[: limit if limit and limit > 0 else None]

    # Flatten the attention band across shown rows, ranked by severity then recency.
    attention: list[dict] = []
    for row in shown:
        for item in row["attention_items"]:
            attention.append({**item, "_mtime": row["mtime"]})
    attention.sort(key=lambda it: (_ATTENTION_RANK.get(it["kind"], 9), -it.pop("_mtime")))

    # Header counts + spend over the SHOWN rows. The unmetered/total call counts are summed
    # alongside the dollars (#331) so the board-wide figure can be qualified the same way a
    # single run's is: unmetered calls contribute $0 to `total_spend_usd`, making it a floor.
    counts: dict[str, int] = {}
    total_spend = 0.0
    unmetered_calls = 0
    total_invocations = 0
    for row in shown:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
        if isinstance(row.get("cost_usd"), (int, float)):
            total_spend += row["cost_usd"]
        unmetered_calls += row.get("unmetered_calls") or 0
        total_invocations += row.get("total_invocations") or 0

    header = {
        "generated_at": datetime.fromtimestamp(now_epoch, tz=UTC).isoformat(),
        "usage": _read_usage(usage_reader),
        "total_spend_usd": round(total_spend, 4),
        "unmetered_calls": unmetered_calls,
        "total_invocations": total_invocations,
        "counts": counts,
        "shown": len(shown),
        "total_discovered": total_discovered,
        "attention_count": len(attention),
        "all_quiet": len(attention) == 0,
        "running": counts.get("running", 0),
        "paused": counts.get("paused", 0),
    }
    return {"header": header, "attention": attention, "runs": shown}


# --- rendering --------------------------------------------------------------------------


def _state_icon(state: str) -> str:
    return {
        "running": "*",
        "paused": "!",
        "parked": "!",
        "completed": "+",
        "completed_with_rejections": "~",
        "superseded": "=",  # #257: retired by a human, not run to a conclusion
        "failed": "x",
        "pending": "-",
        "<unreadable>": "?",
    }.get(state, "-")


def _progress_str(progress: dict) -> str:
    total = progress.get("total", 0)
    # "done" = every terminal, non-pending-on-a-human outcome: shipped, deliberately
    # closed (#67), or retired as superseded (#257). A terminal task left out here reads
    # as still-outstanding work on the board it will never leave.
    done = (
        progress.get("completed", 0)
        + progress.get("closed_infeasible", 0)
        + progress.get("superseded", 0)
    )
    return f"{done}/{total}"


def _fmt_age(secs: float | None) -> str:
    if secs is None:
        return "?"
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs // 60)}m"
    return f"{int(secs // 3600)}h"


def _render_attention_item(item: dict) -> str:
    kind = item["kind"]
    run = item["run_id"]
    if kind == "blocked_on_human":
        return f"  ! [{run}] {item['task_id']} BLOCKED_ON_HUMAN — {item.get('reason')}"
    if kind == "paused":
        return f"  ! [{run}] PAUSED — {item.get('reason')}"
    if kind == "parked":
        command = item.get("resume_command") or "start a fresh interactive supervisor"
        reason = item.get("reason") or "supervisor context exhausted"
        return f"  ! [{run}] PARKED — needs a fresh supervisor ({reason}); resume: {command}"
    if kind == "budget_exhausted":
        frac = item.get("fraction")
        pct = f"{frac * 100:.0f}%" if isinstance(frac, (int, float)) else "?"
        return f"  ! [{run}] BUDGET EXHAUSTED — metered spend at {pct} of budget"
    if kind == "stale":
        secs = item.get("seconds_since_update")
        ago = _fmt_age(secs) if isinstance(secs, (int, float)) else "?"
        return f"  ! [{run}] {item.get('task_id')} STALE — no update for {ago} (stage {item.get('stage')})"
    if kind == "unreadable":
        return f"  ! [{run}] UNREADABLE status — inspect runs/{run}/ by hand"
    return f"  ! [{run}] {kind}"  # pragma: no cover - defensive


def render_dashboard(snapshot: dict) -> str:
    """Render a snapshot to a compact, plain-text board: a global header, a top "needs you"
    band (empty → an all-quiet line), then one line per run with indented in-flight lines."""
    header = snapshot["header"]
    attention = snapshot["attention"]
    runs = snapshot["runs"]
    lines: list[str] = []

    # --- header ---
    counts = header["counts"]
    if header["all_quiet"]:
        done = (
            counts.get("completed", 0)
            + counts.get("completed_with_rejections", 0)
            + counts.get("superseded", 0)  # #257
        )
        lines.append(f"ALL QUIET — {header['running']} running, {done} done")
    else:
        lines.append(f"ATTENTION — {header['attention_count']} item(s) need you")

    usage = header.get("usage")
    if usage and usage.get("five_hour_pct") is not None:
        lines.append(
            f"usage: 5h {usage['five_hour_pct']:.0f}% / 7d "
            f"{usage.get('seven_day_pct') or 0:.0f}%"
        )
    else:
        lines.append("usage: unavailable")

    state_bits = " ".join(f"{s}={n}" for s, n in sorted(counts.items()))
    # #331: unmetered calls sum into total_spend_usd at $0, so an unqualified figure would
    # understate the board's real spend while looking exact. Same honesty rule as
    # cost-summary.md, in one header-width phrase.
    unmetered = header.get("unmetered_calls") or 0
    invocations = header.get("total_invocations") or 0
    if unmetered and invocations and unmetered >= invocations:
        spend_str = f"n/a — all {unmetered} call(s) unmetered"
    elif unmetered:
        spend_str = (
            f"≥${header['total_spend_usd']:.4f} "
            f"({unmetered} unmetered call(s) of unknown cost excluded)"
        )
    else:
        spend_str = f"${header['total_spend_usd']:.4f}"
    lines.append(f"spend: {spend_str} across {header['shown']} run(s)  |  {state_bits}")

    # --- attention band ---
    if attention:
        lines.append("")
        lines.append("── needs you ──")
        lines.extend(_render_attention_item(it) for it in attention)

    # --- runs ---
    lines.append("")
    lines.append("── runs ──")
    if not runs:
        lines.append("  (no runs found)")
    for row in runs:
        icon = _state_icon(row["state"])
        if row.get("unreadable"):
            lines.append(f"  {icon} {row['run_id']:<24} <unreadable status>")
            continue
        # #331: `≥$X` / `n/a (unmetered)` rather than a bare figure when this run's spend is
        # partly or wholly unknown; `$?` stays the marker for "no cost data at all".
        cost = row.get("cost_usd")
        cost_str = (
            aggregate_cost_cell(
                cost, row.get("unmetered_calls") or 0, row.get("total_invocations") or 0
            )
            if isinstance(cost, (int, float))
            else "$?"
        )
        flags = f"  [{', '.join(row['flags'])}]" if row["flags"] else ""
        age = _fmt_age(row.get("last_event_age_s"))
        lines.append(
            f"  {icon} {row['run_id']:<24} {row['state']:<26} "
            f"{_progress_str(row['progress']):>7}  {cost_str:>10}{flags}  (last {age} ago)"
        )
        for inf in row["inflight"]:
            lines.append(f"      {inf['task_id']} · {inf['line']}")
    return "\n".join(lines)


# --- watch loop -------------------------------------------------------------------------


def render_watch(
    root: str | Path,
    *,
    emit: Callable[[str], None],
    sleeper: Callable[[float], None],
    clear: Callable[[], None] | None = None,
    interval: float = 30,
    max_iters: int | None = None,
    **snapshot_kwargs: Unpack[DashboardSnapshotKwargs],
) -> None:
    """Clear-screen + reprint the board every ``interval`` seconds until interrupted.

    Mirrors the ``watch``/``tail --follow`` injected-sleeper pattern so it is drivable without
    real sleeping: ``sleeper`` is ``time.sleep`` in production and a stub in tests, and a
    ``KeyboardInterrupt`` from anywhere (Ctrl-C, or a test sleeper that raises to stop) ends the
    loop cleanly. ``max_iters`` bounds it for tests; ``clear`` defaults to an ANSI clear."""
    if clear is None:
        clear = lambda: emit("\x1b[2J\x1b[H")  # noqa: E731 - tiny ANSI clear
    iters = 0
    try:
        while max_iters is None or iters < max_iters:
            clear()
            emit(render_dashboard(dashboard_snapshot(root, **snapshot_kwargs)))
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
            sleeper(interval)
    except KeyboardInterrupt:
        pass
