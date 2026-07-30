"""JSON-file status store (§7): atomic writes, locked read-modify-write, audit sidecar.

Persistence layout under a run's ``root`` directory:
  - Run doc:   ``status-<run_id>.json``
  - Task doc:  ``status-<run_id>-<task_id>.json``
  - Audit log: ``events.jsonl`` (append-only sidecar)

Writes are atomic: a temp file ``<name>.tmp.<pid>.<seq>`` is fully written and fsync'd,
then ``os.replace()`` swaps it into place — a reader never sees a partial file.
The temp suffix is per-writer, not just per-pid (#312), so two same-process writers to
one path can never collide on the temp name.

Locking ports the as-built flock-with-mkdir-fallback contract: prefer
``fcntl.flock(LOCK_EX)`` on a ``<path>.lock`` file; where fcntl is unavailable
(e.g. Windows), spin on ``os.mkdir(<path>.lockdir)`` (atomic dir create).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import RunExistsError, StatusNotFoundError, StatusStoreError
from .schemas.enums import SCHEMA_VERSION
from .schemas.status import Run, Task

try:  # fcntl is POSIX-only; mkdir fallback covers the rest.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

# Per-writer suffix for `_atomic_write` temp files (#312): the pid alone is not unique
# within a process, so two same-process writers to one path would collide.
_TMP_SEQ = itertools.count()

# Ceiling on a persisted dispatch prompt (#314). Deliberately far above any real prompt
# (a fat SCOPE prompt is tens of KB), so it is a backstop against a pathological
# context-plane blowup filling the run dir — not a routine trim. When it does bite, the
# write is NOT silent: `write_stage_prompt` RETURNS the dropped char count and the engine
# call site emits the warning event (the "never silent" split — the writer reports what it
# dropped, only the caller emits).
STAGE_PROMPT_MAX_CHARS = 1_000_000


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock keyed on ``path`` (flock, with mkdir-spin fallback).

    Module-level so writers OUTSIDE the status store can share the exact same
    locking contract on their own files — the cost ledger's scan-then-append
    idempotency (#277) needs the same mutual exclusion the status docs get.
    """
    path = Path(path)
    if _HAVE_FCNTL:
        lock_file = path.with_name(f"{path.name}.lock")
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
        lock_dir = path.with_name(f"{path.name}.lockdir")
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


def safe_task_dirname(task_id: str) -> str:
    """The filesystem-safe directory component for a task's per-stage log tree
    (``stages/<safe>/``). Extracted so the execution adapters that tee raw provider
    streams write into the SAME per-stage dir this store writes stage records into —
    one sanitization, one location."""
    return task_id.replace("#", "").replace("/", "_") or "task"


class StatusStore:
    """File-backed persistence for Run/Task documents plus an audit sidecar."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- path helpers ---------------------------------------------------

    def _run_path(self, run_id: str) -> Path:
        return self.root / f"status-{run_id}.json"

    def _task_path(self, run_id: str, task_id: str) -> Path:
        return self.root / f"status-{run_id}-{task_id}.json"

    @property
    def _events_path(self) -> Path:
        return self.root / "events.jsonl"

    def _stages_dir(self, task_id: str) -> Path:
        return self.root / "stages" / safe_task_dirname(task_id)

    # ---- atomic write ---------------------------------------------------

    def _atomic_write(self, path: Path, text: str) -> None:
        """Write ``text`` to ``path`` atomically (write-temp + rename).

        The temp name is unique per WRITER, not per process (#312): two writers in the
        same process targeting one path would otherwise compute the same
        ``<name>.tmp.<pid>`` and race on the rename, losing one document with no error.
        Callers do serialize same-path writes under that path's lock, so the collision is
        unreachable today — but that safety is a property of every call site's locking
        discipline rather than of this function, and #278's pre-fix run reached it for
        real once doc writes were briefly unserialized. The counter makes the write safe
        on its own terms; ``itertools.count`` is atomic in CPython, so no extra lock.
        """
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{next(_TMP_SEQ)}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # atomic rename
        except OSError as exc:  # pragma: no cover - defensive
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise StatusStoreError(f"atomic write failed for {path}: {exc}") from exc

    # ---- locking --------------------------------------------------------

    @contextmanager
    def with_lock(self, path: Path) -> Iterator[None]:
        """Exclusive lock keyed on ``path`` (flock, with mkdir-spin fallback)."""
        with file_lock(path):
            yield

    @contextmanager
    def with_task_lock(self, run_id: str, task_id: str) -> Iterator[None]:
        """Hold a task doc's write lock across a MULTI-STEP caller transaction (#278).

        The store's own writers each take this lock for the duration of one
        read-modify-write; a caller that must keep two writes (a run-doc mutation and the
        task doc it pairs with) exclusive against a concurrent caller needs the lock to
        span both, and takes it here. The locks are not re-entrant (a second ``flock`` on
        the same path from the same process blocks forever), so inside this block write the
        task doc with :meth:`write_task_locked` — never ``save_task``/``update_task``.

        Lock order: this task lock is the OUTER lock; the run lock may be taken inside it
        (``update_run``). No path ever takes them the other way round, so there is no cycle
        to deadlock on."""
        with self.with_lock(self._task_path(run_id, task_id)):
            yield

    def write_task_locked(self, task: Task) -> None:
        """Atomically write ``task``'s document while the CALLER holds its lock (i.e. from
        inside :meth:`with_task_lock`). ``save_task`` is the locking equivalent for callers
        that hold nothing."""
        self._write_task(task)

    def sweep_locks(self) -> int:
        """Delete the ``.lock``/``.lockdir`` sentinels under the run dir; return the count.

        These are ``flock``/mkdir sentinels needed only while writers are active. They
        can't be unlinked on release (delete-then-recreate races two writers onto
        different inodes → no mutual exclusion), so they linger. Sweeping is safe ONLY
        when the run is terminal — no writers remain — which is the only place the engine
        calls this."""
        removed = 0
        for lock in self.root.rglob("*.lock"):
            with contextlib.suppress(OSError):
                lock.unlink()
                removed += 1
        for lockdir in self.root.rglob("*.lockdir"):
            with contextlib.suppress(OSError):
                lockdir.rmdir()
                removed += 1
        return removed

    # ---- versioning -----------------------------------------------------

    @staticmethod
    def _migrate(raw: dict) -> dict:
        """Reader-tolerant migration. A doc without ``schema_version`` is v0."""

        if "schema_version" not in raw:
            raw = {**raw, "schema_version": SCHEMA_VERSION}
        # v1 -> v2: task docs gained `pipeline`. The derivation itself lives on the Task
        # model's before-validator (lane preset), so here we only stamp the version —
        # a doc read through this migration validates as v2-shaped.
        if raw.get("schema_version") == "1":
            raw = {**raw, "schema_version": SCHEMA_VERSION}
        return raw

    def _read_json(self, path: Path) -> dict:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise StatusNotFoundError(f"status file not found: {path}") from exc
        except OSError as exc:  # pragma: no cover - defensive
            raise StatusStoreError(f"failed to read {path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StatusStoreError(f"corrupt status file {path}: {exc}") from exc
        return self._migrate(raw)

    # ---- run API --------------------------------------------------------

    def _write_run(self, run: Run) -> None:
        """Unlocked atomic write — callers hold the run lock."""
        self._atomic_write(self._run_path(run.run_id), run.model_dump_json(indent=2))

    def save_run(self, run: Run) -> None:
        # Public writers take the lock so a direct save can't clobber a concurrent
        # locked read-modify-write (the unlocked path was a lost-update race).
        with self.with_lock(self._run_path(run.run_id)):
            self._write_run(run)

    def create_run_doc(self, run: Run) -> None:
        """Write a run doc that must not already exist, raising ``RunExistsError`` if it
        does (#280). The exists-check and the write happen under the SAME run lock that
        every other run writer takes, so two concurrent creators cannot both see "absent"
        and race a second one over the first's document."""
        path = self._run_path(run.run_id)
        with self.with_lock(path):
            if path.exists():
                raise RunExistsError(
                    f"run {run.run_id} already exists at {path}; "
                    "refusing to overwrite it (choose a new run id)"
                )
            self._write_run(run)

    def run_exists(self, run_id: str) -> bool:
        """True iff ``run_id`` has a loadable run doc. Only a genuine not-found returns
        False; an unreadable or corrupt-JSON run doc raises (``StatusNotFoundError`` is
        the narrow not-found signal, so I/O and parse errors are NOT swallowed as "run
        absent" — #112). Callers use this to *report* on a run's presence; they must not
        use it to guard a create (that check-then-write is racy — ``create_run_doc``
        does the check under the write lock)."""
        try:
            self.load_run(run_id)
            return True
        except StatusNotFoundError:
            return False

    def load_run(self, run_id: str) -> Run:
        return Run.model_validate(self._read_json(self._run_path(run_id)))

    def update_run(self, run_id: str, mutator: Callable[[Run], None]) -> Run:
        path = self._run_path(run_id)
        with self.with_lock(path):
            run = self.load_run(run_id)
            mutator(run)
            run.updated_at = _utc_now_iso()
            self._write_run(run)  # already under the lock
            return run

    # ---- task API -------------------------------------------------------

    def _write_task(self, task: Task) -> None:
        """Unlocked atomic write — callers hold the task lock."""
        self._atomic_write(
            self._task_path(task.run_id, task.task_id), task.model_dump_json(indent=2)
        )

    def save_task(self, task: Task) -> None:
        with self.with_lock(self._task_path(task.run_id, task.task_id)):
            self._write_task(task)

    def load_task(self, run_id: str, task_id: str) -> Task:
        return Task.model_validate(self._read_json(self._task_path(run_id, task_id)))

    def update_task(
        self, run_id: str, task_id: str, mutator: Callable[[Task], None]
    ) -> Task:
        path = self._task_path(run_id, task_id)
        with self.with_lock(path):
            task = self.load_task(run_id, task_id)
            mutator(task)
            task.updated_at = _utc_now_iso()
            self._write_task(task)  # already under the lock
            return task

    def commit_task_events(
        self,
        run_id: str,
        task_id: str,
        mutator: Callable[[Task], None],
        events: list[dict] | Callable[[Task], list[dict]] | None = None,
    ) -> Task:
        """Transactional task mutation + bookkeeping events under one task lock.

        Read-modify-write the task like :meth:`update_task`, but append the
        associated audit events (built after the mutation, so they can reflect the
        mutated state) and commit the task doc together. The ordering is the
        invariant: the events are appended to ``events.jsonl`` FIRST (each append
        atomic), then the task doc is written LAST as the single durable commit
        point. Because the task-doc write is ordered last, a durably persisted task
        mutation implies its events are already on disk — closing the orphan window
        where a crash between the task write and a separate ``append_event`` left a
        task claiming a dispatch with no matching ``stage_dispatched`` event.

        ``events`` may be a static list or a callable receiving the mutated task
        (used when an event field is only known after the read-modify-write, e.g.
        a superseded lease id captured inside the mutator).
        """
        path = self._task_path(run_id, task_id)
        with self.with_lock(path):
            task = self.load_task(run_id, task_id)
            mutator(task)
            task.updated_at = _utc_now_iso()
            evs = events(task) if callable(events) else (events or [])
            for ev in evs:
                self.append_event(run_id, ev)  # atomic append, events-file lock
            self._write_task(task)  # durable commit point — LAST, already under lock
            return task

    # ---- audit sidecar --------------------------------------------------

    def append_event(self, run_id: str, event: dict) -> None:
        """Append one JSON line to events.jsonl (audit sidecar).

        The line is well under PIPE_BUF, so a single ``write`` in append mode is
        atomic on POSIX; the lock additionally guards against interleaving.
        """

        line = json.dumps(event, separators=(",", ":")) + "\n"
        path = self._events_path
        with self.with_lock(path), open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def read_events(self, run_id: str | None = None) -> list[dict]:
        """Read the events.jsonl timeline (optionally filtered to one run)."""
        path = self._events_path
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if run_id is None or ev.get("run_id") == run_id:
                out.append(ev)
        return out

    def read_stage_logs(self, task_id: str) -> list[dict]:
        """Read a task's durable per-stage JSON records in sequence order."""
        d = self._stages_dir(task_id)
        if not d.exists():
            return []

        def _seq(path: Path) -> tuple[int, str]:
            # Sort by the numeric NN- prefix, not lexically (NN is only 2-padded, so
            # "100-" would sort before "99-" under a plain string sort).
            head = path.name.split("-", 1)[0]
            return (int(head), path.name) if head.isdigit() else (1 << 30, path.name)

        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(d.glob("*.json"), key=_seq)
        ]

    def write_stage_log(
        self, task_id: str, seq: int, stage: str, payload: dict
    ) -> Path:
        """Persist one stage's durable record to stages/<task>/NN-<stage>.json.

        This is the per-stage log tree (the interactive-lane analog of the bash
        system's stages/NN-stage.* files); it captures the StageResult including
        structured_output and raw_output. Returns the path written.
        """

        d = self._stages_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{seq:02d}-{stage}.json"
        self._atomic_write(path, json.dumps(payload, indent=2, default=str))
        return path

    def write_stage_prompt(
        self, task_id: str, filename: str, text: str
    ) -> tuple[Path, int | None]:
        """Persist one dispatch's rendered prompt into the task's stage dir (#314).

        ``filename`` comes from ``stream_probe.prompt_filename`` — that module owns the
        stage-file naming and imports THIS one for ``safe_task_dirname``, so the dependency
        runs one way and the engine composes the two rather than the store importing back.

        Returns ``(path, dropped_chars)``. ``dropped_chars`` is ``None`` for the normal
        whole-prompt write and the number of characters cut when
        :data:`STAGE_PROMPT_MAX_CHARS` bites; the caller emits the notice (this writer never
        logs a truncation itself). A truncated file also says so in-band, so a human reading
        the ``.txt`` alone is never misled into treating it as the complete prompt.
        """
        d = self._stages_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        dropped: int | None = None
        if len(text) > STAGE_PROMPT_MAX_CHARS:
            dropped = len(text) - STAGE_PROMPT_MAX_CHARS
            text = (
                text[:STAGE_PROMPT_MAX_CHARS]
                + f"\n\n[truncated by orchestrator: {dropped} of {dropped + STAGE_PROMPT_MAX_CHARS}"
                " chars dropped; see the stage_prompt_truncated event]\n"
            )
        self._atomic_write(path, text)
        return path, dropped

    def write_stage_markdown(self, task_id: str, seq: int, stage: str, text: str) -> Path:
        """Human-readable per-stage Markdown alongside the JSON record."""
        d = self._stages_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{seq:02d}-{stage}.md"
        self._atomic_write(path, text)
        return path

    def write_task_index(self, task_id: str, text: str) -> Path:
        d = self._stages_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "index.md"
        self._atomic_write(path, text)
        return path

    def write_approval(self, run_id: str, task_id: str, payload: dict) -> Path:
        """Durable human-approval artifact (design pass §4): who approved what, when."""
        safe = safe_task_dirname(task_id)
        path = self.root / f"approval-{run_id}-{safe}.json"
        self._atomic_write(path, json.dumps(payload, indent=2, default=str))
        return path

    def load_approval(self, run_id: str, task_id: str) -> dict | None:
        safe = safe_task_dirname(task_id)
        path = self.root / f"approval-{run_id}-{safe}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_rejection(self, run_id: str, task_id: str, payload: dict) -> Path:
        """Durable human-rejection artifact (issue #49): who rejected what, when, why —
        the terminal confirm-and-close counterpart to write_approval."""
        safe = safe_task_dirname(task_id)
        path = self.root / f"rejection-{run_id}-{safe}.json"
        self._atomic_write(path, json.dumps(payload, indent=2, default=str))
        return path

    def load_rejection(self, run_id: str, task_id: str) -> dict | None:
        safe = safe_task_dirname(task_id)
        path = self.root / f"rejection-{run_id}-{safe}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_run_artifact(self, name: str, text: str) -> Path:
        """Write a run-level text artifact (e.g. cost-summary.md) under the root."""
        path = self.root / name
        self._atomic_write(path, text)
        return path
