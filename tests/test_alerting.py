"""Alerting seam (#55): the notify hook + the stall/failure/pause/finalize emissions
+ the shared, sleep-free dedupe core the scheduler loop and the `watch` CLI share.

The old bash monitor emailed + desktop-notified on stalls/failures; the rebuild had
only the sensor (``Engine.status`` flags ``stale``). These tests pin the consumer:
once-per-episode stall dedupe, the transition emissions, best-effort hook isolation,
the ``notification`` audit rows, and the getattr fallback when a project has no hook.
"""

from __future__ import annotations

from orchestrator.alerting import (
    NOTIFY_TASK_STALE,
    stale_notifications,
    watch,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _recording_project(**engine_kw):
    """A FakeProject with a recording ``notify`` hook (assigned as an instance attr —
    the engine looks it up via getattr, so a plain 2-arg callable is enough)."""
    project = FakeProject()
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    return project, calls


def _status(run_id: str, tasks: dict, run_state: str = "running") -> dict:
    """A minimal status() snapshot shaped like Engine.status output."""
    return {"run_id": run_id, "run_state": run_state, "tasks": tasks}


# --- the pure dedupe core --------------------------------------------------------

def test_stale_fires_once_per_episode() -> None:
    snap = _status("r1", {
        "A": {"stale": True, "seconds_since_update": 2000, "current_stage": "implement",
              "state": "running"},
        "B": {"stale": False, "seconds_since_update": 3, "current_stage": "test",
              "state": "running"},
    })
    notes, sent = stale_notifications(snap, set())
    assert [n["task_id"] for n in notes] == ["A"]  # only the stale one
    assert notes[0]["kind"] == NOTIFY_TASK_STALE
    assert notes[0]["seconds_since_update"] == 2000
    assert notes[0]["stage"] == "implement"
    assert "STALLED" in notes[0]["summary"]
    assert sent == {"A"}

    # Same snapshot, same sent set → no re-ping (dedupe holds).
    again, sent2 = stale_notifications(snap, sent)
    assert again == []
    assert sent2 == {"A"}


def test_stale_reset_when_task_updates_then_restalls() -> None:
    stale = _status("r1", {"A": {"stale": True, "seconds_since_update": 2000,
                                 "current_stage": "implement", "state": "running"}})
    _, sent = stale_notifications(stale, set())
    assert sent == {"A"}

    # A moves again → no longer stale → drops out of the dedupe set (episode ended).
    moved = _status("r1", {"A": {"stale": False, "seconds_since_update": 1,
                                 "current_stage": "test", "state": "running"}})
    notes, sent = stale_notifications(moved, sent)
    assert notes == []
    assert sent == set()

    # A stalls again → a NEW episode re-fires.
    notes, sent = stale_notifications(stale, sent)
    assert [n["task_id"] for n in notes] == ["A"]
    assert sent == {"A"}


# --- transition emissions via a recording hook -----------------------------------

def _drive_intake(eng, run_id="r1", task_id="t1"):
    eng.create_run(run_id)
    eng.add_task(run_id, task_id)
    eng.record(run_id, make_result(eng.next_work(run_id, task_id)))  # intake (deterministic)


def test_task_failed_and_run_finalized_emitted(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project, max_attempts=1)
    _drive_intake(eng)
    # Fail the next model stage terminally (max_attempts=1).
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom",
                                 structured_output={}))

    kinds = [k for k, _ in calls]
    assert "task_failed" in kinds
    assert "run_finalized" in kinds  # the "batch is done" ping
    failed = next(p for k, p in calls if k == "task_failed")
    assert failed["task_id"] == "t1"
    assert failed["reason"] == "boom"
    assert failed["run_id"] == "r1"
    final = next(p for k, p in calls if k == "run_finalized")
    assert final["state"] == "failed"


def test_blocked_on_human_emitted(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    _drive_intake(eng)
    w = eng.next_work("r1", "t1")  # scope
    assert w.stage is Stage.SCOPE
    eng.record("r1", make_result(w, structured_output={
        "feasible": False, "blocked_reason": "needs an API that does not exist", "plan": []}))

    blocked = [p for k, p in calls if k == "task_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "needs an API that does not exist"
    assert "BLOCKED_ON_HUMAN" in blocked[0]["summary"]
    # A held run is NOT finalized (non-terminal), so no finalize ping yet.
    assert "run_finalized" not in [k for k, _ in calls]


def test_run_finalized_emitted_once_on_clean_run(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    finals = [p for k, p in calls if k == "run_finalized"]
    assert len(finals) == 1  # exactly once, at the finalize transition
    assert finals[0]["state"] == "completed"


def test_batch_pause_emits_run_paused(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project, max_attempts=1, breaker_threshold=9)
    eng.create_run("r1")
    for i in range(4):
        eng.add_task("r1", f"t{i}")

    def failing(work):
        return [make_result(w, status=ResultStatus.FAILURE, error="systemic")
                if w.stage is not Stage.INTAKE else make_result(w) for w in work]

    Scheduler(eng, max_concurrent=1, batch_failure_threshold=3).run("r1", failing)

    paused = [p for k, p in calls if k == "run_paused"]
    assert len(paused) == 1
    assert "circuit breaker" in paused[0]["reason"]
    assert "PAUSED" in paused[0]["summary"]


# --- best-effort isolation + audit rows ------------------------------------------

def test_raising_notify_hook_never_breaks_record(tmp_path) -> None:
    project = FakeProject()

    def boom(kind, payload):
        raise RuntimeError("hook exploded")

    project.notify = boom
    eng = _engine(tmp_path, project, max_attempts=1)
    _drive_intake(eng)
    w = eng.next_work("r1", "t1")
    # record() must NOT propagate the hook's exception.
    out = eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom",
                                       structured_output={}))
    assert out["task_state"] == "failed"

    events = eng.store.read_events("r1")
    # The notification audit row is still written (best-effort call, then the failure).
    assert [e for e in events if e["type"] == "notification"], "notification rows expected"
    assert [e for e in events if e["type"] == "notify_failed"], "notify_failed expected"
    nf = next(e for e in events if e["type"] == "notify_failed")
    assert "hook exploded" in nf["error"]


def test_notification_rows_written_without_a_hook(tmp_path, project) -> None:
    # The default FakeProject has NO notify hook (getattr returns None) — everything
    # still works AND the audit rows are still appended.
    assert not hasattr(project, "notify")
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    events = eng.store.read_events("r1")
    notifs = [e for e in events if e["type"] == "notification"]
    assert notifs, "notification audit rows appended even with no hook"
    assert any(e["kind"] == "run_finalized" for e in notifs)
    # No hook installed → no notify_failed rows.
    assert [e for e in events if e["type"] == "notify_failed"] == []


def test_scheduler_stall_emits_once(tmp_path) -> None:
    project, calls = _recording_project()
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # Dispatch leaves the task RUNNING with an outstanding lease (a stuck stage).
    eng.next_work("r1", "t1")
    sched = Scheduler(eng)

    # stale_after_s=-1 → any age (>= 0) counts as stale for a non-terminal task.
    sent = sched._alert_stale("r1", set(), stale_after_s=-1)
    assert sent == {"t1"}
    stale_calls = [p for k, p in calls if k == NOTIFY_TASK_STALE]
    assert len(stale_calls) == 1

    # Second poll with the threaded dedupe set → no re-ping.
    sched._alert_stale("r1", sent, stale_after_s=-1)
    assert len([p for k, p in calls if k == NOTIFY_TASK_STALE]) == 1


# --- the shared watch loop (sleep-free) ------------------------------------------

class _ScriptedEngine:
    """A stand-in engine for watch(): yields scripted status snapshots and records
    every emit_notification call, so the loop is drivable without real sleeping."""

    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = snapshots
        self.notified: list[tuple[str, dict]] = []

    def status(self, run_id: str, *, stale_after_s: int = 1800) -> dict:
        return self._snapshots.pop(0)

    def emit_notification(self, run_id: str, kind: str, payload: dict) -> None:
        self.notified.append((kind, payload))


def test_watch_polls_dedupes_and_exits_on_terminal() -> None:
    # Poll 1: A stale (fires). Poll 2: A still stale (no re-ping). Poll 3: terminal.
    stale_task = {"A": {"stale": True, "seconds_since_update": 2000,
                        "current_stage": "implement", "state": "running"}}
    eng = _ScriptedEngine([
        _status("r1", stale_task, "running"),
        _status("r1", stale_task, "running"),
        _status("r1", {"A": {"stale": False, "seconds_since_update": 1,
                             "current_stage": "review", "state": "completed"}}, "completed"),
    ])
    slept: list[int] = []
    lines: list[str] = []
    final = watch(eng, "r1", interval=5, sleeper=slept.append, emit=lines.append)

    assert final["run_state"] == "completed"
    stale_notes = [k for k, _ in eng.notified if k == NOTIFY_TASK_STALE]
    assert len(stale_notes) == 1  # once, despite two stale polls
    assert len(lines) == 1  # the human line printed once too
    assert slept == [5, 5]  # slept between the three polls, not after the terminal one
