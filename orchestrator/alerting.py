"""Shared alerting logic (#55): turn a ``status()`` snapshot into the stall
notifications that are due, with dedupe so a stalled task is signalled ONCE per
stall episode.

The old bash monitor emailed + desktop-notified on stalls/failures; the rebuild
only had the *sensor* (``Engine.status`` flags ``stale`` + ``seconds_since_update``).
This module is the consumer's shared core: the scheduler's long-running ``run()``
loop and the ``watch`` CLI both feed it a status snapshot + the set of task ids
already signalled, so the dedupe lives in ONE pure, sleep-free, unit-testable place.

Point-in-time transitions (task completed / task failed / blocked-on-human / run paused /
run finalized) are emitted where they HAPPEN — in the engine's ``record`` /
``_on_task_completed`` / ``_maybe_finalize_run`` and the scheduler's breaker — via
``Engine.emit_notification``.
This module owns only the poll-driven stall case, which has no single transition to
hang an emit on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .schemas.enums import TERMINAL_RUN_STATES, RunState

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an engine<-alerting import cycle
    from .engine import Engine

# Notification ``kind`` vocabulary (payload["kind"] + the events.jsonl `notification`
# row's ``kind``). Kept here so the emit sites and any consumer share one spelling.
NOTIFY_TASK_STALE = "task_stale"
NOTIFY_TASK_FAILED = "task_failed"
# #359: the success half of the per-task pair. Before this, a task that LANDED was only
# observable at whole-run granularity (``run_finalized``), which on a multi-task batch says
# nothing about WHICH task shipped or where its PR is — so a detached driver could finish a
# task and the only way to learn it was to poll ``status``. Emitted from the engine's
# ``_on_task_completed`` choke point (NOT ``_terminal_effects``, which only runs the
# failed/rejected dispositions), so it fires exactly once per completed task and covers the
# decomposition-parent path too.
NOTIFY_TASK_COMPLETED = "task_completed"
NOTIFY_TASK_BLOCKED = "task_blocked"
NOTIFY_RUN_PAUSED = "run_paused"
NOTIFY_RUN_FINALIZED = "run_finalized"
# #257: the human retired the run as superseded. Distinct from run_finalized so an
# operator can tell a run that was stopped from one that ran to its own conclusion.
NOTIFY_RUN_SUPERSEDED = "run_superseded"
# #313: the scheduler loop stopped with work left that it may not touch — every
# non-terminal task holds a dispatch lease it could not reclaim. Distinct from
# run_paused (a deliberate gate) and task_stale (the task may still be progressing).
NOTIFY_RUN_BLOCKED = "run_blocked"

# Run states that end a watch. Derived from the enum's own terminal set rather than
# re-spelled here (#257): this module works off the JSON status snapshot, so it compares
# STRING values — but a hand-maintained copy silently omits each new terminal state, and
# an omitted one means `watch` polls a finished run forever. Includes
# completed_with_rejections (#67) and superseded (#257) by construction.
_TERMINAL_RUN_STATES = frozenset(s.value for s in TERMINAL_RUN_STATES)
_WATCH_STOP_STATES = _TERMINAL_RUN_STATES | {RunState.PARKED.value}


def stale_notifications(
    status: dict, sent: set[str]
) -> tuple[list[dict], set[str]]:
    """Given a ``status()`` snapshot and the set of task ids already signalled as
    stale, return ``(new_notifications, updated_sent)``.

    Dedupe contract (fire once per stall episode):
      - a task flagged ``stale`` that is NOT in ``sent`` yields one notification and
        joins the returned set;
      - a task in ``sent`` that is no longer stale (it updated, or went terminal /
        BLOCKED_ON_HUMAN — ``status`` never flags those stale) DROPS out, so a fresh
        later stall re-fires.

    The returned set is exactly the currently-stale task ids, so threading it back in
    each poll pass yields the once-per-episode behavior. Pure and sleep-free.
    """
    run_id = status.get("run_id")
    tasks: dict[str, dict] = status.get("tasks", {}) or {}
    currently_stale = {tid for tid, ts in tasks.items() if ts.get("stale")}
    fresh = currently_stale - set(sent)
    notifications: list[dict] = []
    for tid in sorted(fresh):  # sorted: deterministic order for callers/tests
        ts = tasks[tid]
        secs = ts.get("seconds_since_update")
        stage = ts.get("current_stage")
        notifications.append(
            {
                "run_id": run_id,
                "task_id": tid,
                "kind": NOTIFY_TASK_STALE,
                "summary": (
                    f"task {tid} STALLED — no update for {secs}s "
                    f"(stage {stage}, state {ts.get('state')})"
                ),
                "seconds_since_update": secs,
                "stage": stage,
                "state": ts.get("state"),
            }
        )
    return notifications, currently_stale


def _fmt_activity(act: dict | None) -> str:
    """A one-line description of a probe's ``current_activity`` (``{"tool","detail"}``)."""
    if not act:
        return "working"
    tool = act.get("tool") or "working"
    detail = act.get("detail")
    return f"{tool}: {detail}" if detail else str(tool)


def _driver_down_reason(driver: dict) -> str:
    """Why a still-running driver counts as down (#334) — the operator-facing half of
    ``driver.alive``. Prefers the recorded exit (the driver said so itself) over an inferred
    stale heartbeat, and degrades to a generic phrase if the block carries neither."""
    if driver.get("exited"):
        reason = driver.get("exit_reason")
        return f"it recorded an exit ({reason})" if reason else "it recorded an exit"
    age = driver.get("heartbeat_age_s")
    if driver.get("heartbeat_stale") and isinstance(age, (int, float)):
        return f"its last heartbeat was {age}s ago"
    if driver.get("heartbeat_stale"):
        return "its heartbeat has gone stale"
    return "it is no longer reporting alive"


def activity_lines(status: dict, *, stall_after_s: int = 300) -> list[str]:
    """Human activity lines for a status snapshot taken with ``include_activity=True`` (#66):
    one line per RUNNING task that has a live provider stream, describing what the model is
    doing + how long since the stream last grew. A stream that hasn't grown for
    ``stall_after_s`` while the stage is RUNNING gets a DISTINCT ``STREAM STALLED`` note — an
    earlier signal than task-level staleness (which only fires on the whole task not moving).

    #313: a frozen stream has two causes that call for opposite responses — the model went
    quiet (wait, or look at the stage), or the DRIVER process died and nothing is running at
    all (re-invoke the driver). When the snapshot's ``driver`` block says the claiming
    process is gone, the line says NO LIVE DRIVER instead, so the operator is pointed at the
    real cause rather than at the model. Pure and sleep-free, so it is unit-testable off a
    synthetic snapshot.

    #334: keyed off #323's merged ``driver.alive`` (pid claim + heartbeat freshness + a
    recorded exit) rather than the narrow #313 ``state == "dead"`` pid probe, so a driver
    whose process is technically still running but has STOPPED LOOPING is caught too — the
    state a pid probe cannot see. The two down-cases get distinct wording because the fix
    differs: a gone process is re-invoked directly, a wedged one must be killed first.
    ``alive`` is tri-state and fails safe (``None`` = cannot answer, e.g. a foreign-host
    claim), so only an explicit ``False`` raises the alarm."""
    tasks: dict[str, dict] = status.get("tasks", {}) or {}
    driver: dict = status.get("driver") or {}
    driver_process_gone = driver.get("state") == "dead"
    # Strict superset of the #313 condition: an explicit alive=False OR the old pid verdict,
    # so a pre-#323 snapshot (no ``alive`` key at all) keeps its NO LIVE DRIVER alert.
    driver_down = driver.get("alive") is False or driver_process_gone
    lines: list[str] = []
    for tid in sorted(tasks):
        ts = tasks[tid]
        act = ts.get("activity")
        if not act:
            continue
        stage = ts.get("current_stage")
        since = act.get("seconds_since_event")
        desc = _fmt_activity(act.get("current_activity"))
        seen = act.get("events_seen")
        if isinstance(since, (int, float)) and since >= stall_after_s and driver_down:
            if driver_process_gone:
                lines.append(
                    f"[{tid}] NO LIVE DRIVER — the driver process (pid {driver.get('pid')}) "
                    f"is gone, so nothing is running this stage; no stream output for {since}s "
                    f"(stage {stage}, {desc}). Re-invoke the driver to resume."
                )
            else:
                why = _driver_down_reason(driver)
                lines.append(
                    f"[{tid}] DRIVER NOT LOOPING — the driver process (pid "
                    f"{driver.get('pid')}) is still running but {why}, so nothing is "
                    f"advancing this stage; no stream output for {since}s (stage {stage}, "
                    f"{desc}). Kill its process group, then re-invoke the driver to resume."
                )
        elif isinstance(since, (int, float)) and since >= stall_after_s:
            lines.append(
                f"[{tid}] STREAM STALLED — no stream output for {since}s "
                f"(stage {stage}, {desc})"
            )
        else:
            ago = f"{since}s ago" if isinstance(since, (int, float)) else "unknown"
            lines.append(f"[{tid}] {stage}: {desc} ({seen} events, last {ago})")
    return lines


def watch(
    engine: Engine,
    run_id: str,
    *,
    interval: int = 60,
    stale_after_s: int = 1800,
    sleeper: Callable[[int], None],
    emit: Callable[[str], None] = lambda _line: None,
    activity: bool = False,
    stall_after_s: int = 300,
) -> dict:
    """Poll ``run_id`` until terminal or supervisor-parked, firing stall notifications
    with the shared once-per-episode dedupe. Usable for ANY run — including single-task
    engine-lane runs that no scheduler loop is watching.

    Each pass: read ``status`` (with ``stale_after_s``), emit any newly-due stall
    notifications (project hook + audit row via ``engine.emit_notification``, plus a
    human line via ``emit``), and return the final status once the run is terminal. A
    PARKED run is also a stop condition: it emits the persisted fresh-supervisor resume
    command but does not classify the clean handoff as stale or human-blocked.
    ``sleeper`` is injected (``time.sleep`` in production, a stub in tests) so the loop
    is drivable without real sleeping.

    ``activity`` (#66): also emit a live per-running-task activity line each pass (what the
    model is doing + seconds since its stream last grew), with a distinct stream-stall note
    when a stream freezes for ``stall_after_s`` — earlier than task-level staleness. Opt-in so
    the default watch stays a lean stall/terminal monitor.
    """
    stale_sent: set[str] = set()
    while True:
        status = (
            engine.status(run_id, stale_after_s=stale_after_s, include_activity=True)
            if activity
            else engine.status(run_id, stale_after_s=stale_after_s)
        )
        notes, stale_sent = stale_notifications(status, stale_sent)
        for note in notes:
            engine.emit_notification(run_id, note["kind"], note)
            emit(note["summary"])
        if activity:
            for line in activity_lines(status, stall_after_s=stall_after_s):
                emit(line)
        if str(status.get("run_state")) == RunState.PARKED.value:
            parked = status.get("supervisor_parked") or {}
            emit(
                "run PARKED — needs a fresh supervisor; resume: "
                f"{parked.get('resume_command') or 'resume-supervisor'}"
            )
            return status
        if str(status.get("run_state")) in _WATCH_STOP_STATES:
            return status
        sleeper(interval)
