"""Queue-file ingestion for unattended (cron) batch runs (#1).

Ports the as-built scheduler §1.8 (`ralph-queue.json`): a JSON array of batch entries
``{tasks, branch, enqueued_at}`` that an unattended launch drains one batch at a time.
This is the credit/headless lane's front door — the human is not in the loop, so the
entrypoint is a file the operator (or a cron `--enqueue`) appends to, and a long-running
``run-queue`` process that ingests each head batch and drives it to terminal through the
existing, already-resumable ``Scheduler.run``.

Two halves live here:

* ``QueueFile`` — a thin, project-agnostic abstraction over the queue JSON. Every
  mutation (``append`` at the tail, the consumer's ``claim_head`` / ``complete_head`` /
  ``unclaim_head``) is a read-modify-write of the whole array, so each is guarded by an
  exclusive advisory lock (``fcntl.flock``, mkdir-spin fallback) and finished with an
  atomic temp-file + ``os.replace``. The lock is what makes concurrent producers safe:
  §1.8's bare append assumed a single enqueuer, but two parallel cron ``--enqueue``
  invocations would otherwise both read the old array and one entry would be lost — the
  lock serializes them instead. Malformed content raises the typed ``QueueError``.
* ``drive_queue`` — the unattended loop, built on a CLAIM-IN-PLACE protocol (#279): the
  head entry is never popped before the work is durably done. The consumer stamps the
  head with ``claim = {run_id, owner, claimed_at}`` in one atomic write — the entry STAYS
  in the queue, so no kill window (SIGKILL, power loss) can ever remove the only durable
  representation of the batch. The claim names the derived run id BEFORE any run state
  exists, so a restarted consumer always knows which run a claimed entry became: a head
  claimed by this ``owner`` is adopted (its recorded ``claim.run_id`` is resumed, not
  re-derived), a head claimed by a DIFFERENT owner raises ``QueueError`` (two-consumer
  exclusion). Ingestion then creates-or-reuses that run and adds each task in listed
  order, ``Scheduler.run`` drives it to terminal, and only THEN is the entry removed
  (``complete_head``, which verifies the claim still matches). On an ingest failure the
  claim is stripped in place (``unclaim_head``) so the same head is retried next launch —
  nothing is silently dropped. Between batches it idle-waits, polling every
  ``poll_interval_s`` up to ``idle_timeout_s`` before exiting.

The engine is never touched directly for model work — ``drive_queue`` only calls the same
public ``create_or_reuse_run`` / ``add_task`` / ``Scheduler.run`` surface a supervisor
would, so it stays project-agnostic and inherits every engine guarantee (idempotent adds,
resumability). Each claimed entry gets a FRESH engine from the caller's ``engine_factory``
rooted at that run's own store (#281), so no two derived runs ever share a status store,
cost ledger, or stage-log tree.
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
from .errors import OrchestratorError
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
    claim = entry.get("claim")
    if claim is not None:
        # An in-place consumer claim (#279): {run_id, owner, claimed_at}, all non-empty
        # strings. Optional — an unclaimed entry simply has no `claim` key.
        if not isinstance(claim, dict):
            raise QueueError(
                f"{where}: `claim` must be a JSON object or absent, "
                f"got {type(claim).__name__}"
            )
        for field in ("run_id", "owner", "claimed_at"):
            value = claim.get(field)
            if not isinstance(value, str) or not value.strip():
                raise QueueError(f"{where}: `claim.{field}` must be a non-empty string")
    return entry


class QueueFile:
    """A ``ralph-queue.json``-style queue of batch entries backed by a JSON array file.

    A missing file reads as an empty queue. Every write is atomic (temp-file in the same
    directory + ``os.replace``) so a crash mid-write can never corrupt the queue. Every
    mutation is a whole-array read-modify-write, so each holds an exclusive advisory lock
    (``.lock`` sentinel next to the queue) for its full read→write span: this is what keeps
    concurrent enqueuers safe. §1.8 originally called ``append`` "lock-free" on a
    single-producer assumption, but two parallel cron ``--enqueue`` runs would each read
    the old array and lose one entry without the lock.

    The consumer side is a CLAIM-IN-PLACE protocol (#279), not pop-then-requeue: an entry
    is only ever removed AFTER its derived run reached terminal. ``claim_head`` stamps the
    head with ``{run_id, owner, claimed_at}`` in one atomic write (the entry stays queued —
    no kill window between dequeue and durable run state can lose the batch),
    ``complete_head`` pops the head only when its claim matches the caller, and
    ``unclaim_head`` strips a matching claim so a failed ingest is retried. All three take
    the same lock so they never race a producer either.
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
            fh = open(lock_file, "a")  # noqa: SIM115 - held across the yield; append avoids truncating the sentinel
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

        Intentionally *not* locked: a peek is only a hint. Any follow-up mutation
        (``claim_head`` / ``complete_head`` / ``unclaim_head``) re-reads and re-verifies
        the head under the exclusive lock, so a stale peek can never act on the wrong
        entry — a racing consumer's claim surfaces there as a typed ``QueueError``."""
        entries = self.read()
        return entries[0] if entries else None

    @staticmethod
    def _check_ids(owner: str, run_id: str, verb: str) -> None:
        """Both halves of a claim identity must be real strings — an empty owner or run id
        would stamp (or match) a claim that no restarted consumer could ever adopt."""
        if not isinstance(owner, str) or not owner.strip():
            raise QueueError(f"cannot {verb}: `owner` must be a non-empty string")
        if not isinstance(run_id, str) or not run_id.strip():
            raise QueueError(f"cannot {verb}: `run_id` must be a non-empty string")

    def claim_head(self, owner: str, run_id: str) -> dict:
        """Stamp the head entry with ``claim = {run_id, owner, claimed_at}`` in ONE atomic
        write and return the claimed entry. The entry STAYS in the queue — claiming is
        in-place, so at no instant is the batch's only durable representation gone (#279).
        Idempotent for the same ``(owner, run_id)`` (a consumer that crashed between claim
        and ingest re-claims its own head); a head already claimed by anyone else raises
        ``QueueError`` and the queue is untouched. An empty queue also raises — the caller
        peeked a head, so a vanished one is a broken single-consumer assumption."""
        self._check_ids(owner, run_id, "claim_head")
        with self._with_lock():
            entries = self.read()
            if not entries:
                raise QueueError("cannot claim_head: queue is empty")
            head = entries[0]
            claim = head.get("claim")
            if claim is not None:
                if claim["owner"] == owner and claim["run_id"] == run_id:
                    return head  # already ours — idempotent re-claim after a crash
                raise QueueError(
                    f"cannot claim_head: head is already claimed by owner "
                    f"{claim['owner']!r} for run {claim['run_id']!r}"
                )
            head = {
                **head,
                "claim": {"run_id": run_id, "owner": owner, "claimed_at": _utc_now_iso()},
            }
            self._write([head, *entries[1:]])
        return head

    def complete_head(self, owner: str, run_id: str) -> dict:
        """Remove and return the head entry — but ONLY if its claim matches
        ``(owner, run_id)``. This is the terminal dequeue of the claim protocol: it runs
        after the derived run reached a terminal state, so removing the entry never drops
        the batch's only durable representation. A missing head, an unclaimed head, or a
        claim held by someone else raises ``QueueError`` (the queue is untouched)."""
        self._check_ids(owner, run_id, "complete_head")
        with self._with_lock():
            head = self._matching_head(owner, run_id, "complete_head")
            self._write(self.read()[1:])
        return head

    def unclaim_head(self, owner: str, run_id: str) -> dict:
        """Strip a matching claim off the head entry IN PLACE and return the (now
        unclaimed) entry — the ingest-failure undo: the entry stays at the head, ready to
        be re-claimed and retried on the next launch. A missing/unclaimed head or a claim
        held by someone else raises ``QueueError`` (the queue is untouched)."""
        self._check_ids(owner, run_id, "unclaim_head")
        with self._with_lock():
            head = self._matching_head(owner, run_id, "unclaim_head")
            head = {k: v for k, v in head.items() if k != "claim"}
            self._write([head, *self.read()[1:]])
        return head

    def _matching_head(self, owner: str, run_id: str, verb: str) -> dict:
        """The head entry, verified (under the caller's lock) to be claimed by exactly
        ``(owner, run_id)``. Raises ``QueueError`` naming what actually holds it."""
        entries = self.read()
        if not entries:
            raise QueueError(f"cannot {verb}: queue is empty")
        claim = entries[0].get("claim")
        if claim is None:
            raise QueueError(f"cannot {verb}: head entry is not claimed")
        if claim["owner"] != owner or claim["run_id"] != run_id:
            raise QueueError(
                f"cannot {verb}: head claim (owner {claim['owner']!r}, run "
                f"{claim['run_id']!r}) does not match (owner {owner!r}, run {run_id!r})"
            )
        return entries[0]


def run_id_for(entry: dict, *, prefix: str = "queue") -> str:
    """Derive a STABLE run id from a batch entry's ``enqueued_at`` timestamp. Stable so a
    driver that crashes and restarts reuses the same run (create-or-reuse) instead of
    forking a duplicate for the same batch. Timestamp punctuation is normalized to ``-``."""
    stamp = re.sub(r"[^0-9a-zA-Z]+", "-", entry.get("enqueued_at", "")).strip("-")
    return f"{prefix}-{stamp}" if stamp else prefix


def _ingest_batch(
    engine: Engine, entry: dict, run_id: str, *, lane: ExecutionLane
) -> tuple[list[str], bool]:
    """Create-or-reuse ``run_id`` and add each of the batch's tasks in listed order,
    returning ``(added_task_ids, run_was_created)``. Fully idempotent so a restarted
    ingest converges: an existing run is reused (``created=False``) and an already-added
    task is skipped (mirrors ``batch_plan.apply_plan``'s per-task add loop, minus the
    DAG — a queue entry is a flat task list with no encoded edges). Reuse goes through
    the EXPLICIT ``create_or_reuse_run`` (#280) so a stable run id can never be
    re-created over live state, and a corrupt run doc surfaces as an error rather than
    being silently replaced.

    "Already added" is ``engine.registered_task_ids`` — a task ref is only skipped once
    its status document is verified to exist and to agree with the ref's identity (#278).
    Skipping on the bare ref would make a crash between ``add_task``'s ref write and its
    doc write permanent: the half-registered task could never be rebuilt."""
    _run, created = engine.create_or_reuse_run(run_id, lane)
    already = engine.registered_task_ids(run_id)
    added: list[str] = []
    for task_id in entry["tasks"]:
        if task_id in already:
            continue
        engine.add_task(run_id, task_id)
        added.append(task_id)
    return added, created


#: Per-run construction seam (#281): given a derived run id, return a FRESH ``Engine``
#: (and its registry-backed ``Runner``) rooted at that run's OWN store — never a shared
#: one. ``drive_queue`` calls it once per claimed entry.
EngineFactory = Callable[[str], tuple[Engine, Runner]]


def drive_queue(
    queue: QueueFile,
    engine_factory: EngineFactory,
    *,
    owner: str = "default",
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

    The unattended (cron) loop, restart-safe at every boundary (#279):

    1. Peek the head batch. If the queue is empty, idle-wait: poll every ``poll_interval_s``
       up to ``idle_timeout_s`` (using ``sleeper``), then exit. Without a ``sleeper`` the
       loop returns immediately when the queue drains (the caller owns re-invocation).
    2. Claim the head IN PLACE: derive the stable run id and ``claim_head(owner, run_id)``
       — one atomic write that records which run this entry becomes while the entry stays
       queued (no kill window can remove the batch's only durable representation). A head
       already claimed by THIS owner is a crash leftover: its recorded ``claim.run_id`` is
       adopted (not re-derived) and resumed. A head claimed by a DIFFERENT owner raises
       ``QueueError`` — two consumers must never process the same entry.
    3. Build a fresh per-run engine+runner via ``engine_factory(run_id)`` (#281) and
       ingest the batch (create-or-reuse the run, add its tasks — idempotent, so a
       restart after partial ingest converges). On any ingest failure the claim is
       stripped (``unclaim_head``) and the failure re-raised — the entry stays at the
       head and is retried next launch, never silently dropped.
    4. Drive the run through ``Scheduler.run`` (inheriting its resumability,
       capacity-wait, circuit breaker); only once it returns is the entry removed via
       ``complete_head`` (which re-verifies the claim), and the per-run final status
       recorded. A death mid-scheduler leaves the claimed entry, so a restart resumes
       the run rather than losing it.

    ``owner`` is a STABLE consumer identity (not a pid): a SIGKILLed consumer relaunched
    with the same owner id reclaims and resumes its own stale claims.

    Returns a structured summary: ``{batches_processed, runs_created, runs[...],
    idle_timed_out}`` where each ``runs`` row is ``{run_id, tasks, added, final_state}``.
    """
    if not isinstance(owner, str) or not owner.strip():
        raise QueueError("drive_queue: `owner` must be a non-empty string")
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
        claim = head.get("claim")
        if claim is None:
            # The claim names the derived run id BEFORE any run state exists, so a
            # restart at any later boundary knows which run this entry became.
            run_id = run_id_for(head, prefix=run_id_prefix)
            head = queue.claim_head(owner, run_id)
        elif claim["owner"] == owner:
            # Crash leftover from a previous launch of THIS consumer: adopt the recorded
            # run id (never re-derive — the claim is the durable name binding) and resume.
            run_id = claim["run_id"]
        else:
            raise QueueError(
                f"queue head is claimed by another consumer (owner {claim['owner']!r}, "
                f"run {claim['run_id']!r}); refusing to process it"
            )

        # #281: a FRESH engine rooted at this run's own store — constructed only after
        # the claim fixed the run id, once per claimed entry.
        engine, runner = engine_factory(run_id)
        try:
            added, created_now = _ingest_batch(engine, head, run_id, lane=lane)
        except Exception as exc:
            # Ingest failed — strip the claim so the entry (still at the head) is retried
            # next launch, then surface the failure (looping on the same bad head would spin).
            queue.unclaim_head(owner, run_id)
            raise QueueError(
                f"failed to ingest batch {head['tasks']} (run {run_id}); "
                f"claim released, entry stays queued: {exc}"
            ) from exc
        if created_now:
            created += 1

        status = Scheduler(engine, max_concurrent=max_concurrent).run(
            run_id, runner, util_pct=util_pct, util_provider=util_provider, sleeper=sleeper,
        )
        # Terminal (or scheduler-returned) — only NOW is the entry dequeued, and only if
        # our claim still holds.
        queue.complete_head(owner, run_id)
        runs.append({
            "run_id": run_id,
            "tasks": head["tasks"],
            "branch": head.get("branch"),
            "added": added,
            "final_state": status.get("run_state"),
        })

    return {
        "batches_processed": len(runs),
        "runs_created": created,
        "runs": runs,
        "idle_timed_out": idle_timed_out,
    }
