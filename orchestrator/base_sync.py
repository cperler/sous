"""Bring a task branch up to date with its base before DELIVER (#585).

A batch against a busy repo fails at DELIVER when trunk moved after the task branch was
cut: the branch no longer contains the base, and before this module nothing in the
pipeline merged it in. :func:`sync_base` is the deterministic git half of the fix. It
fetches, checks whether the base is already in the branch, and if not merges it into the
task worktree. It reports what happened and never raises. The engine decides what each
outcome means for the task (re-run TEST, re-open IMPLEMENT, or dispatch DELIVER) and
emits the event.

Merge, not rebase: a rebase rewrites commits that DELIVER may already have pushed (a
review fix cycle's PR), while a merge only adds one commit on top.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .gitcmd import run_git

SYNCED = "synced"
ALREADY_CURRENT = "already_current"
CONFLICT = "conflict"
SKIPPED = "skipped"

# Enough conflicting paths to act on; a merge that conflicts in more files is reported
# with the count, so a long list never bloats the task doc or the IMPLEMENT prompt.
_CONFLICT_PATHS_CAP = 50
_STDERR_TAIL = 300


@dataclass(frozen=True)
class BaseSyncOutcome:
    """What one sync did.

    ``outcome`` is one of ``synced``, ``already_current``, ``conflict`` or ``skipped``.
    ``base_ref`` is the remote ref the branch was compared with (``origin/main``), and
    ``base_tip`` the commit it pointed at. ``head_before``/``head_after`` are the task
    branch before and after (equal unless ``synced``). ``conflicts`` lists the paths a
    conflicting merge left unmerged (capped; ``conflicts_count`` is the full number).
    ``reason`` says why a sync was skipped. ``advance_base_sha`` is the base tip when the
    task's recorded ``base_sha`` is an ancestor of it, so the task's own change can be
    measured from the new tip. It stays None when ``base_sha`` is off trunk, as it is
    for a task that composed dependency branches in at INTAKE."""

    outcome: str
    base_ref: str | None = None
    base_tip: str | None = None
    head_before: str | None = None
    head_after: str | None = None
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    conflicts_count: int = 0
    reason: str | None = None
    advance_base_sha: str | None = None


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess | None:
    """``run_git`` that turns a timeout or a missing git binary into None."""
    try:
        return run_git(cwd, *args)
    except (OSError, subprocess.SubprocessError):
        return None


def _tail(proc: subprocess.CompletedProcess | None) -> str:
    if proc is None:
        return "git did not run (timeout or not installed)"
    return ((proc.stderr or "") + (proc.stdout or "")).strip()[-_STDERR_TAIL:]


def _rev(cwd: str, ref: str) -> str | None:
    proc = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _resolve_base_ref(cwd: str, project_base: str | None) -> str | None:
    """The remote base ref: the project's declared branch, else ``origin/HEAD``, else
    ``origin/main`` or ``origin/master`` when either exists."""
    candidates: list[str] = []
    if project_base:
        name = project_base.strip()
        candidates.append(name if name.startswith("origin/") else f"origin/{name}")
    head = _git(cwd, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head is not None and head.returncode == 0 and head.stdout.strip():
        candidates.append(head.stdout.strip())
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        if _rev(cwd, ref):
            return ref
    return None


def _is_ancestor(cwd: str, older: str, newer: str) -> bool | None:
    proc = _git(cwd, "merge-base", "--is-ancestor", older, newer)
    if proc is None or proc.returncode not in (0, 1):
        return None
    return proc.returncode == 0


def _advance(cwd: str, base_sha: str | None, base_tip: str) -> str | None:
    if not base_sha or base_sha == base_tip:
        return None
    return base_tip if _is_ancestor(cwd, base_sha, base_tip) else None


def sync_base(
    worktree: str, *, base_sha: str | None, project_base: str | None = None
) -> BaseSyncOutcome:
    """Fetch ``origin`` and merge its base branch into ``worktree`` when the branch does
    not already contain it. Never raises: a missing worktree, no ``origin`` remote, a
    failed fetch, an unresolvable base, uncommitted tracked changes, or a merge that
    fails for any reason other than a conflict all come back as ``skipped`` with a
    ``reason``. A conflicting merge is aborted, so the tree is left as it was."""
    if not worktree or not Path(worktree).is_dir():
        return BaseSyncOutcome(SKIPPED, reason="task worktree missing")
    remote = _git(worktree, "remote", "get-url", "origin")
    if remote is None or remote.returncode != 0:
        return BaseSyncOutcome(SKIPPED, reason="no origin remote")
    fetch = _git(worktree, "fetch", "--prune", "origin")
    if fetch is None or fetch.returncode != 0:
        return BaseSyncOutcome(SKIPPED, reason=f"git fetch failed: {_tail(fetch)}")
    base_ref = _resolve_base_ref(worktree, project_base)
    if base_ref is None:
        return BaseSyncOutcome(SKIPPED, reason="no base ref found on origin")
    base_tip = _rev(worktree, base_ref)
    head = _rev(worktree, "HEAD")
    if base_tip is None or head is None:
        return BaseSyncOutcome(SKIPPED, base_ref=base_ref, reason="could not read HEAD or base")
    contained = _is_ancestor(worktree, base_tip, head)
    if contained is None:
        return BaseSyncOutcome(
            SKIPPED, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=head, reason="could not compare the branch with its base",
        )
    if contained:
        return BaseSyncOutcome(
            ALREADY_CURRENT, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=head, advance_base_sha=_advance(worktree, base_sha, base_tip),
        )
    status = _git(worktree, "status", "--porcelain", "--untracked-files=no")
    if status is None or status.returncode != 0:
        return BaseSyncOutcome(
            SKIPPED, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=head, reason=f"git status failed: {_tail(status)}",
        )
    if status.stdout.strip():
        return BaseSyncOutcome(
            SKIPPED, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=head, reason="uncommitted tracked changes in the task worktree",
        )
    # --no-verify: this is an engine merge, not a stage's commit, so project commit hooks
    # (which may call tools the engine does not have) do not run on it.
    merge = _git(
        worktree, "merge", "--no-edit", "--no-verify",
        "-m", f"Merge {base_ref} into the task branch before delivery", base_tip,
    )
    if merge is not None and merge.returncode == 0:
        after = _rev(worktree, "HEAD") or head
        return BaseSyncOutcome(
            SYNCED, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=after, advance_base_sha=_advance(worktree, base_sha, base_tip),
        )
    unmerged = _git(worktree, "diff", "--name-only", "--diff-filter=U")
    paths = [
        p for p in ((unmerged.stdout if unmerged and unmerged.returncode == 0 else "")
                    .splitlines()) if p.strip()
    ]
    in_merge = _rev(worktree, "MERGE_HEAD") is not None
    if in_merge:
        _git(worktree, "merge", "--abort")
    if paths:
        return BaseSyncOutcome(
            CONFLICT, base_ref=base_ref, base_tip=base_tip, head_before=head,
            head_after=_rev(worktree, "HEAD") or head,
            conflicts=tuple(paths[:_CONFLICT_PATHS_CAP]), conflicts_count=len(paths),
        )
    return BaseSyncOutcome(
        SKIPPED, base_ref=base_ref, base_tip=base_tip, head_before=head,
        head_after=_rev(worktree, "HEAD") or head,
        reason=f"git merge failed: {_tail(merge)}",
    )
