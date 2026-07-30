"""Resume a run whose driver was killed MID-dispatch (#313).

The kill that `Scheduler.run` did not survive is the likely one: `run-headless` is a
long-lived foreground process, so Ctrl-C lands while dispatches are outstanding. Each
in-flight task is left RUNNING with a dispatch lease held, `dispatchable()` correctly
excludes leased tasks, and the next invocation therefore found nothing to do and returned
the ordinary end-of-run status dump — byte-indistinguishable from a successful no-op.

This pins both halves of the fix:
  - the startup reclaim releases the leases OUR dead driver left, re-dispatching at the
    SAME attempt (no retry budget spent) so the run completes with no manual `record`;
  - a lease we may NOT reclaim (live driver, unclaimed run, foreign host) is never stolen,
    and the loop says so loudly (distinct exit reason, warning event, notification,
    non-zero `run-headless` exit) instead of exiting silently.
"""

from __future__ import annotations

import json
import socket

import pytest

from orchestrator.alerting import activity_lines
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.scheduler import (
    EXIT_BLOCKED_ORPHANED,
    EXIT_DONE,
    Scheduler,
)
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.status import Run, RunDriver
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


class SimRunner:
    """Simulated execution lane recording (task, stage, attempt) of every dispatch."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int]] = []

    def __call__(self, workitems):
        out = []
        for w in workitems:
            self.seen.append((w.task_id, w.stage.value, w.attempt))
            out.append(make_result(w))
        return out


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _mid_dispatch(eng: Engine, *, run: str = "r1", task: str = "t1") -> str:
    """Drive a task to an OUTSTANDING scope dispatch (lease held, no result recorded) —
    the zombie state a driver killed mid-dispatch leaves behind. Returns the leased id."""
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake completes
    work = eng.next_work(run, task)  # scope dispatched, never recorded
    assert work.stage is Stage.SCOPE
    doc = eng.store.load_task(run, task)
    assert doc.pending_work_item_id == work.id
    assert doc.stages[Stage.SCOPE].attempt == 0
    return work.id


def _stamp_driver(eng: Engine, run_id: str, *, pid: int, host: str | None = None) -> None:
    """Forge the driver claim a (now dead or still live) other process would have left."""

    def _set(run: Run) -> None:
        run.driver = RunDriver(
            host=host if host is not None else socket.gethostname(),
            pid=pid,
            claimed_at="2026-07-30T00:00:00+00:00",
        )

    eng.store.update_run(run_id, _set)


def _events(eng: Engine, run_id: str, etype: str) -> list[dict]:
    return [e for e in eng.store.read_events(run_id) if e.get("type") == etype]


# --- the reclaim itself -------------------------------------------------------


def test_scheduler_resumes_own_orphaned_lease_at_the_same_attempt(tmp_path, project) -> None:
    # THE acceptance criterion: re-invoking the driver on a run whose leases it left behind
    # drives the run to completion with no manual `record`, and no attempt is consumed.
    eng = _engine(tmp_path, project)
    leased = _mid_dispatch(eng)
    eng.claim_run_driver("r1")  # the killed driver was this process (same-owner reclaim)

    runner = SimRunner()
    status = Scheduler(eng, max_concurrent=1).run("r1", runner)

    assert status["run_state"] == "completed"
    assert status["tasks"]["t1"]["state"] == "completed"
    # The orphaned stage was re-dispatched exactly once, at attempt 0 — the SAME attempt.
    # A retry (the old hand-crafted-`timeout` recovery) would show attempt 1 here.
    assert [(s, a) for (_t, s, a) in runner.seen if s == "scope"] == [("scope", 0)]
    assert all(attempt == 0 for (_t, _s, attempt) in runner.seen)
    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].attempt == 0

    # ...and it happened because of the reclaim, not by accident.
    sched = status["scheduler"]
    assert sched["exit_reason"] == EXIT_DONE
    assert [r["work_item_id"] for r in sched["reclaimed"]] == [leased]
    assert sched["reclaimed"][0] == {
        "task_id": "t1", "stage": "scope", "attempt": 0, "work_item_id": leased
    }
    assert sched["driver_at_start"]["state"] == "mine"


def test_reclaim_closes_the_retired_lease_in_the_audit(tmp_path, project) -> None:
    # Never silent, and the #175 balance still closes: the released lease gets its own
    # warning-grade `dispatch_reclaimed` event (the counterpart of `lease_superseded`), so
    # the retired work_item_id is CLOSED rather than flagged as an orphan.
    eng = _engine(tmp_path, project)
    leased = _mid_dispatch(eng)
    eng.claim_run_driver("r1")
    status = Scheduler(eng, max_concurrent=1).run("r1", SimRunner())

    evs = _events(eng, "r1", "dispatch_reclaimed")
    assert len(evs) == 1
    assert evs[0]["work_item_id"] == leased
    assert evs[0]["task_id"] == "t1" and evs[0]["stage"] == "scope"
    assert evs[0]["attempt"] == 0
    assert evs[0]["severity"] == "warning"
    assert evs[0]["driver_state"] == "mine"

    audit = status["events_audit"]
    assert audit["clean"] is True and audit["orphans"] == []
    assert audit["reclaimed"] == 1
    assert audit["dispatched"] == (
        audit["recorded"] + audit["superseded"] + audit["abandoned"] + audit["reclaimed"]
    )


def test_dead_driver_pid_on_this_host_is_reclaimed(tmp_path, project, monkeypatch) -> None:
    # The real-world shape: the claim names ANOTHER pid on this host, and that process is
    # gone. Same-owner reclaim covers it, and the classification says exactly why.
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    _stamp_driver(eng, "r1", pid=424242)
    monkeypatch.setattr("orchestrator.engine._pid_alive", lambda _pid: False)

    status = Scheduler(eng, max_concurrent=1).run("r1", SimRunner())

    assert status["run_state"] == "completed"
    assert status["scheduler"]["driver_at_start"] == {
        "state": "dead", "host": socket.gethostname(), "pid": 424242,
        "claimed_at": "2026-07-30T00:00:00+00:00", "reclaimable": True,
    }
    # The new driver took ownership, so a THIRD invocation would classify against us.
    assert eng.driver_claim("r1")["state"] == "mine"


# --- the leases we must never steal -------------------------------------------


@pytest.mark.parametrize(
    ("setup", "expected_state"),
    [
        # A live driver on this host is finishing those dispatches right now.
        ("live", "live"),
        # Nobody ever claimed this run — the per-task CLI supervisor holds live leases
        # exactly this way, so an unclaimed lease is NOT provably an orphan.
        ("unclaimed", "unclaimed"),
        # A pid from another machine says nothing about a local process.
        ("foreign_host", "foreign_host"),
    ],
)
def test_unreclaimable_leases_are_left_alone_and_reported(
    tmp_path, project, monkeypatch, setup: str, expected_state: str
) -> None:
    eng = _engine(tmp_path, project)
    leased = _mid_dispatch(eng)
    if setup == "live":
        _stamp_driver(eng, "r1", pid=424242)
        monkeypatch.setattr("orchestrator.engine._pid_alive", lambda _pid: True)
    elif setup == "foreign_host":
        _stamp_driver(eng, "r1", pid=424242, host="some-other-box")

    runner = SimRunner()
    status = Scheduler(eng, max_concurrent=1).run("r1", runner)

    # Nothing stolen, nothing dispatched — a double-dispatch of the live stage is the
    # failure this conservatism exists to prevent.
    assert runner.seen == []
    assert eng.store.load_task("r1", "t1").pending_work_item_id == leased
    assert _events(eng, "r1", "dispatch_reclaimed") == []

    # ...and the exit is loud rather than a status dump that looks like completion.
    sched = status["scheduler"]
    assert sched["exit_reason"] == EXIT_BLOCKED_ORPHANED
    assert sched["reclaimed"] == []
    assert sched["driver_at_start"]["state"] == expected_state
    assert sched["in_flight"] == ["t1"]
    assert "t1" in sched["message"] and "abandon" in sched["message"]
    assert status["run_state"] != "completed"

    blocked = _events(eng, "r1", "scheduler_exit_blocked")
    assert len(blocked) == 1
    assert blocked[0]["severity"] == "warning"
    assert blocked[0]["in_flight"] == ["t1"]
    notes = [e for e in _events(eng, "r1", "notification") if e.get("kind") == "run_blocked"]
    assert len(notes) == 1 and "t1" in notes[0]["summary"]


def test_claiming_never_overwrites_a_live_foreign_driver(tmp_path, project, monkeypatch) -> None:
    # Ownership evidence must survive: overwriting a LIVE driver's claim would make its
    # leases look reclaimable to the next invocation.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    _stamp_driver(eng, "r1", pid=424242)
    monkeypatch.setattr("orchestrator.engine._pid_alive", lambda _pid: True)

    claim = eng.claim_run_driver("r1")

    assert claim["state"] == "live" and claim["pid"] == 424242
    assert eng.store.load_run("r1").driver.pid == 424242
    assert _events(eng, "r1", "driver_claimed") == []


def test_a_clean_run_claims_the_driver_and_exits_done(tmp_path, project) -> None:
    # No orphans: the reclaim is a no-op, the run still claims ownership (so a kill DURING
    # this run is recoverable), and the exit reason is ordinary completion.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    status = Scheduler(eng, max_concurrent=1).run("r1", SimRunner())

    assert status["run_state"] == "completed"
    assert status["scheduler"] == {
        "exit_reason": EXIT_DONE, "reclaimed": [], "reclaim_skipped": [],
        "driver_at_start": {"state": "unclaimed", "host": None, "pid": None, "claimed_at": None,
                   "reclaimable": False},
        "in_flight": [],
    }
    assert status["driver"]["state"] == "mine"
    assert _events(eng, "r1", "scheduler_exit_blocked") == []
    assert len(_events(eng, "r1", "driver_claimed")) == 1


# --- operator surfaces ---------------------------------------------------------


def test_cli_run_headless_exits_non_zero_when_blocked(tmp_path, capsys) -> None:
    from orchestrator.cli import main

    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]

    def run(*argv, expect: int = 0):
        assert main([*base, *argv]) == expect
        out = capsys.readouterr()
        return json.loads(out.out.strip()) if out.out.strip() else None, out.err

    run("init-run", "--lane", "full")
    run("add-task", "--task", "#42")
    run("next", "--task", "#42")  # intake WorkItem dispatched and never recorded

    status, err = run("run-headless", expect=1)

    assert status["scheduler"]["exit_reason"] == EXIT_BLOCKED_ORPHANED
    assert status["scheduler"]["in_flight"] == ["#42"]
    assert "abandon" in err  # the operator is told how to get out of it


def test_watch_activity_names_a_dead_driver_instead_of_blaming_the_model() -> None:
    # A frozen stream has two causes with opposite responses. With the driver gone, the
    # line must point at the driver, not at the model.
    snapshot = {
        "driver": {"state": "dead", "pid": 424242},
        "tasks": {
            "t1": {"current_stage": "implement",
                   "activity": {"current_activity": {"tool": "Bash", "detail": "pytest"},
                                "events_seen": 12, "seconds_since_event": 900}},
        },
    }
    (line,) = activity_lines(snapshot, stall_after_s=300)
    assert "NO LIVE DRIVER" in line and "424242" in line
    assert "STREAM STALLED" not in line

    # A live driver keeps the original (model-facing) stall wording.
    snapshot["driver"] = {"state": "mine", "pid": 1}
    (line,) = activity_lines(snapshot, stall_after_s=300)
    assert "STREAM STALLED" in line and "NO LIVE DRIVER" not in line
