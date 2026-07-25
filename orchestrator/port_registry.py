"""Per-task port-block allocator for parallel worktrees (#5).

Tasks running in parallel git worktrees (the headless/codex batch lane — see
``adapters/execution/deterministic_setup``) collide on fixed resources: a project's
dev-server / test-server binds ``:3000`` (or Vite's ``:5173``), and two tasks that boot
it at once fight over the socket. The reference bash system solved this with a small JSON
"port registry" (``.claude/scripts/lib/port-registry.sh``) that handed each worktree its
own port out of a fixed range, locked with a lock dir and pruned by pid-liveness.

This is the rebuild of that idea, generalized:

  * A contiguous port BLOCK (not a single port) is allocated per ``(run_id, task_id)`` from
    a configured range, so a project can map several servers (dev, test, api) onto one
    task's slice without further coordination.
  * Persistence is a single JSON file guarded by an OS advisory lock (``fcntl.flock``) held
    across each read-modify-write, so concurrent same-host schedulers never double-allocate.
  * Allocation is idempotent per ``(run, task)`` (a retried intake re-uses its block).
  * Each candidate block is BIND-PROBED (a real TCP bind on every port) before it is handed
    out; an occupied block is skipped and the scan advances — the registry never hands out a
    port something else is already listening on.
  * Records carry ``pid`` + ``ts``; ``reclaim_stale`` frees the block of any allocation whose
    task is terminal (via a caller-supplied predicate), whose owning process is gone, or that
    has aged past a TTL — so a crashed run's ports come back.

SAME-HOST SCOPE ONLY. There is no distributed coordination: the lock and the bind-probe are
host-local, so this is correct for the (intended) case of several scheduler processes on ONE
machine sharing worktrees, and makes no promise across machines that share a filesystem.

The engine stays project-agnostic and never allocates unless a project OPTS IN — see
``project_needs_ports`` (a duck-typed ``port_env`` hook or an explicit ``needs_ports``
attribute). Absent both, every path here is a clean no-op and no registry file is touched.
"""

from __future__ import annotations

import contextlib
import os
import socket
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:  # POSIX advisory file locking (darwin/linux). Absent on exotic hosts -> degrade.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX; single-process correctness only
    fcntl = None  # type: ignore[assignment]

# Defaults. The old system used a single port per worktree from 5173-5272; there was no
# block concept, so the block size is a NEW knob (issue #5) defaulted to 10 — a comfortable
# slice (dev + test + api + headroom) — over a fresh, collision-free range well clear of the
# reference Vite range and the common 3000/8000/8080 dev ports.
DEFAULT_PORT_RANGE: tuple[int, int] = (42000, 42999)
DEFAULT_BLOCK_SIZE = 10
DEFAULT_TTL_S = 6 * 3600  # a block older than this with no live owner is reclaimable

# Convenience env var names the engine injects into every stage subprocess (a project's
# ``port_env`` hook may add/override with its own names — e.g. heysoo's REACT_PORT).
ENV_PORT_BASE = "ORCHESTRATOR_PORT_BASE"
ENV_PORT_COUNT = "ORCHESTRATOR_PORT_COUNT"
ENV_PORT = "PORT"  # the single most-common convenience var == the block base


@dataclass(frozen=True)
class Allocation:
    base: int
    count: int
    run_id: str
    task_id: str
    pid: int | None
    ts: str

    def to_dict(self) -> dict:
        return {
            "base": self.base, "count": self.count, "run_id": self.run_id,
            "task_id": self.task_id, "pid": self.pid, "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Allocation | None:
        try:
            return cls(
                base=int(d["base"]), count=int(d["count"]),
                run_id=str(d["run_id"]), task_id=str(d["task_id"]),
                pid=(int(d["pid"]) if d.get("pid") is not None else None),
                ts=str(d.get("ts") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None  # a corrupt row is dropped, never crashes an allocate/reclaim


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_registry_path() -> Path:
    """The host-wide registry file. Overridable via ``ORCHESTRATOR_PORT_REGISTRY`` so a
    test (or a deployment that wants it under the repo) can redirect it; otherwise a stable
    per-host temp location shared by every scheduler on the box (that shared location is the
    whole point — ports are a host resource, not a per-run one)."""
    env = os.environ.get("ORCHESTRATOR_PORT_REGISTRY")
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "orchestrator" / "port-registry.json"


def _pid_alive(pid: int | None) -> bool:
    """Is ``pid`` a live process on this host? Unknown/None -> treated as alive (never
    reclaim a block we can't prove is dead on pid alone; TTL still catches true orphans)."""
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else -> alive
        return True
    except OSError:
        return True
    return True


class PortRegistry:
    """Same-host contiguous-port-block allocator backed by a locked JSON file."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
        block_size: int = DEFAULT_BLOCK_SIZE,
        ttl_s: float = DEFAULT_TTL_S,
        bind_probe: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else default_registry_path()
        lo, hi = int(port_range[0]), int(port_range[1])
        if lo > hi:
            lo, hi = hi, lo
        self.lo, self.hi = lo, hi
        self.block_size = max(1, int(block_size))
        self.ttl_s = float(ttl_s)
        self.bind_probe = bind_probe

    # --- public API -----------------------------------------------------------
    def allocate(
        self, run_id: str, task_id: str, *, pid: int | None = None, now: str | None = None
    ) -> int | None:
        """Reserve a contiguous block for ``(run_id, task_id)`` and return its base port,
        or ``None`` when the range is exhausted. Idempotent: an existing allocation for the
        same pair is returned unchanged (retry-safe intake). Skips any block whose ports are
        already recorded OR that fails the bind-probe (something is listening), advancing to
        the next aligned block. The whole read-modify-write is under the file lock."""
        pid = pid if pid is not None else os.getpid()
        now = now or _now()
        with self._locked():
            records = self._prune(self._read(), now=now, is_terminal=None)
            for rec in records:
                if rec.run_id == run_id and rec.task_id == task_id:
                    self._write(records)  # persist any pruning we did above
                    return rec.base
            occupied = {p for rec in records for p in range(rec.base, rec.base + rec.count)}
            for base in range(self.lo, self.hi - self.block_size + 2, self.block_size):
                block = range(base, base + self.block_size)
                if block.stop - 1 > self.hi:
                    break
                if any(p in occupied for p in block):
                    continue
                if self.bind_probe and not self._block_free(block):
                    continue
                records.append(Allocation(base, self.block_size, run_id, task_id, pid, now))
                self._write(records)
                return base
            self._write(records)  # persist pruning even when we couldn't allocate
            return None

    def release(self, run_id: str, task_id: str) -> bool:
        """Free the block held by ``(run_id, task_id)``. Returns whether anything was
        removed. A no-op (and no file write) when the registry file doesn't exist."""
        if not self.path.exists():
            return False
        with self._locked():
            records = self._read()
            kept = [r for r in records if not (r.run_id == run_id and r.task_id == task_id)]
            if len(kept) != len(records):
                self._write(kept)
                return True
            return False

    def reclaim_stale(
        self,
        is_terminal: Callable[[str, str], bool] | None = None,
        *,
        now: str | None = None,
    ) -> list[Allocation]:
        """Free every stale block and return what was freed. Stale = the owning task is
        terminal (``is_terminal(run_id, task_id) -> bool``, when supplied), OR the owning
        pid is gone, OR the record has aged past the TTL. A no-op when the file is absent."""
        if not self.path.exists():
            return []
        now = now or _now()
        with self._locked():
            records = self._read()
            live: list[Allocation] = []
            dropped: list[Allocation] = []
            for rec in records:
                (dropped if self._is_stale(rec, now, is_terminal) else live).append(rec)
            if dropped:
                self._write(live)
            return dropped

    def allocation_for(self, run_id: str, task_id: str) -> Allocation | None:
        """The current allocation for a pair (no lock needed — a single atomic read)."""
        for rec in self._read():
            if rec.run_id == run_id and rec.task_id == task_id:
                return rec
        return None

    # --- staleness ------------------------------------------------------------
    def _prune(
        self,
        records: list[Allocation],
        *,
        now: str,
        is_terminal: Callable[[str, str], bool] | None,
    ) -> list[Allocation]:
        return [r for r in records if not self._is_stale(r, now, is_terminal)]

    def _is_stale(
        self, rec: Allocation, now: str, is_terminal: Callable[[str, str], bool] | None
    ) -> bool:
        if is_terminal is not None:
            try:
                if is_terminal(rec.run_id, rec.task_id):
                    return True
            except Exception:  # noqa: BLE001 - a flaky predicate must not wedge reclaim
                pass
        if not _pid_alive(rec.pid):
            return True
        return self._aged(rec.ts, now)

    def _aged(self, ts: str, now: str) -> bool:
        try:
            age = (datetime.fromisoformat(now) - datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError):
            return False  # unparsable stamp -> never age it out (pid check still applies)
        return age > self.ttl_s

    # --- bind probe -----------------------------------------------------------
    def _block_free(self, block: range) -> bool:
        """True iff every port in the block can be bound right now (nothing listening)."""
        return all(self._port_free(p) for p in block)

    @staticmethod
    def _port_free(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # No SO_REUSEADDR: we want the bind to FAIL if anything already holds the port,
            # which is exactly the collision we're avoiding.
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        finally:
            s.close()
        return True

    # --- persistence ----------------------------------------------------------
    def _read(self) -> list[Allocation]:
        import json
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        try:
            data = json.loads(raw) if raw.strip() else []
        except json.JSONDecodeError:
            return []  # a corrupt registry starts clean rather than wedging every run
        out: list[Allocation] = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict) and (a := Allocation.from_dict(row)) is not None:
                out.append(a)
        return out

    def _write(self, records: list[Allocation]) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([r.to_dict() for r in records], indent=2)
        # Atomic replace so a concurrent reader (allocation_for) never sees a half-written
        # file; the lock still serializes writers.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".port-registry.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive advisory lock on ``<path>.lock`` across a read-modify-write.
        Degrades to a no-op lock where ``fcntl`` is unavailable (still correct single-process)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# --- opt-in + env injection helpers ------------------------------------------------------

def project_needs_ports(project: object) -> bool:
    """Whether this project opts into port allocation. Opt-in is a duck-typed ``port_env``
    hook (the project translates a block into its own server env vars) OR an explicit truthy
    ``needs_ports`` attribute. Absent both, the whole feature is a no-op for that project."""
    if callable(getattr(project, "port_env", None)):
        return True
    return bool(getattr(project, "needs_ports", False))


def port_env_for(project: object, base: int, count: int) -> dict[str, str]:
    """The env vars to export into a task's stage subprocess for its port block. Always the
    generic trio (``ORCHESTRATOR_PORT_BASE``/``ORCHESTRATOR_PORT_COUNT``/``PORT``); a project's
    optional ``port_env(base, count) -> dict[str, str]`` hook adds/overrides with its own names
    (e.g. heysoo's ``REACT_PORT``/``HEYSOO_REACT_URL``). A raising/malformed hook is ignored —
    injection must never break a dispatch."""
    env = {ENV_PORT_BASE: str(base), ENV_PORT_COUNT: str(count), ENV_PORT: str(base)}
    hook = getattr(project, "port_env", None)
    if callable(hook):
        try:
            extra = hook(base, count)
        except Exception:  # noqa: BLE001 - a project hook must never break injection
            extra = None
        if isinstance(extra, dict):
            for k, v in extra.items():
                env[str(k)] = str(v)
    return env


def registry_for_project(project: object) -> PortRegistry:
    """Build the registry a project uses, honoring optional duck-typed overrides
    (``port_registry_path`` / ``port_range`` / ``port_block_size``) so every component
    — the setup runner's allocate, the engine's release, the scheduler's reclaim —
    resolves the SAME file and range for a given project."""
    path = getattr(project, "port_registry_path", None)
    rng = getattr(project, "port_range", None) or DEFAULT_PORT_RANGE
    block = getattr(project, "port_block_size", None) or DEFAULT_BLOCK_SIZE
    return PortRegistry(
        Path(path) if path else None,
        port_range=(int(rng[0]), int(rng[1])),
        block_size=int(block),
    )
