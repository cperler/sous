"""Deterministic ENGINE-lane runner — no model call (heysoo #227).

heysoo #227: an LLM asked to run ``git worktree add`` + emit structured JSON does the
agentic work then answers in prose, failing schema validation. So the mechanical stages
are scripts, not model calls. This runner serves the whole
``(ExecutionMode.ENGINE, Provider.NONE)`` cell and dispatches by stage:

  - INTAKE  → this module: create/reuse an isolated worktree+branch, best-effort install,
    tag the baseline, actually run the unit tests to capture the baseline (``intake``).
    Ports ``run_setup_stage`` from the reference bash system.
  - TEST    → ``deterministic_test.DeterministicTestRunner`` (#33): run the project's test
    commands, classify failures, split inherited baseline red from caused (``test``).
  - DELIVER → ``deterministic_deliver.DeterministicDeliverRunner`` (#33): push the branch
    and open/reuse a PR (``deliver``).

The registry keys one runner per cell, so this class is the single ENGINE-lane entry and
delegates TEST/DELIVER to their focused modules (the engine still never imports any of
them — they are execution-adapter concerns).

For INTAKE a project may override the git logic with a ``setup_task(task_id) -> dict``
method (the intake structured_output) — duck-typed, so tests supply a no-git fake and
offline projects can pick their own worktree convention.

The built-in git path discovers the product repo from an explicit ``repo_root`` the
project supplies (``ProjectConfig.repo_root`` — optional, duck-typed), falling back to
process CWD (``.``) when absent (#42: don't bind git to the orchestrator's CWD).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from adapters.project.base import ProjectConfig
from orchestrator.port_registry import (
    port_env_for,
    project_needs_ports,
    registry_for_project,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import StageResult, WorkItem

from . import install_cache
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

    def __init__(self, project: ProjectConfig, *, base_ref: str = "HEAD") -> None:
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
        # The ENGINE cell serves every deterministic stage; delegate the non-intake ones to
        # their focused runners (#33). Local imports keep the adapter modules decoupled and
        # avoid pulling test/deliver in when only intake runs.
        if work.stage is Stage.TEST:
            from .deterministic_test import DeterministicTestRunner

            return DeterministicTestRunner(self._project).dispatch(work)
        if work.stage is Stage.DELIVER:
            from .deterministic_deliver import DeterministicDeliverRunner

            return DeterministicDeliverRunner(self._project).dispatch(work)
        # #5: allocate this task's per-task port block AT INTAKE (where the worktree is
        # created), so every later stage's dev/test server binds a slice unique to the task.
        # Best-effort and opt-in: a project without port needs (no port_env/needs_ports)
        # gets None and nothing is recorded — a clean no-op.
        ports = self._allocate_ports(work)
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
                out, checkpoint = self._git_setup(work, ports)
            if ports is not None:
                out["port_base"], out["port_count"] = ports
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

    def _allocate_ports(self, work: WorkItem) -> tuple[int, int] | None:
        """Reserve this task's contiguous port block (#5), returning ``(base, count)`` or
        None. No-op + None when the project doesn't opt in (``project_needs_ports``), the
        range is exhausted, or anything raises — port allocation must never break intake."""
        if not project_needs_ports(self._project):
            return None
        try:
            reg = registry_for_project(self._project)
            base = reg.allocate(work.run_id, work.task_id, pid=os.getpid())
        except Exception:  # noqa: BLE001 - allocation is best-effort; never fail setup
            return None
        return (base, reg.block_size) if base is not None else None

    def _git_setup(
        self, work: WorkItem, ports: tuple[int, int] | None = None
    ) -> tuple[dict, dict | None]:
        # #42: discover the project repo from an EXPLICIT path the project supplies
        # (``ProjectConfig.repo_root`` — optional, duck-typed), not process CWD. The
        # documented fallback is "." (process CWD) so existing callers that run the
        # orchestrator from the product repo root keep working unchanged.
        base = str(getattr(self._project, "repo_root", None) or ".")
        top = _git(base, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise _SetupError("not inside a git repository")
        repo_root = Path(top.stdout.strip())

        safe = _ref_safe(work.task_id.lstrip("#"))
        branch = f"task/{safe}"
        worktree = repo_root / ".worktrees" / safe
        self._ensure_worktree(repo_root, worktree, branch)

        # Best-effort dependency install (never fatal — a later stage's `uv run`/`npm`
        # re-syncs; a broken install shouldn't block worktree readiness). #63: skip it
        # when this worktree already holds a successful install of the same lockfiles.
        install_note, install_meta = self._install(worktree)

        checkpoint = _tag_head(str(worktree), work.checkpoint_tag) if work.checkpoint_tag else None
        head = _git(str(worktree), "rev-parse", "HEAD")
        base_full = head.stdout.strip() if head.returncode == 0 else ""
        base_sha = base_full[:12] if base_full else "?"

        # ACTUALLY capture the test baseline (ADR-035 parity): run the project's unit
        # tests at base and record the pre-existing failures, so the TEST stage can
        # separate regressions-you-introduced from inherited red — deterministically,
        # not by model judgment. baseline_captured is honest: True only when the test
        # command really ran to completion here.
        # #5: capture the baseline with the task's port block exported, so a suite that boots
        # a server binds this task's ports even at the intake baseline run.
        port_env = port_env_for(self._project, *ports) if ports else None
        baseline = self._capture_baseline(worktree, port_env)
        out = {
            "branch": branch,
            "worktree": str(worktree),
            # The fork point (worktree HEAD right after branch creation, before any
            # implement work). The deterministic TEST runner diffs base_sha..worktree to
            # classify the change (#41 docs-only detection). Empty when HEAD is unreadable.
            "base_sha": base_full,
            "baseline_captured": baseline["captured"],
            "baseline_failures": baseline["failures"],
            "baseline": (
                f"isolated worktree off {base_sha}; install: {install_note}; "
                f"tests: {baseline['note']}"
            ),
            **install_meta,  # #63: install_skipped / install_reason / install_lockfiles
        }
        return out, checkpoint

    def _install(self, worktree: Path) -> tuple[str, dict]:
        """Run (or skip) the project's dependency install, keyed per-worktree on the
        lockfile hash (#63). Returns a short human note plus the honest structured-output
        fields (``install_skipped`` / ``install_reason`` / ``install_lockfiles``) so the
        decision is visible in the intake log and events — never silent."""
        names = install_cache.project_lockfiles(self._project)
        present = install_cache.discover(worktree, names)
        digest = install_cache.compute_hash(present)
        lockfiles = [p.name for p in present]
        marker = self._install_marker(worktree)
        if install_cache.should_skip(marker, digest):
            return "skipped (lockfile-hash-match)", {
                "install_skipped": True,
                "install_reason": "lockfile-hash-match",
                "install_lockfiles": lockfiles,
            }
        # Full install. Any doubt lands here; record WHY for the audit trail.
        note = self._run_project(self._project.install_cmd, worktree)
        success = note == "ok"
        if install_cache.cache_disabled():
            reason = "cache-disabled"
        elif digest is None:
            reason = "no-lockfiles"
        elif not success:
            reason = "install-failed"
        else:
            reason = "installed"
        # Only cache when there is a lockfile basis AND somewhere to record it. Persist the
        # outcome (success flag included) so a subsequent FAILED install correctly forces a
        # reinstall next time rather than skipping on a stale success.
        if digest is not None and marker is not None:
            install_cache.save_marker(marker, digest=digest, lockfiles=lockfiles, success=success)
        return note, {
            "install_skipped": False,
            "install_reason": reason,
            "install_lockfiles": lockfiles,
        }

    def _install_marker(self, worktree: Path) -> Path | None:
        """The per-worktree cache marker path (inside this worktree's private git dir), or
        None when it can't be resolved (a non-git worktree => never cache => always install)."""
        gd = _git(str(worktree), "rev-parse", "--absolute-git-dir")
        if gd.returncode != 0 or not gd.stdout.strip():
            return None
        return Path(gd.stdout.strip()) / install_cache.MARKER_NAME

    def _capture_baseline(self, worktree: Path, port_env: dict[str, str] | None = None) -> dict:
        """Run ``test_unit_cmd`` at base and classify the failures (via the project's
        classifier when present). Never fatal: a missing/no-op command, a timeout, or an
        unrunnable suite yields ``captured: False`` with the reason in the note — an
        HONEST miss, never a fabricated baseline. ``port_env`` (#5) is merged over the
        process env so the baseline run uses the task's port block."""
        getter = getattr(self._project, "test_unit_cmd", None)
        try:
            argv = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 - a project command surface must never fail setup
            argv = None
        if not argv or argv == ["true"]:  # the no-op sentinel
            return {"captured": False, "failures": [], "note": "n/a (no unit-test command)"}
        env = {**os.environ, **port_env} if port_env else None
        try:
            proc = subprocess.run(  # noqa: S603
                argv, cwd=worktree, capture_output=True, text=True, timeout=900, env=env
            )
        except subprocess.TimeoutExpired:
            return {"captured": False, "failures": [], "note": "baseline run timed out (900s)"}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"captured": False, "failures": [],
                    "note": f"baseline run error ({type(exc).__name__})"}
        if proc.returncode == 0:
            return {"captured": True, "failures": [], "note": "green at base"}
        failures = self._classify_baseline(f"{proc.stdout}\n{proc.stderr}")
        note = (
            f"RED at base (rc={proc.returncode}): {len(failures)} known-failing test(s)"
            if failures
            else f"RED at base (rc={proc.returncode}); failures unparsed — "
                 f"regression diff unavailable"
        )
        return {"captured": True, "failures": failures[:40], "note": note}

    def _classify_baseline(self, test_output: str) -> list[str]:
        """Failing-test ids from raw output via the project classifier (best-effort)."""
        classifier = getattr(self._project, "classifier", None)
        if classifier is None:
            return []
        try:
            return [f.test for f in classifier.classify(test_output) if f.test != "<infra>"]
        except Exception:  # noqa: BLE001 - classification must never fail setup
            return []

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
