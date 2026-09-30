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
budget-exhausted, unreadable, adapter-unresolved) into a top ATTENTION band — each carrying
the commands that resolve it — and renders it. Each row also carries the per-task, driver,
cost-breakdown and recent-event detail both the console and the web view read (#525).

The engine is never touched for model work: the dashboard only *reads*. The ``Engine`` used
per run is built by an injected ``engine_factory`` (the CLI supplies a real one; tests supply
a FakeProject one) so the assembly is drivable off tmp runs, and the clock / usage probe are
injected too so a snapshot is deterministic and a probe failure can never break the board.

The board spans ROOTS and PROJECTS, not just runs (#386). Two projects running batches at
once keep their runs under their own ``<project>/runs/`` and drive them through different
project adapters, so every entry point here accepts several runs-roots and the engine
factory resolves each row's adapter from the ``project_ref`` persisted on that run's own
doc. Selection, the attention-first sort and the header stay GLOBAL across roots — a run
needing a human outranks a healthy run in another project — and the utilization probe is
labelled as the account-wide figure it has always been, now that several projects visibly
share it. A run whose adapter will not resolve degrades to one marked row.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias, TypedDict, Unpack

from .alerting import _driver_down_reason, _fmt_activity, release_commands
from .driver_log import (
    STATE_DISPATCHING,
    STATE_PLANNING,
    STATE_WAITING_CAPACITY,
    STATE_WAITING_COOLDOWN,
)
from .render import aggregate_cost_cell
from .schemas.enums import TERMINAL_RUN_STATES, TERMINAL_TASK_STATES
from .stream_probe import find_current_stream

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .engine import Engine
    from .schemas.status import Task

#: One runs-root, or several (#386). Every public entry point here takes either, so a
#: single-root caller reads exactly as it did before.
Roots: TypeAlias = "str | Path | Sequence[str | Path]"

#: Builds the read-only ``Engine`` for ONE run: given that run's StatusStore root and the
#: project adapter ref persisted on its own run doc (``Run.project_ref``, None for a
#: pre-#386 doc), return an Engine wired to it. Taking the ref as an argument is what lets
#: one board render runs from several projects — the resolution is per run, not per board.
EngineFactory = Callable[[Path, "str | None"], "Engine"]

# Terminal run states as bare strings (this module works off the JSON snapshot, not enums).
_TERMINAL = {s.value for s in TERMINAL_RUN_STATES}
_TERMINAL_TASK = {s.value for s in TERMINAL_TASK_STATES}

#: A live provider stream that has not grown for this long reads as STALLED rather than
#: active (#525). The same default ``watch --activity`` uses (``alerting.activity_lines``).
DEFAULT_STALL_AFTER_S = 300

#: How many of a run's newest events a row carries as readable sentences (#525).
DEFAULT_RECENT_EVENTS = 10

# Attention severity rank — lower sorts higher in the "needs you" band. Human-gated items
# (approve/reject, unpause) rank above the softer stall/unreadable signals.
_ATTENTION_RANK = {
    "blocked_on_human": 0,
    "parked": 1,
    "paused": 2,
    "budget_exhausted": 3,
    "unreadable": 4,
    # #386: a run whose project adapter will not load is as unreadable as a corrupt doc —
    # the board can name it but can say nothing about its state.
    "adapter_unresolved": 5,
    "stale": 6,
}


# --- run discovery ----------------------------------------------------------------------


@dataclass(frozen=True)
class _RunLoc:
    """Where a run lives on disk: its id, its StatusStore root (the ``runs/<id>/`` subdir),
    the run-doc mtime (recency key), whether its status doc is unreadable/corrupt, and the
    project adapter it was created with (#386 — ``Run.project_ref``, None for a run doc
    written before that field existed or an unreadable one). The ref is lifted here, from
    the parse discovery already does, so resolving a row's adapter costs no second read."""

    run_id: str
    root: Path
    mtime: float
    unreadable: bool
    project_ref: str | None = None


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
        project_ref: str | None = None
        mtime = child.stat().st_mtime
        for c in candidates:
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("document_type") == "run":
                run_id = str(data.get("run_id") or child.name)
                ref = data.get("project_ref")
                project_ref = str(ref) if ref else None
                with contextlib.suppress(OSError):  # pragma: no cover - defensive
                    mtime = c.stat().st_mtime
                break
        if run_id is not None:
            locs.append(_RunLoc(run_id, child, mtime, unreadable=False, project_ref=project_ref))
        else:
            # Status files present but no readable run doc → unreadable run (best-effort id
            # = the subdir name, which is the run id by the runs/<id>/ convention).
            locs.append(_RunLoc(child.name, child, mtime, unreadable=True))
    locs.sort(key=lambda loc: loc.mtime, reverse=True)
    return locs


def normalize_roots(root: Roots) -> list[Path]:
    """One or many runs-roots as an ordered, de-duplicated list of paths (#386).

    The board spans ROOTS as well as runs: two projects running batches at once keep their
    runs under their own ``<project>/runs/``. A bare path stays a one-element list, so every
    single-root caller is unchanged. De-duplication is by RESOLVED path, so passing the same
    root twice (or by two spellings) does not double every row."""
    raw = [root] if isinstance(root, (str, Path)) else list(root)
    seen: set[Path] = set()
    out: list[Path] = []
    for item in raw:
        path = Path(item)
        try:
            key = path.resolve()
        except OSError:  # pragma: no cover - defensive (unresolvable path)
            key = path
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _discover_all(root: Roots) -> list[_RunLoc]:
    """Every run under EVERY given runs-root, merged into one recency-ordered list. The
    merge is global on purpose: the board's attention-first ordering must not be grouped by
    root, because a run needing a human outranks a healthy run in another project."""
    locs: list[_RunLoc] = []
    for one in normalize_roots(root):
        locs.extend(_discover(one))
    locs.sort(key=lambda loc: loc.mtime, reverse=True)
    return locs


def resolve_run_root(root: Roots, run_id: str, *, prefer_root: str | None = None) -> Path | None:
    """The StatusStore root for ``run_id``, or None when the board cannot name exactly one.

    With several runs-roots (#386), ``<root>/<run_id>`` is no longer a safe join: the run may
    live under any of them, and two roots may even hold the same run id. Resolve by the same
    discovery the board uses, and when more than one root matches let the caller disambiguate
    with ``prefer_root`` (the web page sends the row's own ``root``, which it already has).
    Returning None instead of a guessed path is what makes a wrong-root read impossible."""
    matches = [loc for loc in _discover_all(root) if loc.run_id == run_id]
    if prefer_root is not None:
        matches = [loc for loc in matches if str(loc.root) == prefer_root]
    return matches[0].root if len(matches) == 1 else None


def discover_runs(root: Roots) -> list[str]:
    """All run ids under the runs ``root`` (active and terminal), most-recently-active first
    (run-doc status-file mtime). Unreadable run dirs are included. Accepts several roots."""
    return [loc.run_id for loc in _discover_all(root)]


# --- default engine factory (production) ------------------------------------------------


class AdapterUnresolved(Exception):
    """No project adapter could be resolved for a run (#386): its ``project_ref`` names an
    adapter that will not load, or the doc predates the field and no ``--project`` fallback
    was given. Raised by the engine factory and caught by ``dashboard_snapshot``, which
    degrades that one run to a clearly-marked reduced row — one unreadable run must never
    blank a board that is now the whole machine's view."""


def default_engine_factory(
    project_spec: str | None,
    *,
    mode: str = "interactive",
    provider: str | None = None,
) -> EngineFactory:
    """A per-run ``Engine`` builder for the CLI: given a run's StatusStore root and the
    project adapter ref persisted on its run doc, wire an Engine on it exactly like
    ``orchestrator status`` would. The dashboard only calls ``engine.status``, so no
    interactive/model wiring runs — but the registry is built so the lane audit inside
    ``status`` still resolves. Imported lazily to avoid an import cycle.

    The adapter is resolved PER RUN (#386): a board spanning several runs-roots holds runs
    from several projects, and rendering them all through one ``--project`` would attribute
    every row to whichever adapter happened to be passed. A run doc with no ref (written
    before the field existed) falls back to ``project_spec``; with neither, resolution
    raises and the caller degrades that ONE row rather than the board. Adapters are cached
    per spec, so N runs of one project load it once.

    Resolution goes through ``project_loader`` by name/path only — the engine never imports
    ``adapters`` (#273); this is the same seam ``--project`` already uses.
    """
    from .cost_ledger import CostLedger
    from .engine import Engine
    from .lane_loader import build_registry
    from .ports.project import ProjectConfig
    from .project_loader import load_engine_task_source, load_project, normalize_project_ref
    from .routing import Router
    from .schemas.enums import ExecutionMode, Provider
    from .status_store import StatusStore

    fallback = normalize_project_ref(project_spec)
    exec_mode = ExecutionMode(mode)
    prov = Provider(provider) if provider else None
    meta_task_source = load_engine_task_source()
    cache: dict[str, ProjectConfig] = {}

    def resolve(project_ref: str | None) -> ProjectConfig:
        spec = normalize_project_ref(project_ref) or fallback
        if spec is None:
            raise AdapterUnresolved(
                "no project adapter for this run: its run doc predates `project_ref` "
                "(#386) and no --project fallback was given"
            )
        if spec not in cache:
            try:
                cache[spec] = load_project(spec)
            # load_project exits the process on an unknown/broken spec, which is right for
            # `--project` but fatal for a board where ONE stale ref must not blank the view.
            except (Exception, SystemExit) as exc:
                raise AdapterUnresolved(f"project adapter {spec!r} did not load: {exc}") from exc
        return cache[spec]

    def factory(run_root: Path, project_ref: str | None = None) -> Engine:
        project = resolve(project_ref)
        store = StatusStore(run_root)
        ledger = CostLedger(run_root / "stage-costs.jsonl")
        registry = build_registry(
            include_interactive=False,  # read-only board: never build interactive lanes
            headless_schema_provider=getattr(project, "schema_for", None),
            codex_schema_provider=getattr(project, "schema_for", None),
            setup_project=project,
            run_log_root=run_root,
        )
        router = Router(execution_mode=exec_mode, orchestrator_provider=prov)
        return Engine(
            store,
            ledger,
            project,
            meta_task_source=meta_task_source,
            router=router,
            registry=registry,
        )

    return factory


# --- per-run row assembly ---------------------------------------------------------------


def _reasons_from_events(
    events: list[dict],
) -> tuple[str | None, dict[str, str | None], dict[str, str]]:
    """(last pause reason, {task_id: last blocked reason}, {task_id: last blocked gate})
    mined from a run's events.jsonl — the durable place ``run_paused`` / ``task_blocked``
    reasons are recorded. Later rows win. The gate is what an ``approve --note`` names, so
    the board's release command matches the one the ``task_blocked`` alert carried (#409)."""
    pause_reason: str | None = None
    blocked: dict[str, str | None] = {}
    gates: dict[str, str] = {}
    for ev in events:
        etype = ev.get("type")
        kind = ev.get("kind")
        if etype == "run_paused" or kind == "run_paused":
            pause_reason = ev.get("reason") or pause_reason
        if etype == "task_blocked" or kind == "task_blocked":
            tid = ev.get("task_id")
            if tid:
                blocked[tid] = ev.get("reason") or ev.get("blocked_reason") or blocked.get(tid)
                if ev.get("gate"):
                    gates[tid] = str(ev["gate"])
    return pause_reason, blocked, gates


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


def _project_label(project_ref: str | None, project_name: str | None) -> str:
    """What to show in the row's project column (#386). The adapter's own ``name`` when the
    adapter loaded (``sous``, ``family-finance``) — that is what a human calls the project.
    Otherwise the persisted ref, shortened so a long directory spec does not eat the line —
    and shortened to the PROJECT dir, not the adapter dir, since every project's adapter is
    called ``.orchestration``. ``?`` when the run doc names no adapter at all."""
    if project_name:
        return project_name
    if not project_ref:
        return "?"
    if "/" not in project_ref:
        return project_ref  # module path or entry-point name — already short and meaningful
    path = Path(project_ref)
    # ``…/family-finance/.orchestration`` → ``family-finance``: the dotted adapter dir is the
    # same name in every project, so it identifies nothing on a cross-project board.
    return path.parent.name or path.name if path.name.startswith(".") else path.name


#: Longest exception message a degraded row keeps (#498). Enough to name the module, key or
#: path that failed; a multi-line traceback-sized message would swamp a board line.
_ERROR_MESSAGE_CAP = 240

UNREADABLE_STATE = "<unreadable>"
ADAPTER_UNRESOLVED_STATE = "<adapter-unresolved>"


def _error_info(exc: BaseException) -> dict:
    """The type and message of the exception that degraded a row (#498). Before this the
    catch discarded it, so a transient failure on a healthy run was undiagnosable after the
    fact. Only the first line of the message is kept, capped, so it fits a board line."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if len(message) > _ERROR_MESSAGE_CAP:
        message = message[: _ERROR_MESSAGE_CAP - 1] + "…"
    return {"type": type(exc).__name__, "message": message}


def _error_text(error: dict | None) -> str | None:
    """``Type: message`` for a retained error, or None."""
    if not error:
        return None
    return f"{error['type']}: {error['message']}" if error.get("message") else error["type"]


def _reduced_row(
    base: dict,
    *,
    flag: str,
    kind: str,
    state: str = UNREADABLE_STATE,
    detail: str | None = None,
    error: dict | None = None,
) -> dict:
    """A row for a run the board could not read or could not resolve an adapter for: the
    run stays VISIBLE (id, project, recency) with its state replaced by a marker, and it is
    lifted into the attention band. Never a crash and never a silent drop — with the board
    spanning every project on the machine, one bad run must cost one row, not the view.

    ``degraded`` is the common marker. ``unreadable`` is set ONLY for the unreadable kind
    (#498): a run whose adapter will not resolve has a perfectly readable run doc, and
    labelling it unreadable contradicted the adapter flag beside it. ``error`` keeps the
    caught exception's type and message so the row names the real cause."""
    item: dict = {"kind": kind, "run_id": base["run_id"], "commands": []}
    if detail:
        item["reason"] = detail
    if error:
        item["error"] = error
    return {
        **base,
        "unreadable": kind == "unreadable",
        "degraded": True,
        "error": error,
        "state": state,
        "attention": True,
        "flags": [flag],
        "progress": {},
        "cost_usd": None,
        "unmetered_calls": None,
        "total_invocations": None,
        "budget": None,
        "last_event_age_s": None,
        "attention_items": [item],
    }


def _base_row(loc: _RunLoc, *, project: str | None = None) -> dict:
    """The identity fields every row carries, readable or not."""
    return {
        "run_id": loc.run_id,
        "root": str(loc.root),
        "mtime": loc.mtime,
        # #386: which project this run belongs to. With rows from several projects on one
        # board the run id alone is ambiguous, so every row — including a degraded one —
        # answers "whose run is this?".
        "project": project if project is not None else _project_label(loc.project_ref, None),
        "project_ref": loc.project_ref,
        "unreadable": False,
        # #498: any reduced row (unreadable OR adapter-unresolved), plus the exception that
        # caused it. Distinct from ``unreadable``, which now names only the unreadable kind.
        "degraded": False,
        "error": None,
        "attention": False,
        "flags": [],
        "inflight": [],
        "attention_items": [],
        # #525 detail blocks — present (empty) on every row so a renderer never has to
        # test for the key, only for content.
        "tasks": [],
        "driver": None,
        "cost_breakdown": None,
        "recent_events": [],
    }


# --- attention commands (#525) ------------------------------------------------------------


def _cli_base(root: str, run_id: str) -> str:
    """``orchestrator --root <root> --run <id>``, shell-quoted — the prefix every command an
    attention item carries shares, spelled the same way ``alerting.release_commands`` does."""
    return f"orchestrator --root {shlex.quote(root)} --run {shlex.quote(run_id)}"


def _unpause_commands(root: str, run_id: str, *, budget: bool) -> list[dict[str, str]]:
    """The commands that release a PAUSED run. A budget pause gets the raise-budget form
    first, because a bare unpause drops the cap entirely — the operator should see both."""
    base = _cli_base(root, run_id)
    commands: list[dict[str, str]] = []
    if budget:
        commands.append(
            {
                "label": "raise the budget and resume (fill in the new ceiling)",
                "command": f"{base} unpause --raise-budget '<usd>'",
            }
        )
    commands.append(
        {
            "label": (
                "unpause — resume with no budget cap"
                if budget
                else "unpause — release the paused run once the cause is fixed"
            ),
            "command": f"{base} unpause",
        }
    )
    return commands


# --- driver summary (#525) ----------------------------------------------------------------


def _driver_summary(driver: dict | None, *, terminal: bool) -> dict:
    """The run's driver in plain words: alive (and what it is doing), sleeping out a capacity
    stall or rate-limit cooldown, dead (with the reason), or no driver at all.

    Keyed off ``status()``'s merged ``driver`` block (#313 claim + #323 heartbeat). An
    ``unclaimed`` run has no driver by design (the per-task CLI path, or not started yet),
    which ``liveness_from_log`` reports as ``alive=False`` — so it is classified before the
    alive verdict is consulted, or every CLI-driven run would read as a dead driver."""
    driver = driver or {}
    claim = driver.get("state")
    pid = driver.get("pid")
    out = {
        "state": "none",
        "summary": "",
        "reason": None,
        "pid": pid,
        "host": driver.get("host"),
        "heartbeat_age_s": driver.get("heartbeat_age_s"),
        "last_state": driver.get("last_state"),
    }
    alive = driver.get("alive")
    if terminal:
        out.update(state="finished", summary="run finished; no driver needed")
    elif claim in (None, "unclaimed"):
        out["summary"] = "no headless driver (driven task by task from the CLI, or not started)"
    elif claim == "foreign_host" or alive is None:
        out.update(
            state="unknown",
            summary=f"claimed from another host ({driver.get('host')}); cannot check it here",
        )
    elif alive is False:
        reason = (
            f"its process (pid {pid}) is gone"
            if claim == "dead" and not driver.get("exited")
            else _driver_down_reason(driver)
        )
        out.update(state="dead", summary=f"driver down: {reason}", reason=reason)
    else:
        last = driver.get("last_state")
        if last == STATE_WAITING_CAPACITY:
            out.update(state="capacity_wait", summary="alive, sleeping out a capacity stall")
        elif last == STATE_WAITING_COOLDOWN:
            out.update(state="cooldown_wait", summary="alive, waiting out a rate-limit cooldown")
        elif last == STATE_DISPATCHING:
            out.update(state="alive", summary="alive, running stages")
        elif last == STATE_PLANNING:
            out.update(state="alive", summary="alive, nothing ready to dispatch this pass")
        else:
            out.update(state="alive", summary="alive")
    return out


# --- cost breakdown (#525, #331) ----------------------------------------------------------


def _cost_bucket() -> dict:
    return {
        "cost_usd": 0.0,
        "calls": 0,
        "unmetered_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _add_row(bucket: dict, row: dict) -> None:
    bucket["cost_usd"] += float(row.get("cost_usd") or 0.0)
    bucket["calls"] += 1
    if row.get("metered") is False:
        bucket["unmetered_calls"] += 1
    bucket["input_tokens"] += int(row.get("input_tokens") or 0)
    bucket["output_tokens"] += int(row.get("output_tokens") or 0)


def _round_bucket(bucket: dict) -> dict:
    return {**bucket, "cost_usd": round(bucket["cost_usd"], 4)}


def _cost_breakdown(rows: list[dict]) -> tuple[dict, dict[tuple[str, str], dict]]:
    """Per-run cost split by task and by stage from the run's own ledger rows (#525), plus
    the (task, stage) buckets the per-task stage list reads.

    Every bucket carries its unmetered-call count next to the dollars (#331): an unmetered
    call sums in at $0, so each level — run, task, stage — needs its own count to say
    whether its figure is exact, a floor, or unknown. Token counts ride along because a
    zero-dollar interactive stage can still be compared by volume."""
    total = _cost_bucket()
    by_task: dict[str, dict] = {}
    by_stage: dict[str, dict] = {}
    by_task_stage: dict[tuple[str, str], dict] = {}
    for row in rows:
        tid = str(row.get("task_id") or "?")
        stage = str(row.get("stage") or "?")
        for bucket in (
            total,
            by_task.setdefault(tid, _cost_bucket()),
            by_stage.setdefault(stage, _cost_bucket()),
            by_task_stage.setdefault((tid, stage), _cost_bucket()),
        ):
            _add_row(bucket, row)
    breakdown = {
        **_round_bucket(total),
        "by_task": {k: _round_bucket(v) for k, v in by_task.items()},
        "by_stage": {k: _round_bucket(v) for k, v in by_stage.items()},
    }
    return breakdown, {k: _round_bucket(v) for k, v in by_task_stage.items()}


# --- per-task detail (#525) ---------------------------------------------------------------


def _iso_epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def _stream_activity(ts: dict, *, stall_after_s: int) -> dict:
    """The live-stream verdict for one task: ``active`` (grew within ``stall_after_s``),
    ``stalled`` (a stream exists but has not grown for that long), or ``none`` (no provider
    stream — an interactive/ENGINE-lane stage, or a task not in flight). Reuses the
    ``activity`` block ``status(include_activity=True)`` already computes. A stream with no
    readable timestamp counts as active: a stall alarm must rest on evidence."""
    act = ts.get("activity")
    if not act:
        return {"state": "none", "seconds_since_event": None, "line": None}
    since = act.get("seconds_since_event")
    stalled = isinstance(since, (int, float)) and since >= stall_after_s
    return {
        "state": "stalled" if stalled else "active",
        "seconds_since_event": since,
        "line": _fmt_activity(act.get("current_activity")),
        "events_seen": act.get("events_seen"),
    }


def _stage_progress(task: Task, stage_costs: dict, *, now_epoch: float) -> list[dict]:
    """One entry per stage of the task's OWN pipeline, in order: status, attempt, duration,
    and the ledger's cost for that stage summed over every attempt (the StageRecord only
    holds the latest attempt's). A stage with no ledger rows falls back to its record."""
    pipeline = task.pipeline or tuple(task.stages)
    out: list[dict] = []
    for stage in pipeline:
        rec = task.stages.get(stage)
        if rec is None:
            continue
        name = stage.value
        started = _iso_epoch(rec.started_at)
        ended = _iso_epoch(rec.completed_at)
        status = rec.status.value
        duration: float | None = None
        if started is not None and ended is not None:
            duration = round(max(0.0, ended - started), 1)
        elif started is not None and status == "running":
            duration = round(max(0.0, now_epoch - started), 1)
        ledger = stage_costs.get(name)
        if ledger is not None:
            cost: float | None = ledger["cost_usd"]
            metered = ledger["unmetered_calls"] == 0
            calls, unmetered = ledger["calls"], ledger["unmetered_calls"]
        else:
            cost, metered = rec.cost_usd, rec.metered
            calls, unmetered = 0, 0
        out.append(
            {
                "stage": name,
                "status": status,
                "attempt": rec.attempt,
                "model": rec.model,
                "cost_usd": cost,
                "metered": metered,
                "calls": calls,
                "unmetered_calls": unmetered,
                "duration_s": duration,
            }
        )
    return out


def _task_details(
    engine: Engine,
    run_id: str,
    tasks: dict[str, dict],
    *,
    graph: dict[str, list[str]],
    task_costs: dict[tuple[str, str], dict],
    blocked_reasons: dict[str, str | None],
    stall_after_s: int,
    now_epoch: float,
) -> list[dict]:
    """Per-task detail for a run row (#525): what the task is, where it is, how long it has
    been there, what it has cost, and what it is waiting on. ``tasks`` is ``status()``'s
    per-task block; the task doc is re-read for what that block omits (title, stage records).
    A task doc that will not load now (``status`` read it a moment ago) keeps the fields the
    status block has rather than dropping the task."""
    out: list[dict] = []
    for tid in sorted(tasks):
        ts = tasks[tid]
        state = ts.get("state")
        current = ts.get("current_stage")
        try:
            task: Task | None = engine.store.load_task(run_id, tid)
        except Exception:  # noqa: BLE001 - one unloadable doc must not blank the row
            task = None
        stage_costs = {stage: b for (t, stage), b in task_costs.items() if t == tid}
        stages = _stage_progress(task, stage_costs, now_epoch=now_epoch) if task else []
        current_rec = next((s for s in stages if s["stage"] == current), None)
        in_stage = (
            current_rec is not None
            and current_rec["status"] == "running"
            and state not in _TERMINAL_TASK
        )
        deps = list(graph.get(tid) or (task.depends_on if task else []) or [])
        waiting_on = (
            [d for d in deps if (tasks.get(d) or {}).get("state") != "completed"]
            if state not in _TERMINAL_TASK
            else []
        )
        out.append(
            {
                "task_id": tid,
                "title": (task.title if task else "") or "",
                "issue_url": engine._issue_url(tid),
                "pr_url": ts.get("pr_url"),
                "state": state,
                "current_stage": current,
                "stage_age_s": current_rec["duration_s"] if in_stage and current_rec else None,
                "model": current_rec["model"] if in_stage and current_rec else None,
                "seconds_since_update": ts.get("seconds_since_update"),
                "stale": bool(ts.get("stale")),
                "blocked_reason": (
                    blocked_reasons.get(tid) if state == "blocked_on_human" else None
                ),
                "stages": stages,
                "depends_on": deps,
                "waiting_on": waiting_on,
                "activity": _stream_activity(ts, stall_after_s=stall_after_s),
            }
        )
    return out


# --- recent events as sentences (#525) ----------------------------------------------------

#: Notification kinds that duplicate a first-class event of the same meaning — the
#: ``notification`` audit row would otherwise print the same fact twice.
_NOTIFICATION_DUPLICATES = frozenset({"task_completed", "run_paused", "run_finalized"})


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip()


def _money(value: object) -> str | None:
    return f"${value:.2f}" if isinstance(value, (int, float)) and value else None


def event_sentence(ev: dict) -> str:
    """One readable sentence for an events.jsonl row (#525). Pure: no I/O, no clock.

    Covers the event types an operator watches for; anything else falls back to its
    humanised type name (plus task and stage when present), so a new event type still
    reads as words rather than a raw identifier."""
    etype = str(ev.get("type") or "event")
    kind = ev.get("kind")
    task = ev.get("task_id")
    stage = ev.get("stage")
    who = f"{task}: " if task else ""
    stage_s = str(stage).upper() if stage else "a stage"

    if etype == "notification" and kind:
        etype = str(kind)
    if etype == "stage_dispatched":
        attempt = ev.get("attempt")
        retry = f" (retry {attempt})" if isinstance(attempt, int) and attempt > 0 else ""
        model = f" on {ev['model']}" if ev.get("model") else ""
        return f"{who}started {stage_s}{model}{retry}"
    if etype == "stage_recorded":
        status = ev.get("status")
        cost = _money(ev.get("cost_usd"))
        tail = f" ({cost})" if cost else ""
        if status == "success":
            return f"{who}finished {stage_s}{tail}"
        outcome = _humanize(str(ev.get("outcome") or status or "did not succeed"))
        return f"{who}{stage_s} did not succeed: {outcome}{tail}"
    if etype == "task_blocked":
        reason = ev.get("reason") or ev.get("blocked_reason")
        at = f" at {stage_s}" if stage else ""
        return f"{who}blocked{at}, needs a human decision" + (f" ({reason})" if reason else "")
    if etype == "task_completed":
        pr = f", PR {ev['pr_url']}" if ev.get("pr_url") else ""
        return f"{who}completed{pr}"
    if etype == "run_paused":
        return "run paused" + (f": {ev['reason']}" if ev.get("reason") else "")
    if etype == "run_unpaused":
        return "run unpaused"
    if etype == "run_finalized":
        return f"run finished ({ev.get('state') or 'final'})"
    if etype == "driver_claimed":
        host = f" on {ev['host']}" if ev.get("host") else ""
        return f"driver pid {ev.get('pid')}{host} took over the run"
    if etype == "dispatch_reclaimed":
        return (
            f"{who}{stage_s} was re-queued at the same attempt after its driver died"
        )
    if etype == "rate_limit_cooldown":
        until = f" until {ev['not_before']}" if ev.get("not_before") else ""
        return f"{who}{stage_s} hit a rate limit; waiting{until}"
    if etype == "stage_rerouted_to_engine_lane":
        reason = f" ({ev['reason']})" if ev.get("reason") else ""
        return f"{who}{stage_s} moved to the deterministic engine lane{reason}"
    if etype.startswith("batch_integration_gate"):
        failing = ", ".join(str(f) for f in ev.get("failing") or [])
        if etype == "batch_integration_gate_red" or (
            etype == "batch_integration_gate_ran" and ev.get("green") is False
        ):
            return "batch integration check failed" + (f": {failing}" if failing else "")
        if etype == "batch_integration_gate_ran":
            return "batch integration check passed"
        if etype == "batch_integration_gate_skipped":
            reason = f" ({_humanize(str(ev['reason']))})" if ev.get("reason") else ""
            return f"batch integration check skipped{reason}"
        return _humanize(etype)
    at = f" ({str(stage).upper()})" if stage else ""
    return f"{who}{_humanize(etype)}{at}"


def _recent_events(events: list[dict], limit: int) -> list[dict]:
    """The newest ``limit`` events (oldest first, as a timeline reads) as sentences."""
    picked: list[dict] = []
    for ev in reversed(events):
        if len(picked) >= limit:
            break
        if ev.get("type") == "notification" and ev.get("kind") in _NOTIFICATION_DUPLICATES:
            continue
        level = ev.get("severity") or ev.get("level")
        picked.append(
            {
                "ts": ev.get("ts"),
                "type": ev.get("type"),
                "task_id": ev.get("task_id"),
                "level": level if isinstance(level, str) else None,
                "sentence": event_sentence(ev),
            }
        )
    picked.reverse()
    return picked


def _run_row(
    engine: Engine,
    loc: _RunLoc,
    *,
    stale_after_s: int,
    include_activity: bool,
    now_epoch: float,
    stall_after_s: int = DEFAULT_STALL_AFTER_S,
    recent_event_limit: int = DEFAULT_RECENT_EVENTS,
) -> dict:
    """Fold one run's ``status()`` into a compact dashboard row. An unreadable run (or one
    whose ``status()`` raises on a partial doc) yields an ``<unreadable>`` row — never a crash
    — that keeps the exception's type and message (#498)."""
    base = _base_row(
        loc, project=_project_label(loc.project_ref, getattr(engine.project, "name", None))
    )
    if loc.unreadable:
        return _reduced_row(
            base,
            flag="unreadable status",
            kind="unreadable",
            detail="no status-*.json in the run dir parses as a run document",
        )
    try:
        status = engine.status(
            loc.run_id, stale_after_s=stale_after_s, include_activity=include_activity
        )
    # Kept broad on purpose — one bad run must cost one row, not the board — but the cause
    # is RETAINED (#498): the bare catch used to discard it, leaving a transient failure on
    # a healthy run undiagnosable after the fact.
    except Exception as exc:  # noqa: BLE001 - a partial/corrupt task doc becomes a marked row
        error = _error_info(exc)
        return _reduced_row(
            base,
            flag=f"unreadable status: {error['type']}",
            kind="unreadable",
            detail=_error_text(error),
            error=error,
        )

    state = str(status.get("run_state"))
    progress = status.get("progress") or {}
    tasks: dict[str, dict] = status.get("tasks") or {}
    cost = status.get("cost") or {}
    budget = status.get("budget")
    events = _read_events(engine, loc.run_id)
    pause_reason, blocked_reasons, blocked_gates = _reasons_from_events(events)
    root = base["root"]

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

    # Every attention item carries ``commands`` (#525): the exact lines that resolve it,
    # rooted at THIS row's store dir so a renderer can print them verbatim next to the item.
    # Empty where no single command resolves it (a stale task wants a look, not a verb).
    budget_exhausted = bool(budget and budget.get("exhausted"))
    for tid in blocked_tasks:
        reason = blocked_reasons.get(tid) or "held at human gate"
        flags.append(f"blocked_on_human:{tid}")
        attention_items.append(
            {
                "kind": "blocked_on_human",
                "run_id": loc.run_id,
                "task_id": tid,
                "reason": reason,
                "commands": release_commands(
                    root=root, run_id=loc.run_id, task_id=tid, gate=blocked_gates.get(tid)
                ),
            }
        )
    if state == "paused":
        reason = pause_reason or "paused"
        flags.append("paused")
        attention_items.append(
            {
                "kind": "paused",
                "run_id": loc.run_id,
                "reason": reason,
                "commands": _unpause_commands(root, loc.run_id, budget=budget_exhausted),
            }
        )
    if state == "parked":
        parked = status.get("supervisor_parked") or {}
        reason = parked.get("reason") or "needs a fresh supervisor"
        resume = parked.get("resume_command")
        flags.append("parked:needs-fresh-supervisor")
        attention_items.append(
            {
                "kind": "parked",
                "run_id": loc.run_id,
                "reason": reason,
                "resume_command": resume,
                "commands": (
                    [{"label": "resume in a fresh supervisor session", "command": resume}]
                    if resume
                    else []
                ),
            }
        )
    if budget_exhausted:
        flags.append("budget-exhausted")
        attention_items.append(
            {
                "kind": "budget_exhausted",
                "run_id": loc.run_id,
                "fraction": budget.get("fraction") if budget else None,
                "commands": _unpause_commands(root, loc.run_id, budget=True),
            }
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
                "commands": [],
            }
        )

    # --- #525 detail blocks -----------------------------------------------------------
    # Best-effort reads beyond what ``status()`` returns: the ledger rows (cost by task and
    # stage) and the run doc (dependency graph). ``status()`` just read both, so a failure
    # here is a race; it thins the detail rather than degrading the whole row.
    try:
        ledger_rows = engine.run_rows(loc.run_id)
    except Exception:  # noqa: BLE001 - a garbled ledger thins the detail, never the row
        ledger_rows = []
    cost_breakdown, task_costs = _cost_breakdown(ledger_rows)
    try:
        graph = dict(engine.store.load_run(loc.run_id).dependency_graph)
    except Exception:  # noqa: BLE001 - see above
        graph = {}
    task_details = _task_details(
        engine,
        loc.run_id,
        tasks,
        graph=graph,
        task_costs=task_costs,
        blocked_reasons=blocked_reasons,
        stall_after_s=stall_after_s,
        now_epoch=now_epoch,
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
        # The same qualification for the OTHER way this figure can mislead: a run priced
        # under the pre-#350 regime is ~20x overstated and byte-compatible with a current
        # one. The board sums across runs, so it is exactly where two regimes would blend
        # into one meaningless total unless the row carries its own provenance.
        "legacy_accounting_rows": int(
            (cost.get("accounting") or {}).get("legacy_affected_rows") or 0
        ),
        "budget": budget,
        "last_event_age_s": _last_event_age_s(events, now_epoch=now_epoch),
        "flags": flags,
        "attention": bool(attention_items),
        "attention_items": attention_items,
        "tasks": task_details,
        "driver": _driver_summary(status.get("driver"), terminal=state in _TERMINAL),
        "cost_breakdown": cost_breakdown,
        "recent_events": _recent_events(events, recent_event_limit),
    }


# --- the snapshot -----------------------------------------------------------------------
#
# Snapshot layout (#525 design pass). Both views read this one structure, ordered by the
# questions an operator asks: does anything need me / what is running now / how far along
# / what has it cost / what happened. Mocks, then the fields each element reads.
#
# Console, compact (narrow terminal):
#   NEEDS YOU (2)
#     ! batch-7  #512 blocked at REVIEW: needs an API that does not exist
#         approve: orchestrator --root … --run batch-7 approve --task '#512' --by "$USER"
#     ! batch-6  paused: 3 consecutive failures      unpause: orchestrator … unpause
#   RUNNING  sous: batch-7  driver alive, running stages     3/5  ≥$4.10 (1 unmetered)
#     #514 Fix the flaky gate   IMPLEMENT 12m  opus  stream active (8s)
#     #515 Add the CSV export   waiting on #514
#   RECENT   14:02 #514: started IMPLEMENT on opus · 13:58 #512: blocked at REVIEW …
#
# Console, wide: the same bands, plus per task the stage strip and per-stage cost —
#     #514 Fix the flaky gate  PR —  intake ✓ scope ✓ implement ▶ test · review · deliver
#                              $0.00 · $0.40 · $1.90 (running 12m) · — · — · —
#   and a COST band per run: by stage, by task, tokens in/out, unmetered counts.
#
# Web: header (projects, usage, board spend) → attention cards with copy-able commands →
#   one card per run (driver line, progress, cost) with a collapsible task table (stage
#   strip, links, waiting-on) → a recent-events timeline per run.
#
# Element → fields:
#   header / project groups   header.projects[{project, run_ids, runs, attention, running}]
#   attention item + command  attention[].{kind, reason, error, commands[{label, command}]}
#   driver line               runs[].driver.{state, summary, reason, heartbeat_age_s}
#   task line                 runs[].tasks[].{task_id, title, issue_url, pr_url, state,
#                             current_stage, stage_age_s, model, activity.{state,
#                             seconds_since_event, line}, depends_on, waiting_on}
#   stage strip               runs[].tasks[].stages[].{stage, status, attempt, cost_usd,
#                             metered, unmetered_calls, duration_s}
#   cost band                 runs[].cost_breakdown.{cost_usd, calls, unmetered_calls,
#                             input_tokens, output_tokens, by_task{}, by_stage{}}
#   timeline                  runs[].recent_events[].{ts, task_id, level, sentence}
#   degraded row              runs[].{degraded, unreadable, state, error.{type, message}}


class DashboardSnapshotKwargs(TypedDict, total=False):
    """Optional keyword arguments accepted by :func:`dashboard_snapshot`."""

    stale_after_s: int
    include_activity: bool
    limit: int
    show_all: bool
    recent_terminal: int
    engine_factory: EngineFactory | None
    usage_reader: Callable[[], object] | None
    clock: Callable[[], float]
    stall_after_s: int
    recent_event_limit: int


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
    root: Roots,
    *,
    stale_after_s: int = 1800,
    include_activity: bool = True,
    limit: int = 20,
    show_all: bool = False,
    recent_terminal: int = 5,
    engine_factory: EngineFactory | None = None,
    usage_reader: Callable[[], object] | None = None,
    clock: Callable[[], float] = time.time,
    stall_after_s: int = DEFAULT_STALL_AFTER_S,
    recent_event_limit: int = DEFAULT_RECENT_EVENTS,
) -> dict:
    """Assemble the whole-board snapshot: a global header + a sorted ATTENTION band + the
    per-run rows, all attention-first.

    Selection: every discovered run is folded into a row (recency order). By default the board
    shows all non-terminal runs plus the ``recent_terminal`` most-recent terminal runs; with
    ``show_all`` it shows everything. The kept rows are then sorted attention-first (anything
    needing a human above healthy runs, ties by recency) and truncated to ``limit``.

    ``root`` is one runs-root or several (#386): rows from every root are merged into ONE
    board before selection, so the attention-first ordering stays global — a run needing a
    human outranks a healthy run in another project rather than sorting below its own root.

    ``engine_factory(run_root, project_ref) -> Engine`` builds the read-only engine per run
    (required — the CLI passes ``default_engine_factory``). It is handed the adapter ref off
    that run's OWN doc, which is what lets one board render several projects; a factory that
    cannot resolve an adapter raises ``AdapterUnresolved`` and costs exactly one row.
    ``usage_reader`` / ``clock`` are injected so a probe failure can't break the board and
    the snapshot is deterministic in tests.

    #525: each readable row also carries ``tasks`` (per-task detail: title, links, current
    stage and how long it has been there, model, per-stage progress and cost, dependencies
    and what it is waiting on, stream activity judged against ``stall_after_s``), ``driver``
    (a plain-words summary), ``cost_breakdown`` (by task and by stage, unmetered-aware) and
    ``recent_events`` (the newest ``recent_event_limit`` events as sentences); attention
    items carry ``commands``; the header adds ``projects``. See the snapshot layout note for
    which renderer element reads which field (the layout note above this function).
    Every pre-#525 key is unchanged.
    """
    if engine_factory is None:
        raise ValueError("dashboard_snapshot requires an engine_factory")
    now_epoch = clock()
    locs = _discover_all(root)
    rows: list[dict] = []
    for loc in locs:
        try:
            engine = engine_factory(loc.root, loc.project_ref)
        # The factory resolves this run's adapter, so it is the first thing that can fail
        # per-row. Degrade THIS run (a marked, still-visible row) rather than the board.
        # #498: the run doc parsed (discovery read it), so this row is NOT unreadable — its
        # own state marker says what actually went wrong, and the cause is retained.
        except AdapterUnresolved as exc:
            rows.append(
                _reduced_row(
                    _base_row(loc),
                    flag=f"adapter unresolved: {loc.project_ref or 'no project_ref'}",
                    kind="adapter_unresolved",
                    state=ADAPTER_UNRESOLVED_STATE,
                    detail=str(exc),
                    error=_error_info(exc),
                )
            )
            continue
        rows.append(
            _run_row(
                engine,
                loc,
                stale_after_s=stale_after_s,
                include_activity=include_activity,
                now_epoch=now_epoch,
                stall_after_s=stall_after_s,
                recent_event_limit=recent_event_limit,
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
    legacy_accounting_rows = 0
    legacy_accounting_runs = 0
    for row in shown:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
        if isinstance(row.get("cost_usd"), (int, float)):
            total_spend += row["cost_usd"]
        unmetered_calls += row.get("unmetered_calls") or 0
        total_invocations += row.get("total_invocations") or 0
        if row.get("legacy_accounting_rows"):
            legacy_accounting_rows += row["legacy_accounting_rows"]
            legacy_accounting_runs += 1

    header = {
        "generated_at": datetime.fromtimestamp(now_epoch, tz=UTC).isoformat(),
        "usage": _read_usage(usage_reader),
        "total_spend_usd": round(total_spend, 4),
        "unmetered_calls": unmetered_calls,
        "total_invocations": total_invocations,
        # A board-wide total spanning two pricing regimes is not a number anyone should act
        # on. Count the runs it came from so the skin can say so rather than print it flat.
        "legacy_accounting_rows": legacy_accounting_rows,
        "legacy_accounting_runs": legacy_accounting_runs,
        "counts": counts,
        "shown": len(shown),
        "total_discovered": total_discovered,
        "attention_count": len(attention),
        "all_quiet": len(attention) == 0,
        "running": counts.get("running", 0),
        "paused": counts.get("paused", 0),
        "projects": _project_groups(shown),
    }
    return {"header": header, "attention": attention, "runs": shown}


def _project_groups(rows: list[dict]) -> list[dict]:
    """The shown rows grouped by their ``project`` label (#525), in board order — so the
    first group is the one holding the most urgent run. Grouping only: which runs exist is
    discovery's business, unchanged here."""
    groups: dict[str, dict] = {}
    for row in rows:
        label = str(row.get("project") or "?")
        group = groups.setdefault(
            label,
            {"project": label, "run_ids": [], "runs": 0, "attention": 0, "running": 0},
        )
        group["run_ids"].append(row["run_id"])
        group["runs"] += 1
        group["attention"] += len(row.get("attention_items") or [])
        group["running"] += 1 if row.get("state") == "running" else 0
    return list(groups.values())


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
        UNREADABLE_STATE: "?",
        ADAPTER_UNRESOLVED_STATE: "?",
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
        # #498: name the cause. "Inspect by hand" alone sent the operator to status docs
        # that were usually fine; the caught exception is what says what actually failed.
        cause = _error_text(item.get("error")) or item.get("reason")
        return f"  ! [{run}] UNREADABLE status — {cause or f'inspect runs/{run}/ by hand'}"
    if kind == "adapter_unresolved":
        return f"  ! [{run}] PROJECT ADAPTER UNRESOLVED — {item.get('reason')}"
    return f"  ! [{run}] {kind}"  # pragma: no cover - defensive


def _degraded_marker(row: dict) -> str:
    """The state cell of a degraded row (#498): what went wrong, not a blanket
    "unreadable" — an adapter that will not resolve says so, and a status read that raised
    names its exception."""
    if row.get("state") == ADAPTER_UNRESOLVED_STATE:
        return "<adapter unresolved>"
    cause = _error_text(row.get("error"))
    return f"<unreadable status: {cause}>" if cause else "<unreadable status>"


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
        # #386: the probe reads the ACCOUNT's window, and the board now spans projects that
        # share it. Say "account" so the number is not misread as this root's own capacity.
        lines.append(
            f"usage (account): 5h {usage['five_hour_pct']:.0f}% / 7d "
            f"{usage.get('seven_day_pct') or 0:.0f}%"
        )
    else:
        lines.append("usage (account): unavailable")

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
    # The board is the one place two pricing regimes get added together. Say it in the
    # header rather than letting a ~20x-overstated legacy run inflate a total silently.
    legacy_runs = header.get("legacy_accounting_runs") or 0
    if legacy_runs:
        lines.append(
            f"  ⚠️ {legacy_runs} run(s) priced under the pre-#350 regime "
            f"({header.get('legacy_accounting_rows') or 0} row(s)) — overstated ~20x, "
            f"not comparable with the rest; the total above is inflated by them"
        )

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
    # #386: the project column is what disambiguates a board holding several projects'
    # runs. Sized to the widest label present so a single-project board stays narrow.
    proj_w = min(max((len(str(r.get("project") or "?")) for r in runs), default=1), 18)
    for row in runs:
        icon = _state_icon(row["state"])
        project = str(row.get("project") or "?")[:proj_w]
        if row.get("degraded") or row.get("unreadable"):
            flags = f"  [{', '.join(row['flags'])}]" if row["flags"] else ""
            lines.append(
                f"  {icon} {row['run_id']:<24} {project:<{proj_w}} {_degraded_marker(row)}"
                f"{flags}"
            )
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
            f"  {icon} {row['run_id']:<24} {project:<{proj_w}} {row['state']:<26} "
            f"{_progress_str(row['progress']):>7}  {cost_str:>10}{flags}  (last {age} ago)"
        )
        for inf in row["inflight"]:
            lines.append(f"      {inf['task_id']} · {inf['line']}")
    return "\n".join(lines)


# --- watch loop -------------------------------------------------------------------------


def render_watch(
    root: Roots,
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
