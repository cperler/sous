"""Shared alerting logic (#55): turn a ``status()`` snapshot into the stall
notifications that are due, with dedupe so a stalled task is signalled ONCE per
stall episode.

The old bash monitor emailed + desktop-notified on stalls/failures; the rebuild
only had the *sensor* (``Engine.status`` flags ``stale`` + ``seconds_since_update``).
This module is the consumer's shared core: the scheduler's long-running ``run()``
loop and the ``watch`` CLI both feed it a status snapshot + the set of task ids
already signalled, so the dedupe lives in ONE pure, sleep-free, unit-testable place.

Point-in-time transitions (task failed / blocked-on-human / run paused / run
finalized) are emitted where they HAPPEN — in the engine's ``record`` /
``_maybe_finalize_run`` and the scheduler's breaker — via ``Engine.emit_notification``.
This module owns only the poll-driven stall case, which has no single transition to
hang an emit on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an engine<-alerting import cycle
    from .engine import Engine

# Notification ``kind`` vocabulary (payload["kind"] + the events.jsonl `notification`
# row's ``kind``). Kept here so the emit sites and any consumer share one spelling.
NOTIFY_TASK_STALE = "task_stale"
NOTIFY_TASK_FAILED = "task_failed"
NOTIFY_TASK_BLOCKED = "task_blocked"
NOTIFY_RUN_PAUSED = "run_paused"
NOTIFY_RUN_FINALIZED = "run_finalized"

# Run states that end a watch (mirror RunState terminal values without importing the
# enum — this module works off the JSON status snapshot, not engine objects).
_TERMINAL_RUN_STATES = frozenset({"completed", "failed"})


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


def watch(
    engine: Engine,
    run_id: str,
    *,
    interval: int = 60,
    stale_after_s: int = 1800,
    sleeper: Callable[[int], None],
    emit: Callable[[str], None] = lambda _line: None,
) -> dict:
    """Poll ``run_id`` to a terminal run state, firing stall notifications with the
    shared once-per-episode dedupe. Usable for ANY run — including single-task
    engine-lane runs that no scheduler loop is watching.

    Each pass: read ``status`` (with ``stale_after_s``), emit any newly-due stall
    notifications (project hook + audit row via ``engine.emit_notification``, plus a
    human line via ``emit``), and return the final status once the run is terminal.
    ``sleeper`` is injected (``time.sleep`` in production, a stub in tests) so the loop
    is drivable without real sleeping.
    """
    stale_sent: set[str] = set()
    while True:
        status = engine.status(run_id, stale_after_s=stale_after_s)
        notes, stale_sent = stale_notifications(status, stale_sent)
        for note in notes:
            engine.emit_notification(run_id, note["kind"], note)
            emit(note["summary"])
        if str(status.get("run_state")) in _TERMINAL_RUN_STATES:
            return status
        sleeper(interval)
