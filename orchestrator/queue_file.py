"""Queue-file ingestion for unattended (cron) batch runs (#1).

Ports the as-built scheduler §1.8 (`ralph-queue.json`): a JSON array of batch entries
``{tasks, branch, enqueued_at}`` that an unattended launch drains one batch at a time.
This is the credit/headless lane's front door — the human is not in the loop, so the
entrypoint is a file the operator (or a cron `--enqueue`) appends to, and a long-running
``run-queue`` process that ingests each head batch and drives it to terminal through the
existing, already-resumable ``Scheduler.run``.

Two halves live here:

* ``QueueFile`` — a thin, project-agnostic abstraction over the queue JSON. Every
  mutation (``append`` at the tail, the single consumer's ``pop_head`` / ``requeue_head``)
  is a read-modify-write of the whole array, so each is guarded by an exclusive advisory
  lock (``fcntl.flock``, mkdir-spin fallback) and finished with an atomic temp-file +
  ``os.replace``. The lock is what makes concurrent producers safe: §1.8's bare append
  assumed a single enqueuer, but two parallel cron ``--enqueue`` invocations would
  otherwise both read the old array and one entry would be lost — the lock serializes
  them instead. Malformed content raises the typed ``QueueError``.
* ``drive_queue`` — the unattended loop. It pops the head batch, derives a STABLE run id
  from ``enqueued_at`` (so a crashed-then-restarted driver reuses the same run rather than
  forking a duplicate), creates-or-reuses that run, adds each task in listed order, then
  hands the run to ``Scheduler.run``. On an ingest failure the batch is re-prepended
  (``requeue_head``) so nothing is silently dropped. Between batches it idle-waits, polling
  every ``poll_interval_s`` up to ``idle_timeout_s`` before exiting.

The engine is never touched directly for model work — ``drive_queue`` only calls the same
public ``create_run`` / ``add_task`` / ``Scheduler.run`` surface a supervisor would, so it
stays project-agnostic and inherits every engine guarantee (idempotent adds, resumability).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import OrchestratorError, StatusStoreError
from .scheduler import Runner, Scheduler
from .schemas.enums import ExecutionLane

try:  # fcntl is POSIX-only; the mkdir-spin fallback covers the rest (e.g. Windows).
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False


class QueueError(OrchestratorError):
    """A queue file was malformed or an entry failed validation."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_entry(
    tasks: list[str], branch: str | None = None, *, enqueued_at: str | None = None
) -> dict:
    """Build a well-formed batch entry. ``enqueued_at`` defaults to now (the enqueue
    timestamp is what the driver derives a stable run id from, so it must always be set)."""
    if not tasks:
        raise QueueError("a queue entry needs a non-empty `tasks` list")
    return {
        "tasks": [str(t) for t in tasks],
        "branch": branch,
        "enqueued_at": enqueued_at or _utc_now_iso(),
    }


def _validate_entry(entry: Any, where: str) -> dict:
    """Validate one batch entry, raising ``QueueError`` (with position) on any violation."""
    if not isinstance(entry, dict):
        raise QueueError(f"{where}: batch entry must be a JSON object, got {type(entry).__name__}")
    tasks = entry.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise QueueError(f"{where}: `tasks` must be a non-empty array of task ids")
    if not all(isinstance(t, str) and t.strip() for t in tasks):
        raise QueueError(f"{where}: every `tasks` entry must be a non-empty string")
    branch = entry.get("branch")
    if branch is not None and not isinstance(branch, str):
        raise QueueError(f"{where}: `branch` must be a string or null")
    enqueued_at = entry.get("enqueued_at")
    if not isinstance(enqueued_at, str) or not enqueued_at.strip():
        raise QueueError(f"{where}: `enqueued_at` must be a non-empty timestamp string")
    return entry


class QueueFile:
    """A ``ralph-queue.json``-style queue of batch entries backed by a JSON array file.

    A missing file reads as an empty queue. Every write is atomic (temp-file in the same
    directory + ``os.replace``) so a crash mid-write can never corrupt the queue. Every
    mutation is a whole-array read-modify-write, so each holds an exclusive advisory lock
    (``.lock`` sentinel next to the queue) for its full read→write span: this is what keeps
    concurrent enqueuers safe. §1.8 originally called ``append`` "lock-free" on a
    single-producer assumption, but two parallel cron ``--enqueue`` runs would each read
    the old array and lose one entry without the lock. ``pop_head`` / ``requeue_head`` are
    the single unattended consumer's dequeue and its undo; they take the same lock so they
    never race a producer either.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _with_lock(self) -> Iterator[None]:
        """Hold an exclusive advisory lock over the queue for one read-modify-write span,
        so concurrent producers/consumers serialize instead of clobbering each other. Ports
        the status-store contract: ``fcntl.flock(LOCK_EX)`` on a ``<name>.lock`` sentinel,
        with an ``os.mkdir``-spin fallback where fcntl is unavailable. The sentinel is left
        in place on release (deleting it would race two writers onto different inodes and
        break mutual exclusion) — it is a tiny, harmless companion to the queue file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _HAVE_FCNTL:
            lock_file = self.path.with_name(f"{self.path.name}.lock")
            fh = open(lock_file, "w")  # noqa: SIM115 - held across the yield
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()
        else:  # pragma: no cover - exercised only without fcntl
            lock_dir = self.path.with_name(f"{self.path.name}.lockdir")
            while True:
                try:
                    os.mkdir(lock_dir)
                    break
                except FileExistsError:
                    time.sleep(0.01)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    os.rmdir(lock_dir)

    def read(self) -> list[dict]:
        """Return the queue as a list of validated entries. Missing file → ``[]``. Raises
        ``QueueError`` on unparseable JSON, a non-array top level, or a malformed entry."""
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise QueueError(f"queue file is not valid JSON ({self.path}): {exc}") from exc
        if not isinstance(raw, list):
            raise QueueError(
                f"queue file must be a JSON array of batch entries ({self.path}), "
                f"got {type(raw).__name__}"
            )
        return [_validate_entry(e, f"entry[{i}]") for i, e in enumerate(raw)]

    def _write(self, entries: list[dict]) -> None:
        """Atomically replace the queue with ``entries`` (temp-file + ``os.replace``)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".queue-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def append(self, entry: dict) -> dict:
        """Append one validated batch entry to the tail and return it. The read→add→write
        is a single unit under ``_with_lock`` so concurrent enqueuers can't each read the
        old array and drop one entry (§1.8's single-producer assumption made explicit)."""
        _validate_entry(entry, "appended entry")
        with self._with_lock():
            self._write([*self.read(), entry])
        return entry

    def peek_head(self) -> dict | None:
        """Return the head batch without removing it, or ``None`` when the queue is empty.

        Intentionally *not* locked: safe only under the single-consumer assumption. With
        multiple concurrent consumers a stale read is possible — the head may be popped by
        another consumer between this peek and any follow-up action."""
        entries = self.read()
        return entries[0] if entries else None

    def pop_head(self) -> dict | None:
        """Remove and return the head batch (FIFO dequeue), or ``None`` when empty. Takes
        ``_with_lock`` so the read→write can't race a concurrent producer's append."""
        with self._with_lock():
            entries = self.read()
            if not entries:
                return None
            head, rest = entries[0], entries[1:]
            self._write(rest)
        return head

    def requeue_head(self, entry: dict) -> None:
        """Re-prepend ``entry`` to the front of the queue (the ingest-failure undo, so a
        popped-then-unprocessable batch is retried first, not dropped). Locked so the
        read→write can't race a concurrent producer's append."""
        _validate_entry(entry, "requeued entry")
        with self._with_lock():
            self._write([entry, *self.read()])


def run_id_for(entry: dict, *, prefix: str = "queue") -> str:
    """Derive a STABLE run id from a batch entry's ``enqueued_at`` timestamp. Stable so a
    driver that crashes and restarts reuses the same run (create-or-reuse) instead of
    forking a duplicate for the same batch. Timestamp punctuation is normalized to ``-``."""
    stamp = re.sub(r"[^0-9a-zA-Z]+", "-", entry.get("enqueued_at", "")).strip("-")
    return f"{prefix}-{stamp}" if stamp else prefix


def _run_exists(engine: Engine, run_id: str) -> bool:
    try:
        engine.store.load_run(run_id)
        return True
    except StatusStoreError:
        return False


def _ingest_batch(
    engine: Engine, entry: dict, run_id: str, *, lane: ExecutionLane
) -> list[str]:
    """Create-or-reuse ``run_id`` and add each of the batch's tasks in listed order. Fully
    idempotent so a requeued/restarted ingest converges: an existing run is reused and an
    already-added task is skipped (mirrors ``batch_plan.apply_plan``'s per-task add loop,
    minus the DAG — a queue entry is a flat task list with no encoded edges)."""
    if not _run_exists(engine, run_id):
        engine.create_run(run_id, lane)
    run = engine.store.load_run(run_id)
    already = {ref.task_id for ref in run.task_refs}
    added: list[str] = []
    for task_id in entry["tasks"]:
        if task_id in already:
            continue
        engine.add_task(run_id, task_id)
        added.append(task_id)
    return added


def drive_queue(
    engine: Engine,
    queue: QueueFile,
    runner: Runner,
    *,
    lane: ExecutionLane = ExecutionLane.FULL,
    util_pct: float = 0.0,
    util_provider: Callable[[], float] | None = None,
    sleeper: Callable[[int], None] | None = None,
    max_concurrent: int = 3,
    idle_timeout_s: int = 300,
    poll_interval_s: int = 15,
    run_id_prefix: str = "queue",
) -> dict:
    """Drain the queue one batch at a time, driving each to terminal via ``Scheduler.run``.

    The unattended (cron) loop:

    1. Peek the head batch. If the queue is empty, idle-wait: poll every ``poll_interval_s``
       up to ``idle_timeout_s`` (using ``sleeper``), then exit. Without a ``sleeper`` the
       loop returns immediately when the queue drains (the caller owns re-invocation).
    2. Pop the head and ingest it (create-or-reuse a stable run id, add its tasks). On any
       ingest failure the batch is re-prepended (``requeue_head``) and the failure is
       re-raised — nothing is silently dropped and the same head is retried next launch.
    3. Drive the ingested run through ``Scheduler.run`` (inheriting its resumability,
       capacity-wait, circuit breaker) and record the per-run final status.

    Returns a structured summary: ``{batches_processed, runs_created, runs[...],
    idle_timed_out}`` where each ``runs`` row is ``{run_id, tasks, added, final_state}``.
    """
    runs: list[dict] = []
    created = 0
    idle_waited = 0
    idle_timed_out = False

    while True:
        head = queue.peek_head()
        if head is None:
            # Queue empty. Idle-wait when a sleeper is supplied; otherwise return (the
            # caller re-launches later — the pre-existing "no sleeper = one pass" contract
            # the Scheduler already uses).
            if sleeper is None or idle_waited >= idle_timeout_s:
                idle_timed_out = sleeper is not None
                break
            sleeper(poll_interval_s)
            idle_waited += poll_interval_s
            continue

        idle_waited = 0  # a batch arrived — reset the idle clock
        entry = queue.pop_head()
        assert entry is not None  # peek saw it; single consumer, so pop can't miss
        run_id = run_id_for(entry, prefix=run_id_prefix)
        existed = _run_exists(engine, run_id)
        try:
            added = _ingest_batch(engine, entry, run_id, lane=lane)
        except Exception as exc:
            # Ingest failed — put the batch back at the head so it is retried first, then
            # surface the failure (re-processing the same bad head in a loop would spin).
            queue.requeue_head(entry)
            raise QueueError(
                f"failed to ingest batch {entry['tasks']} (run {run_id}); re-queued at head: {exc}"
            ) from exc
        if not existed:
            created += 1

        status = Scheduler(engine, max_concurrent=max_concurrent).run(
            run_id, runner, util_pct=util_pct, util_provider=util_provider, sleeper=sleeper,
        )
        runs.append({
            "run_id": run_id,
            "tasks": entry["tasks"],
            "branch": entry.get("branch"),
            "added": added,
            "final_state": status.get("run_state"),
        })

    return {
        "batches_processed": len(runs),
        "runs_created": created,
        "runs": runs,
        "idle_timed_out": idle_timed_out,
    }
