"""Deterministic intake/setup runner — the ENGINE lane (no model call).

heysoo #227: an LLM asked to run ``git worktree add`` + emit structured JSON does the
agentic work then answers in prose, failing schema validation. So setup is a script,
not a model call. This runner serves the ``(ExecutionMode.ENGINE, Provider.NONE)`` cell:
it creates/reuses an isolated worktree+branch, best-effort installs deps, tags the
baseline, and returns the ``intake`` contract — deterministically, at $0. Ports
``run_setup_stage`` from the reference bash system.

A project may override the git logic with a ``setup_task(task_id) -> dict`` method (the
intake structured_output) — duck-typed, so tests supply a no-git fake and offline
projects can pick their own worktree convention.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .base import SUPPORTED, CapabilityDescriptor
from .transport import RawResult, _git, _tag_head, to_stage_result


def _ref_safe(s: str) -> str:
    """Git-ref-safe id component (local copy of engine._ref_safe to avoid an
    engine<-adapter import cycle)."""
    return re.sub(r"[^\w.\-]", "-", s) or "x"


class _SetupError(Exception):
    """A deterministic-setup failure (e.g. git error) — surfaced as ResultStatus.FAILURE."""


class DeterministicSetupRunner:
    """In-process runner for the deterministic ENGINE lane (currently: intake setup)."""

    def __init__(self, project: object, *, base_ref: str = "HEAD") -> None:
        self._project = project  # ProjectConfig (install_cmd; optional setup_task override)
        self._base_ref = base_ref

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                execution_mode=ExecutionMode.ENGINE,
                provider=Provider.NONE,
                in_process=True,
                schema_enforced=True,
                status=SUPPORTED,
            )
        ]

    def dispatch(self, work: WorkItem) -> StageResult:
        override = getattr(self._project, "setup_task", None)
        try:
            if callable(override):
                out = dict(override(work.task_id))
                wt = out.get("worktree")
                checkpoint = (
                    _tag_head(wt, work.checkpoint_tag)
                    if work.checkpoint_tag and wt and Path(wt).exists()
                    else None
                )
            else:
                out, checkpoint = self._git_setup(work)
        except Exception as exc:  # noqa: BLE001 - every dispatch MUST yield a StageResult,
            # never an escaped exception (a _git timeout / stale index.lock, or a raising
            # project setup_task, would otherwise leave the dispatch lease held with no CLI
            # path to clear it). Mirror claude_cli_transport's convert-to-FAILURE contract.
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:setup")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        raw = RawResult(out, exit_code=0, invocation="engine:setup", checkpoint=checkpoint)
        return to_stage_result(work, raw, ResultStatus.SUCCESS,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def _git_setup(self, work: WorkItem) -> tuple[dict, dict | None]:
        top = _git(".", "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise _SetupError("not inside a git repository")
        repo_root = Path(top.stdout.strip())

        safe = _ref_safe(work.task_id.lstrip("#"))
        branch = f"task/{safe}"
        worktree = repo_root / ".worktrees" / safe
        self._ensure_worktree(repo_root, worktree, branch)

        # Best-effort dependency install (never fatal — a later stage's `uv run`/`npm`
        # re-syncs; a broken install shouldn't block worktree readiness).
        install_note = self._run_project(self._project.install_cmd, worktree)

        checkpoint = _tag_head(str(worktree), work.checkpoint_tag) if work.checkpoint_tag else None
        head = _git(str(worktree), "rev-parse", "HEAD")
        base_sha = head.stdout.strip()[:12] if head.returncode == 0 else "?"

        out = {
            "branch": branch,
            "worktree": str(worktree),
            "baseline_captured": True,
            "baseline": f"isolated worktree off {base_sha}; install: {install_note}",
        }
        return out, checkpoint

    def _ensure_worktree(self, repo_root: Path, worktree: Path, branch: str) -> None:
        """Create the worktree+branch, or reuse an existing one (retry idempotency).
        Ports the reference ``run_setup_stage`` create/reuse + stale-branch retry."""
        if worktree.exists():
            co = _git(str(worktree), "checkout", branch)
            if co.returncode != 0:
                co = _git(str(worktree), "checkout", "-b", branch, self._base_ref)
            if co.returncode != 0:
                raise _SetupError(f"could not check out {branch} in existing worktree: "
                                  f"{co.stderr.strip()[:200]}")
            return
        worktree.parent.mkdir(parents=True, exist_ok=True)
        add = _git(str(repo_root), "worktree", "add", str(worktree), "-b", branch, self._base_ref)
        if add.returncode != 0:
            # The branch likely already exists (prior run). Try to REUSE it (attach a
            # worktree to the existing branch) before anything destructive — never
            # `branch -D` first, which would discard unmerged commits on that branch.
            reuse = _git(str(repo_root), "worktree", "add", str(worktree), branch)
            if reuse.returncode == 0:
                return
            # Last resort: the branch is checked out elsewhere / genuinely stale. Delete
            # and recreate from base (matches the reference run_setup_stage retry).
            _git(str(repo_root), "branch", "-D", branch)
            add = _git(str(repo_root), "worktree", "add", str(worktree), "-b", branch, self._base_ref)
        if add.returncode != 0:
            raise _SetupError(f"git worktree add failed: {add.stderr.strip()[:200]}")

    @staticmethod
    def _run_project(cmd_fn, cwd: Path) -> str:
        """Run a project command (install) best-effort; return a short status note."""
        try:
            argv = cmd_fn()
        except Exception:  # noqa: BLE001 - a project command surface must never fail setup
            return "skipped (no command)"
        if not argv or argv == ["true"]:  # the no-op sentinel
            return "n/a"
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=600)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as exc:
            return f"error ({type(exc).__name__})"
        return "ok" if proc.returncode == 0 else f"rc={proc.returncode}"
