"""Run-scoped INTAKE baseline sharing (#553).

Every task's INTAKE runs the project's full unit suite to capture its baseline, even when
several tasks in one run start from identical code. On a slow suite that is many minutes per
task, and a first wave of N tasks runs it N times at once on the same machine. This module
lets tasks that start from the same code share ONE baseline run.

KEY — what the baseline was measured against, and nothing else:

  - the git tree hash of the worktree's HEAD, taken AFTER #216 dep-branch composition (so a
    dependent that composed a sibling's branch never reuses a pre-dep result);
  - the ``test_unit_cmd`` argv;
  - the lockfile hash (the same inputs #63's install cache hashes).

A different key (trunk moved, deps composed, lockfile or command changed) runs its own
baseline, so a stale result is never reused. No key at all — the worktree is not a clean git
checkout (a #385 inherited dirty tree, an install that rewrote a tracked file, untracked
files the suite might read), git is unreadable, or ``ORCHESTRATOR_NO_BASELINE_CACHE`` is set
— means no sharing: the tree hash would not describe what the suite actually runs against.

PERSISTENCE — the cache lives in the run's own dir (``<run dir>/baseline-cache/``), never in
engine memory, because every CLI subcommand rebuilds the Engine from defaults. One lock file
per key (``fcntl.flock``): the first task to need a key runs the suite while holding it;
same-key tasks block on the lock (no polling) and then read the stored result. Different keys
use different locks, so unrelated baselines still run in parallel. Only a CAPTURED result is
stored — a timeout or run error is never shared, so the next sharer retries honestly.

Anything going wrong here (an unwritable dir, a corrupt entry) falls through to running the
suite: the cache can make INTAKE faster, never fail it, and never fabricates a baseline.

Two consequences, stated deliberately:

  - The per-task port block (#5) is exported for the ONE run that populates a key. The stored
    result is a pass/fail list; since only one sharer per key runs the suite, port allocation
    cannot make two sharers' outcomes diverge. A suite whose pass/fail genuinely depends on
    which port block it got would already be non-deterministic per task.
  - A flaky test now lands in every sharer's baseline at once rather than independently. That
    is accepted; set ``ORCHESTRATOR_NO_BASELINE_CACHE`` to capture per task again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from . import install_cache
from .transport import _git

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

CACHE_DIRNAME = "baseline-cache"
# Escape hatch: any non-empty value disables sharing (every task captures its own baseline).
ENV_DISABLE = "ORCHESTRATOR_NO_BASELINE_CACHE"


def cache_disabled() -> bool:
    return bool(os.environ.get(ENV_DISABLE))


def compute_key(worktree: Path, argv: list[str], project: object) -> tuple[str, str] | None:
    """``(key, tree)`` for a baseline measured in ``worktree`` with ``argv``, or None when
    the result must not be shared (disabled, not a git checkout, unreadable HEAD tree, or ANY
    tracked or untracked change — ignored files such as an installed ``.venv`` don't count)."""
    if cache_disabled() or not worktree.is_dir():
        return None
    tree = _git(str(worktree), "rev-parse", "HEAD^{tree}")
    if tree.returncode != 0 or not tree.stdout.strip():
        return None
    status = _git(str(worktree), "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0 or status.stdout.strip():
        return None
    tree_hash = tree.stdout.strip()
    lock_hash = install_cache.compute_hash(
        install_cache.discover(worktree, install_cache.project_lockfiles(project))
    )
    h = hashlib.sha256()
    for part in (tree_hash, json.dumps(list(argv)), lock_hash or ""):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest(), tree_hash


def cached_or_run(
    cache_dir: str | Path,
    key: str,
    *,
    tree: str,
    task_id: str,
    run_fn: Callable[[], dict],
) -> tuple[dict, dict | None]:
    """The baseline for ``key``: ``(result, provenance)``. ``provenance`` is None when THIS
    call ran the suite, else ``{"source_task", "tree"}`` naming the task whose run it reuses.

    Holds the key's lock across the read-run-write, so concurrent same-key callers run the
    suite exactly once. Any cache I/O failure degrades to calling ``run_fn`` directly."""
    root = Path(cache_dir) / CACHE_DIRNAME
    entry_path = root / f"{key}.json"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _locked(root / f"{key}.lock"):
            entry = _load(entry_path)
            if entry is not None:
                return entry["result"], {
                    "source_task": entry["source_task"], "tree": entry["tree"],
                }
            result = run_fn()
            if result.get("captured"):
                _save(entry_path, {
                    "key": key, "tree": tree, "source_task": task_id,
                    "at": datetime.now(UTC).isoformat(), "result": result,
                })
            return result, None
    except OSError:
        # Lock/dir unusable: never fail intake over the cache. (A failure inside run_fn is
        # not an OSError — _capture_baseline converts its own errors to an honest miss.)
        return run_fn(), None


def _load(path: Path) -> dict | None:
    """A stored entry, or None if absent/unreadable/corrupt (=> run the suite)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if (
        not isinstance(result, dict)
        or result.get("captured") is not True
        or not isinstance(result.get("failures"), list)
        or not isinstance(result.get("note"), str)
        or not isinstance(data.get("source_task"), str)
        or not isinstance(data.get("tree"), str)
    ):
        return None
    return data


def _save(path: Path, entry: dict) -> None:
    """Atomic best-effort persist (temp file + ``os.replace``). Never raises into intake."""
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".baseline.", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entry))
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``lock_path`` (blocking, no polling). Degrades to a no-op
    where ``fcntl`` is unavailable — still correct within one process's serial dispatch."""
    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        yield
        return
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
