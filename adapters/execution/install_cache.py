"""Intake install caching — lockfile-hash skip (#63).

Ports ``run_setup_stage``'s install cache from the reference bash system (OC:397-493):
when the project's lockfiles are byte-identical to the last *successful* install into
THIS worktree, skip the (slow) dependency install and record the skip honestly.

CORRECTNESS — the cache is PER-WORKTREE, never global (this is the whole subtlety of
#63). The reference projects install INTO the worktree: heysoo ``npm install && uv
sync`` writes ``node_modules`` + ``.venv`` into the CWD; selfhost ``uv sync`` writes
``.venv``. Every task gets a FRESH worktree, so a naive ``(repo, lockfile)->hash``
cache would match on a brand-new worktree and skip the install, leaving it with no
dependencies — a broken worktree. So the cache marker lives in the worktree's OWN
per-worktree git dir (``<repo>/.git/worktrees/<name>/orchestrator-install-cache.json``,
discovered via ``git rev-parse --absolute-git-dir`` run inside the worktree). That ties
the cache's lifetime to the worktree's:

  - a fresh worktree has no marker -> full install (correct);
  - re-entering the SAME live worktree (retry idempotency, a review fix-cycle re-running
    intake, a resume) finds the marker and — because the deps are demonstrably still
    present, we put them there — safely skips;
  - ``git worktree remove`` (post-run cleanup) deletes the per-worktree git dir, so a
    later re-run that recreates the worktree at the same path gets a fresh (empty) git
    dir and full-installs. No stale skip survives the worktree.

The marker is never committed (git never tracks its private per-worktree dir) and never
shows up in ``git status``. Any doubt at all — cache disabled, no lockfiles found, hash
mismatch, no git dir, an unreadable marker, or a previous install that FAILED — falls
through to a full install.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

# Generic, project-agnostic lockfile names across common ecosystems. A project may
# EXTEND this via an optional duck-typed ``ProjectConfig.lockfiles`` (a ``list[str]`` or
# a zero-arg callable returning one) — see adapters/project/base.py.
DEFAULT_LOCKFILES: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
)

MARKER_NAME = "orchestrator-install-cache.json"
# Escape hatch: set this to any non-empty value to force a full install every intake
# (disables the skip entirely) without touching call sites.
ENV_DISABLE = "ORCHESTRATOR_NO_INSTALL_CACHE"


def cache_disabled() -> bool:
    return bool(os.environ.get(ENV_DISABLE))


def project_lockfiles(project: object) -> list[str]:
    """Default lockfile names plus the project's optional override, de-duplicated."""
    names = list(DEFAULT_LOCKFILES)
    extra = getattr(project, "lockfiles", None)
    if callable(extra):
        try:
            extra = extra()
        except Exception:  # noqa: BLE001 - a project surface must never break intake
            extra = None
    if extra:
        for n in extra:
            if isinstance(n, str) and n and n not in names:
                names.append(n)
    return names


def discover(worktree: Path, names: list[str]) -> list[Path]:
    """Lockfiles from ``names`` that actually exist in ``worktree`` (sorted, stable)."""
    return sorted((worktree / n for n in names if (worktree / n).is_file()), key=lambda p: p.name)


def compute_hash(paths: list[Path]) -> str | None:
    """A sha256 over the (name, bytes) of each present lockfile. ``None`` when there are
    no lockfiles (=> no basis to cache on => always full install) or one is unreadable."""
    if not paths:
        return None
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: x.name):
        try:
            data = p.read_bytes()
        except OSError:
            return None
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def load_marker(marker: Path) -> dict | None:
    """The persisted cache entry, or ``None`` if absent/unreadable/corrupt (=> reinstall)."""
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_marker(marker: Path, *, digest: str, lockfiles: list[str], success: bool) -> None:
    """Best-effort persist of the install outcome. Never raises into intake."""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "hash": digest,
                    "lockfiles": lockfiles,
                    "success": success,
                    "at": datetime.now(UTC).isoformat(),
                }
            )
        )
    except OSError:
        pass


def should_skip(marker: Path | None, digest: str | None) -> bool:
    """Skip ONLY when everything lines up: caching enabled, a hashable lockfile set, a
    known marker path, and a prior SUCCESSFUL install of the very same hash recorded there.
    Every other case (disabled, no lockfiles, no marker, unreadable, mismatch, prior
    failure) returns False -> full install."""
    if cache_disabled() or digest is None or marker is None:
        return False
    entry = load_marker(marker)
    return bool(entry and entry.get("success") and entry.get("hash") == digest)
