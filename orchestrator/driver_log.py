"""Durable driver telemetry: ``runs/<run>/driver.jsonl`` (#323).

``Scheduler.run`` is the DRIVER — one long-lived foreground process that owns a run for
its whole duration. Until this module it left no record of its own existence: a driver
that died mid-batch could not be dated, and a driver legitimately sleeping through a
capacity stall was byte-indistinguishable from a wedged one (both silent). Every fact the
live driver knew — which tick it was on, the utilization it was gated by, how long it was
about to sleep, and why it stopped — evaporated with the process.

The record is a file, not stdout, for the same reason ``events.jsonl`` is: it must survive
the process, a closed terminal, and a lost session (and it is a run log, so the
retain-run-logs rule covers it — nothing prunes it automatically). An ``echo`` callback
mirrors each line to a stream on top; stdout is never the only channel.

Three record types, all appended as JSON lines under the same file lock the status store
uses for ``events.jsonl``:

* ``driver_start`` — pid/ppid/host/argv and the RESOLVED loop settings, once at launch;
* ``driver_heartbeat`` — one per tick before the dispatch it is about to make, plus one
  per sleep slice while waiting, carrying tick number, state, utilization, the computed
  dispatch limit, and the dispatchable/in-flight counts;
* ``driver_exit`` — every catchable termination: an ordinary exit reason, an exception, or
  a trapped signal. SIGKILL cannot be caught, which is exactly why heartbeats carry
  ``next_heartbeat_within_s``: a stale heartbeat bounds the time of death and names the
  state the driver was last in.

Reading is deliberately tolerant of a torn trailing line: a driver killed mid-append (or
an ENOSPC) must not make its own log unreadable to the ``status`` surface that diagnoses
it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from .status_store import file_lock

DRIVER_LOG_NAME = "driver.jsonl"

REC_START = "driver_start"
REC_HEARTBEAT = "driver_heartbeat"
REC_EXIT = "driver_exit"

# Wall-clock cadence for heartbeats emitted while the driver is SLEEPING (a capacity stall
# or a rate-limit cooldown). A tick's own heartbeat is emitted before its dispatch, so the
# gap between heartbeats while work is in flight is the stage's own duration — see
# ``next_heartbeat_within_s`` below for how staleness is judged without lying about that.
DEFAULT_HEARTBEAT_INTERVAL_S = 60

# Grace applied to a heartbeat's own ``next_heartbeat_within_s`` before the driver is
# called stale: a promise to beat again within N seconds is only broken well past N (the
# sleeper is not a real-time scheduler, and a status poll may race the next append).
_STALE_GRACE_FACTOR = 2.0
_STALE_GRACE_FLOOR_S = 15.0

# States a heartbeat can report. ``dispatching`` is the only one that makes NO promise
# about the next beat: the driver is blocked in the execution lane for as long as the
# stage takes, so a long quiet gap there is normal and must not be reported as stale.
STATE_DISPATCHING = "dispatching"
STATE_PLANNING = "planning"  # capacity probed, nothing to hand to the lane this pass
STATE_WAITING_CAPACITY = "waiting_on_capacity"
STATE_WAITING_COOLDOWN = "waiting_on_cooldown"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _age_s(ts: str, *, now: datetime | None = None) -> float | None:
    """Seconds between ``ts`` and now, or None when ``ts`` is unparseable."""
    try:
        then = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:  # a naive stamp is treated as UTC (our writers always tz)
        then = then.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - then).total_seconds())


def driver_log_path(root: Path) -> Path:
    """The path a driver log lives at for a run rooted at ``root``.

    Shared by ``DriverLog`` and every reader so there is exactly one place that decides
    where ``driver.jsonl`` sits alongside ``events.jsonl``.
    """
    return Path(root) / DRIVER_LOG_NAME


class DriverLog:
    """Append-only telemetry for ONE driver process on one run.

    Construction is side-effect free; the file appears with the first record. The
    instance also carries the driver's live state (tick number, last state) so the signal
    handler's last-gasp record can name what the loop was doing without threading that
    context through every call site.
    """

    def __init__(
        self,
        root: Path,
        run_id: str,
        *,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self.path = driver_log_path(root)
        self.run_id = run_id
        self._echo = echo
        self.tick = 0
        self.state: str | None = None
        self._exit_written = False
        self._previous_handlers: dict[int, Any] = {}

    # ---- writing ---------------------------------------------------------

    def _append(self, record: dict, *, lock: bool = True) -> dict:
        """Append one JSON line. ``lock`` may be dropped ONLY from a signal handler.

        The handler runs on the main thread between bytecodes, so it can interrupt an
        append that is already holding this file's ``flock`` — and flock is per
        open-file-description, so the handler's own acquisition would block on a lock its
        own thread holds and wedge the process on the very Ctrl-C it is recording. The
        lock is belt-and-suspenders anyway: a sub-PIPE_BUF line written to an O_APPEND fd
        is atomic on POSIX (the same reasoning ``StatusStore.append_event`` documents), and
        a torn line is tolerated by the reader regardless.
        """
        line = json.dumps(record, separators=(",", ":"))
        try:
            with ExitStack() as stack:
                if lock:
                    stack.enter_context(file_lock(self.path))
                fh = stack.enter_context(open(self.path, "a", encoding="utf-8"))
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            # Telemetry must never take the run down with it: a full or read-only run dir
            # costs us the record, not the batch. The echo below still reaches the operator.
            pass
        if self._echo is not None:
            # A closed/broken mirror stream (the terminal went away) is not the run's
            # problem — the durable line is already on disk.
            with contextlib.suppress(OSError, ValueError):
                self._echo(line)
        return record

    def start(self, *, settings: dict, reclaim: dict | None = None) -> dict:
        """The launch record: who this process is, and the settings it actually resolved.

        ``argv`` and ``ppid`` are here because the post-mortem question is "what was
        launched, by what, with which flags" — the run doc knows none of that.
        """
        return self._append({
            "ts": _now(), "type": REC_START, "run_id": self.run_id,
            "pid": os.getpid(), "ppid": os.getppid(), "host": socket.gethostname(),
            "argv": list(sys.argv), "settings": settings,
            "reclaimed": (reclaim or {}).get("reclaimed", []),
            "driver_at_start": (reclaim or {}).get("driver"),
        })

    def heartbeat(
        self,
        *,
        state: str,
        tick: int | None = None,
        util_pct: float | None = None,
        dispatch_limit: int | None = None,
        dispatchable: int | None = None,
        in_flight: int | None = None,
        next_heartbeat_within_s: float | None = None,
        **extra: object,
    ) -> dict:
        """One heartbeat. ``next_heartbeat_within_s`` is the driver's own promise about
        when the next one is due — the ONLY basis on which a reader may call it stale."""
        if tick is not None:
            self.tick = tick
        self.state = state
        return self._append({
            "ts": _now(), "type": REC_HEARTBEAT, "run_id": self.run_id,
            "pid": os.getpid(), "tick": self.tick, "state": state,
            "util_pct": util_pct, "dispatch_limit": dispatch_limit,
            "dispatchable": dispatchable, "in_flight": in_flight,
            "next_heartbeat_within_s": next_heartbeat_within_s,
            **extra,
        })

    def exit(self, reason: str, *, lock: bool = True, **extra: object) -> dict | None:
        """The termination record. Idempotent BY DESIGN: a trapped SIGINT writes its
        last-gasp record and then re-raises, so the KeyboardInterrupt unwinding the loop
        would otherwise append a second, less informative exit for the same death."""
        if self._exit_written:
            return None
        self._exit_written = True
        return self._append({
            "ts": _now(), "type": REC_EXIT, "run_id": self.run_id, "pid": os.getpid(),
            "reason": reason, "tick": self.tick, "last_state": self.state, **extra,
        }, lock=lock)

    # ---- signal trap -----------------------------------------------------

    @contextmanager
    def trap_signals(self) -> Iterator[None]:
        """Record an exit for SIGTERM/SIGINT/SIGHUP, then let the ORIGINAL handler run.

        Termination behavior is deliberately unchanged: the previous handler is restored
        and the signal re-sent to this process, so SIGINT still raises KeyboardInterrupt
        and SIGTERM still kills the process the way the default disposition does. Off the
        main thread (and on a platform missing a signal) installing is impossible — the
        trap is then a no-op rather than an error, since a driver embedded in a thread is
        somebody else's process to terminate.
        """
        installed: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGTERM", "SIGINT", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is None:  # pragma: no cover - platform dependent
                    continue
                try:
                    installed[int(sig)] = signal.signal(sig, self._on_signal)
                except (ValueError, OSError, RuntimeError):  # pragma: no cover - defensive
                    continue
        self._previous_handlers = installed
        try:
            yield
        finally:
            for signum, previous in installed.items():
                with contextlib.suppress(ValueError, OSError, RuntimeError):
                    signal.signal(signum, previous)
            self._previous_handlers = {}

    def _on_signal(self, signum: int, frame: FrameType | None) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - defensive
            name = str(signum)
        # lock=False: see `_append` — taking the file lock from a handler that may have
        # interrupted an append already holding it would deadlock the process.
        self.exit(f"signal:{name}", lock=False, signal=name, signum=signum)
        # Hand the signal back to whatever was handling it BEFORE us and re-send it, so
        # this process dies exactly as it would have untrapped: SIGINT still reaches
        # Python's default handler and raises KeyboardInterrupt (unwinding the loop's
        # `finally`s), SIGTERM still takes the default disposition. Restoring SIG_DFL
        # unconditionally here would silently turn Ctrl-C into an abrupt kill.
        previous = self._previous_handlers.get(signum, signal.SIG_DFL)
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(signum, previous)
        os.kill(os.getpid(), signum)


# ---- reading -------------------------------------------------------------


def read_records(root: Path, *, run_id: str | None = None) -> list[dict]:
    """Every parseable record, oldest first.

    A torn or truncated line is SKIPPED, not raised on: the file's whole purpose is to be
    readable after the process that was appending to it died mid-write.
    """
    path = driver_log_path(root)
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - defensive
        return []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # torn append (crash/ENOSPC) — the rest of the log still counts
        if not isinstance(rec, dict):
            continue
        if run_id is None or rec.get("run_id") == run_id:
            out.append(rec)
    return out


def last_record(root: Path, *, run_id: str | None = None) -> dict | None:
    """The most recent parseable record (any type), or None if the log is empty/absent."""
    records = read_records(root, run_id=run_id)
    return records[-1] if records else None


def liveness_from_log(
    root: Path, claim: dict, *, run_id: str | None = None, now: datetime | None = None
) -> dict:
    """The ``claim`` (#313's pid classification, untouched) plus what the driver log says.

    Two independent sensors, reported together because either alone misleads:

    * the claim answers "does that pid still exist" — but a pid that exists says nothing
      about a loop that has stopped looping;
    * the log answers "when did the driver last speak, and what was it doing" — but a
      quiet log is normal while a stage is dispatching.

    ``alive`` is the merged verdict for an operator: True only when the process exists,
    has not recorded an exit, and has not broken its own ``next_heartbeat_within_s``
    promise. ``None`` means unknowable (a claim from another host, where a local pid probe
    means nothing). The claim's own ``state``/``reclaimable`` are passed through unchanged
    — lease reclaim safety (#313) is decided by the pid, never by a heartbeat.
    """
    records = read_records(root, run_id=run_id)
    start = next((r for r in records if r.get("type") == REC_START), None)
    beats = [r for r in records if r.get("type") == REC_HEARTBEAT]
    exit_rec = next((r for r in reversed(records) if r.get("type") == REC_EXIT), None)
    last_beat = beats[-1] if beats else None

    age = _age_s(last_beat["ts"], now=now) if last_beat else None
    promised = last_beat.get("next_heartbeat_within_s") if last_beat else None
    stale = False
    if age is not None and isinstance(promised, (int, float)):
        stale = age > max(promised * _STALE_GRACE_FACTOR, promised + _STALE_GRACE_FLOOR_S)

    state = claim.get("state")
    process_alive: bool | None
    if state in ("mine", "live"):
        process_alive = True
    elif state in ("dead", "unclaimed"):
        process_alive = False
    else:  # foreign_host — a pid on another machine is not ours to judge
        process_alive = None

    alive: bool | None
    if process_alive is False or exit_rec is not None or stale:
        alive = False
    elif process_alive is True:
        alive = True
    else:
        alive = None

    return {
        **claim,
        "alive": alive,
        "started_at": start.get("ts") if start else None,
        "last_heartbeat_ts": last_beat.get("ts") if last_beat else None,
        "heartbeat_age_s": round(age, 1) if age is not None else None,
        "heartbeat_stale": stale,
        "last_state": last_beat.get("state") if last_beat else None,
        "tick": last_beat.get("tick") if last_beat else None,
        "heartbeats": len(beats),
        "exited": exit_rec is not None,
        "exit_reason": exit_rec.get("reason") if exit_rec else None,
        "exit_ts": exit_rec.get("ts") if exit_rec else None,
        "log": str(driver_log_path(root)),
    }
