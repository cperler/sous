"""JSON-file status store (§7): atomic writes, locked read-modify-write, audit sidecar.

Persistence layout under a run's ``root`` directory:
  - Run doc:   ``status-<run_id>.json``
  - Task doc:  ``status-<run_id>-<task_id>.json``
  - Audit log: ``events.jsonl`` (append-only sidecar)

Writes are atomic: a temp file ``<name>.tmp.<pid>`` is fully written and fsync'd,
then ``os.replace()`` swaps it into place — a reader never sees a partial file.

Locking ports the as-built flock-with-mkdir-fallback contract: prefer
``fcntl.flock(LOCK_EX)`` on a ``<path>.lock`` file; where fcntl is unavailable
(e.g. Windows), spin on ``os.mkdir(<path>.lockdir)`` (atomic dir create).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import StatusStoreError
from .schemas.enums import SCHEMA_VERSION
from .schemas.status import Run, Task

try:  # fcntl is POSIX-only; mkdir fallback covers the rest.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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

    # ---- atomic write ---------------------------------------------------

    def _atomic_write(self, path: Path, text: str) -> None:
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
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

    # ---- versioning -----------------------------------------------------

    @staticmethod
    def _migrate(raw: dict) -> dict:
        """Reader-tolerant migration. A doc without ``schema_version`` is v0."""

        if "schema_version" not in raw:
            raw = {**raw, "schema_version": SCHEMA_VERSION}
        # Future migration steps keyed on schema_version go here.
        return raw

    def _read_json(self, path: Path) -> dict:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise StatusStoreError(f"status file not found: {path}") from exc
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
            fh.flush()
