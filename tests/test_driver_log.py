"""Driver telemetry: `runs/<run>/driver.jsonl` (#323).

`batch-headless-2`'s driver died after 45 minutes having written 139 bytes — the launch
note — so nothing could answer when it died, what it was doing, how many ticks it had run,
or whether it had received a signal. `status` reported `run_state: running` both while it
was healthy and after it was dead. The loss was entirely diagnostic; this pins the
evidence back in place.

Four properties, one per way the evidence went missing:
  - a driven run leaves a start record, a heartbeat per tick, and an exit record naming
    the cause of every CATCHABLE termination (ordinary exit, exception, signal);
  - a driver sleeping out a capacity stall keeps beating, and each beat states the reason
    and the utilization that gated it — a sleeping driver is visibly distinct from a
    stopped one (they were byte-identical: both silent);
  - a SIGKILL leaves no exit record by definition, so the last heartbeat alone must bound
    the time of death and name the state;
  - `status` reports driver liveness, so a dead or wedged driver does not present
    identically to a working one.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.driver_log import (
    REC_EXIT,
    REC_HEARTBEAT,
    REC_START,
    STATE_DISPATCHING,
    STATE_PLANNING,
    STATE_WAITING_CAPACITY,
    STATE_WAITING_COOLDOWN,
    DriverLog,
    driver_log_path,
    last_record,
    liveness_from_log,
    read_records,
)
from orchestrator.engine import Engine
from orchestrator.scheduler import EXIT_DONE, Scheduler
from orchestrator.status_store import StatusStore, file_lock
from tests.conftest import make_result


class SimRunner:
    """Simulated lane: every dispatch succeeds immediately."""

    def __call__(self, workitems):
        return [make_result(w) for w in workitems]


class BoomRunner:
    def __call__(self, workitems):
        raise RuntimeError("lane exploded")


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _records(root, *, kind: str | None = None) -> list[dict]:
    recs = read_records(root, run_id="r1")
    return [r for r in recs if kind is None or r["type"] == kind]


# --- a driven run records its whole life --------------------------------------


def test_driven_run_writes_start_heartbeats_and_an_exit_record(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    status = Scheduler(eng, max_concurrent=1).run("r1", SimRunner(), util_pct=12.5)

    assert driver_log_path(tmp_path).exists()
    (start,) = _records(tmp_path, kind=REC_START)
    assert start["pid"] == os.getpid() and start["ppid"] == os.getppid()
    assert start["host"] == socket.gethostname()
    assert start["argv"] and isinstance(start["argv"], list)
    # The RESOLVED settings — `batch-headless-2` could not tell a 45-minute life apart
    # from any mix of work and 300s waits because none of these was ever recorded.
    assert start["settings"] == {
        "max_concurrent": 1, "batch_failure_threshold": 3, "drain_wait_s": 300,
        "stale_after_s": 1800, "max_ticks": 10_000, "heartbeat_interval_s": 60,
        "util_mode": "fixed", "util_pct": 12.5, "waits": False,
    }

    beats = _records(tmp_path, kind=REC_HEARTBEAT)
    ticks = [b["tick"] for b in beats]
    assert ticks == sorted(ticks) and ticks[0] == 1
    # >= 1 heartbeat per tick, and every tick that ran is represented exactly once here
    # (this run never sleeps, so the only beats are the per-tick ones).
    assert len(set(ticks)) == len(ticks) == max(ticks)
    assert all(b["util_pct"] == 12.5 for b in beats)
    assert beats[0]["dispatch_limit"] == 1 and beats[0]["dispatchable"] == 1
    # Every pass but the last was about to hand work to the lane; the last found nothing
    # dispatchable and only ever planned to exit — a state the record must not overclaim.
    assert all(b["state"] == STATE_DISPATCHING for b in beats[:-1])
    assert beats[-1]["state"] == STATE_PLANNING and beats[-1]["dispatchable"] == 0

    (exit_rec,) = _records(tmp_path, kind=REC_EXIT)
    assert exit_rec["reason"] == EXIT_DONE == status["scheduler"]["exit_reason"]
    assert exit_rec["tick"] == max(ticks) and exit_rec["in_flight"] == []


def test_the_tick_heartbeat_precedes_the_dispatch_it_is_about_to_block_on(
    tmp_path, project
) -> None:
    # The forensic point of beating BEFORE dispatching: a driver killed mid-`implement`
    # must still have said which tick it was in and what capacity it saw. A beat written
    # after the lane returns would be missing for exactly the death being diagnosed.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    seen: list[dict | None] = []

    def runner(workitems):
        seen.append(last_record(tmp_path, run_id="r1"))
        return [make_result(w) for w in workitems]

    Scheduler(eng, max_concurrent=1).run("r1", runner)

    assert seen and all(r is not None for r in seen)
    assert all(r["type"] == REC_HEARTBEAT and r["state"] == STATE_DISPATCHING for r in seen)
    # ...and it makes no staleness promise: the next beat lands whenever the stage does.
    assert all(r["next_heartbeat_within_s"] is None for r in seen)


def test_exception_termination_is_recorded(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    with pytest.raises(RuntimeError):
        Scheduler(eng, max_concurrent=1).run("r1", BoomRunner())

    (exit_rec,) = _records(tmp_path, kind=REC_EXIT)
    assert exit_rec["reason"] == "exception:RuntimeError"
    assert "lane exploded" in exit_rec["error"]
    assert exit_rec["last_state"] == STATE_DISPATCHING and exit_rec["tick"] == 1


# --- a sleeping driver keeps talking ------------------------------------------


def test_capacity_stall_keeps_beating_with_the_reason_and_utilization(
    tmp_path, project
) -> None:
    """The misread that motivated the issue: at 90% utilization the driver can legitimately
    sleep for hours, and a silent driver is the expected appearance of both "waiting
    correctly" and "hung"."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    utils = [90.0, 90.0, 0.0]  # gated, gated, then the window resets

    def util_provider() -> float:
        return utils.pop(0) if len(utils) > 1 else utils[0]

    slept: list[int] = []
    status = Scheduler(eng, max_concurrent=1).run(
        "r1", SimRunner(), util_provider=util_provider, sleeper=slept.append,
        drain_wait_s=300, heartbeat_interval_s=60,
    )

    assert status["run_state"] == "completed"
    # The wait is unchanged in total (2 stalled ticks x 300s), only sliced so it speaks.
    assert sum(slept) == 600 and set(slept) == {60}
    beats = _records(tmp_path, kind=REC_HEARTBEAT)
    # The gated tick says so before it sleeps: work was ready, capacity said zero.
    assert beats[0]["state"] == STATE_PLANNING
    assert beats[0]["dispatchable"] == 1 and beats[0]["dispatch_limit"] == 0
    waiting = [b for b in beats if b["state"] == STATE_WAITING_CAPACITY]
    assert len(waiting) == 10  # 300s / 60s per stalled tick, twice
    assert all(b["util_pct"] == 90.0 and b["dispatch_limit"] == 0 for b in waiting)
    assert all("dispatch limit 0" in b["reason"] for b in waiting)
    # "1 task ready, limit 0" IS the diagnosis — the counts are carried through the wait
    # rather than zeroed, or a reader could not tell a stall from a finished run.
    assert all(b["dispatchable"] == 1 and b["in_flight"] == 0 for b in waiting)
    # Each beat says how long it is about to sleep and how much of the wait is left, so a
    # reader can tell "woke at HH:MM, sleeping 60s again" from "last heartbeat 40m ago".
    assert [b["sleep_remaining_s"] for b in waiting[:5]] == [300, 240, 180, 120, 60]
    assert all(b["sleep_s"] == 60 and b["next_heartbeat_within_s"] == 60 for b in waiting)
    # `util_mode` is recorded so "was it gated by utilization?" is answerable after the fact.
    assert _records(tmp_path, kind=REC_START)[0]["settings"]["util_mode"] == "auto"


def test_rate_limit_cooldown_wait_also_beats(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    until = (datetime.now(UTC) + timedelta(seconds=100)).isoformat()
    eng.store.update_task("r1", "t1", lambda t: setattr(t, "not_before", until))

    slept: list[int] = []

    def sleeper(secs: int) -> None:
        slept.append(secs)
        eng.store.update_task("r1", "t1", lambda t: setattr(t, "not_before", None))

    Scheduler(eng, max_concurrent=1).run(
        "r1", SimRunner(), sleeper=sleeper, heartbeat_interval_s=60
    )

    cooling = [b for b in _records(tmp_path, kind=REC_HEARTBEAT)
               if b["state"] == STATE_WAITING_COOLDOWN]
    assert cooling and all("cooldown" in b["reason"] for b in cooling)
    assert sum(slept) == cooling[0]["sleep_remaining_s"]  # nothing added to or lost from it


# --- signals -------------------------------------------------------------------


def test_sigint_writes_an_exit_record_and_still_raises_keyboardinterrupt(
    tmp_path, project
) -> None:
    """SIGTERM/SIGINT produce an exit record before the process dies, and do NOT change
    the existing termination behavior otherwise: the trap restores the previous handler
    and re-sends, so Ctrl-C still unwinds as a KeyboardInterrupt (SIGTERM's default
    disposition cannot be exercised in-process without killing the test runner)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    def runner(workitems):
        os.kill(os.getpid(), signal.SIGINT)  # the Ctrl-C that killed batch-headless-2
        return [make_result(w) for w in workitems]

    with pytest.raises(KeyboardInterrupt):
        Scheduler(eng, max_concurrent=1).run("r1", runner)

    exits = _records(tmp_path, kind=REC_EXIT)
    # ONE record, and the specific one: the KeyboardInterrupt unwinding the loop must not
    # overwrite the signal that caused it with a vaguer `exception:` reason.
    assert len(exits) == 1
    assert exits[0]["reason"] == "signal:SIGINT" and exits[0]["signal"] == "SIGINT"
    assert exits[0]["signum"] == int(signal.SIGINT)
    assert exits[0]["last_state"] == STATE_DISPATCHING and exits[0]["tick"] == 1
    # The trap is scoped to the loop: every handler it displaced is back.
    assert {s: signal.getsignal(s) for s in before} == before


def test_the_last_gasp_record_never_blocks_on_the_log_lock(tmp_path) -> None:
    """A signal handler runs on the main thread between bytecodes, so it can interrupt an
    append that is already holding this file's flock — and flock is per open-file-
    description, so a handler that took the lock too would block on a lock its own thread
    holds and wedge the process on the very Ctrl-C it was recording. The exit path
    therefore appends unlocked (O_APPEND of a short line is atomic)."""
    log = DriverLog(tmp_path, "r1")
    log.start(settings={})
    done = threading.Event()

    def _last_gasp() -> None:
        log.exit("signal:SIGTERM", lock=False, signal="SIGTERM")
        done.set()

    with file_lock(log.path):  # an append in flight when the signal lands
        writer = threading.Thread(target=_last_gasp, daemon=True)
        writer.start()
        assert done.wait(timeout=10), "the last-gasp record blocked on the log lock"

    assert last_record(tmp_path, run_id="r1")["reason"] == "signal:SIGTERM"


def test_trap_installs_and_restores_every_supported_signal(tmp_path) -> None:
    log = DriverLog(tmp_path, "r1")
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    with log.trap_signals():
        assert all(signal.getsignal(s) == log._on_signal for s in before)
    assert {s: signal.getsignal(s) for s in before} == before


# --- what a SIGKILL leaves behind ----------------------------------------------


def test_sigkill_analogue_bounds_the_time_of_death_from_the_log_alone(tmp_path) -> None:
    # SIGKILL cannot be caught, so there is no exit record — the last heartbeat is the
    # ONLY evidence, and it must be enough to date the death and name the last state.
    log = DriverLog(tmp_path, "r1")
    log.start(settings={"drain_wait_s": 300})
    log.heartbeat(tick=7, state=STATE_WAITING_CAPACITY, util_pct=90.0, dispatch_limit=0,
                  dispatchable=3, in_flight=0, next_heartbeat_within_s=60)
    # ...and then the process is gone. Nothing else is ever appended.

    claim = {"state": "dead", "host": socket.gethostname(), "pid": 4242,
             "claimed_at": "2026-07-30T17:20:33+00:00", "reclaimable": True}
    now = datetime.now(UTC) + timedelta(minutes=40)
    live = liveness_from_log(tmp_path, claim, run_id="r1", now=now)

    assert live["alive"] is False and live["exited"] is False  # died without saying so
    assert live["last_state"] == STATE_WAITING_CAPACITY and live["tick"] == 7
    assert 2390 <= live["heartbeat_age_s"] <= 2410  # death bounded to a ~1 minute window
    assert live["heartbeat_stale"] is True
    assert live["state"] == "dead" and live["reclaimable"] is True  # #313 claim untouched


def test_a_torn_trailing_line_does_not_break_the_reader(tmp_path) -> None:
    # A driver killed mid-append (or an ENOSPC) must not make its own log unreadable to
    # the surface that diagnoses it.
    log = DriverLog(tmp_path, "r1")
    log.start(settings={})
    log.heartbeat(state=STATE_DISPATCHING, tick=1, next_heartbeat_within_s=None)
    with driver_log_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-07-30T18:12:5')  # the append that died halfway

    recs = read_records(tmp_path, run_id="r1")
    assert [r["type"] for r in recs] == [REC_START, REC_HEARTBEAT]
    assert last_record(tmp_path, run_id="r1")["type"] == REC_HEARTBEAT


# --- the status surface ----------------------------------------------------------


def _beat(log: DriverLog, *, ts: str, state: str, promise: float | None) -> None:
    """Append a heartbeat stamped at ``ts`` (the writer always uses now())."""
    rec = log.heartbeat(state=state, tick=3, util_pct=90.0, dispatch_limit=0,
                        next_heartbeat_within_s=promise)
    path = log.path
    lines = path.read_text(encoding="utf-8").splitlines()
    rec["ts"] = ts
    lines[-1] = json.dumps(rec, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_status_shows_a_live_driver_as_alive_and_a_stale_one_as_not(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.claim_run_driver("r1")  # this process is the driver, and it is very much alive
    log = DriverLog(tmp_path, "r1")
    log.start(settings={})

    _beat(log, ts=datetime.now(UTC).isoformat(), state=STATE_WAITING_CAPACITY, promise=60)
    driver = eng.status("r1")["driver"]
    assert driver["state"] == "mine" and driver["alive"] is True
    assert driver["heartbeat_stale"] is False and driver["last_state"] == STATE_WAITING_CAPACITY
    assert driver["heartbeat_age_s"] < 60 and driver["exited"] is False

    # The `batch-headless-2` situation: the pid still resolves (it is ours) but the loop
    # stopped beating 40 minutes ago. `run_state: running` said nothing; this does.
    stale_ts = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    _beat(log, ts=stale_ts, state=STATE_WAITING_CAPACITY, promise=60)
    driver = eng.status("r1")["driver"]
    assert driver["alive"] is False and driver["heartbeat_stale"] is True
    assert driver["heartbeat_age_s"] > 2000
    assert driver["last_state"] == STATE_WAITING_CAPACITY  # what it was last doing


def test_a_long_dispatch_is_quiet_but_never_reported_stale(tmp_path, project) -> None:
    # A stage can take half an hour; the driver is blocked in the lane, not wedged. Only a
    # heartbeat that PROMISED a successor may be called stale, so this quiet is honest.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.claim_run_driver("r1")
    log = DriverLog(tmp_path, "r1")
    log.start(settings={})
    _beat(log, ts=(datetime.now(UTC) - timedelta(minutes=40)).isoformat(),
          state=STATE_DISPATCHING, promise=None)

    driver = eng.status("r1")["driver"]
    assert driver["heartbeat_stale"] is False and driver["alive"] is True
    assert driver["last_state"] == STATE_DISPATCHING and driver["heartbeat_age_s"] > 2000


def test_status_driver_block_on_a_run_that_was_never_driven(tmp_path, project) -> None:
    # No driver log at all (per-task CLI supervisor lane): the claim still answers, and
    # nothing pretends there were heartbeats.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")

    driver = eng.status("r1")["driver"]
    assert driver["state"] == "unclaimed" and driver["alive"] is False
    assert driver["last_heartbeat_ts"] is None and driver["heartbeats"] == 0
    assert driver["heartbeat_stale"] is False and driver["exited"] is False


def test_an_exited_driver_is_not_alive_even_though_the_pid_is_ours(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    Scheduler(eng, max_concurrent=1).run("r1", SimRunner())

    driver = eng.status("r1")["driver"]
    assert driver["state"] == "mine"  # the claim is never cleared — it is the evidence
    assert driver["exited"] is True and driver["exit_reason"] == EXIT_DONE
    assert driver["alive"] is False  # ...but the loop is over, and says so


# --- the operator's terminal -------------------------------------------------------


def test_run_headless_mirrors_driver_records_to_stderr(tmp_path, capsys) -> None:
    from orchestrator.cli import main

    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    assert main([*base, "add-task", "--task", "#42"]) == 0
    assert main([*base, "next", "--task", "#42"]) == 0  # a lease nobody will record
    capsys.readouterr()

    assert main([*base, "run-headless"]) == 1  # blocked on the orphaned lease (#313)
    out = capsys.readouterr()

    mirrored = [json.loads(line) for line in out.err.splitlines() if line.startswith("{")]
    assert [r["type"] for r in mirrored][0] == REC_START
    assert REC_HEARTBEAT in {r["type"] for r in mirrored}
    assert mirrored[-1]["type"] == REC_EXIT
    # stdout stays exactly one JSON status document for scripted callers.
    assert json.loads(out.out.strip())["run_id"] == "run1"
    # ...and the durable record is the file, not the terminal that may be gone by then.
    assert [r["type"] for r in read_records(tmp_path, run_id="run1")] == [
        r["type"] for r in mirrored
    ]
