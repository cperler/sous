"""Disposable worktree and port isolation for model-lane REVIEW calls (#301).

A REVIEW deliberately keeps command execution so a verifier can run tests.  Tool posture
alone therefore cannot protect the task's live worktree: shell commands can create caches or
edit files, and a background server can keep the task's port block occupied.  This module
wraps the transport below the runner seam so every single reviewer — and every finder/verifier
sub-call in a panel — receives its own independent Git checkout and port allocation.

The checkout is an independent local clone (no shared object hardlinks or writable remote),
then the live worktree payload is copied over it.  That preserves ignored dependencies and
any dirty state the reviewer was asked to judge while keeping Git reads useful.  Both the
checkout and all temporary port records are discarded when the parent REVIEW dispatch ends.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path

from orchestrator.port_registry import (
    ENV_PORT_BASE,
    PortRegistry,
    port_env_for,
    project_needs_ports,
    registry_for_project,
)
from orchestrator.ports.project import ProjectConfig
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.work import WorkItem

from .transport import RawResult, Transport
from .worktree_origin import (
    fresh_install_paths,
    remove_fresh_install_paths,
    verify_worktree_origin,
)

_GIT_TIMEOUT_S = 120


class _IsolationError(RuntimeError):
    """A REVIEW could not be contained; the call must fail instead of using the live tree."""

    def __init__(self, message: str, *, notices: tuple[dict[str, object], ...] = ()) -> None:
        super().__init__(message)
        self.notices = notices


class ReviewIsolation:
    """Create one disposable workspace/port block per REVIEW transport invocation.

    ``session`` spans the whole runner dispatch.  Port allocations are intentionally retained
    until that outer scope exits, so sequential panel sub-calls cannot be handed the same block.
    If a prior reviewer leaves a listener behind, later allocations are different during the
    panel and future allocations skip the still-bound block via ``PortRegistry``'s bind probe.
    """

    def __init__(self, project: ProjectConfig | None = None) -> None:
        self._project = project

    @contextmanager
    def session(self, work: WorkItem, inner: Transport) -> Iterator[Transport]:
        """Yield a transport that isolates each invocation belonging to ``work``'s REVIEW.

        Synthetic/nonexistent worktrees used by seam tests pass through: there is no live tree
        to protect and no command can execute in them.  Once an existing directory is present,
        isolation is fail-closed — clone/copy/port failures become a failed ``RawResult`` and
        the inner transport is never called on the task worktree.
        """
        source = Path(work.cwd).resolve() if work.cwd else None
        if work.stage is not Stage.REVIEW or source is None or not source.is_dir():
            yield inner
            return

        allocations: list[tuple[PortRegistry, str]] = []
        call_number = 0

        def isolated(sub: WorkItem) -> RawResult:
            nonlocal call_number
            call_number += 1
            allocation: tuple[PortRegistry, str, dict[str, str]] | None = None
            # `None` is meaningful here — it is the dispatch's own "inherit the environment"
            # signal, distinct from an empty mapping — so the unallocated branch passes
            # `sub.env` through unchanged rather than normalizing it to `{}`.
            env: dict[str, str] | None = None
            try:
                allocation = self._allocate_ports(sub, call_number)
                if allocation is not None:
                    registry, key, env = allocation
                    allocations.append((registry, key))
                else:
                    env = sub.env
                with tempfile.TemporaryDirectory(
                    prefix="orchestrator-review-", ignore_cleanup_errors=True
                ) as tmp:
                    isolated_cwd = self._copy_worktree(
                        source, Path(tmp) / "worktree", self._project
                    )
                    self._prepare_toolchain(isolated_cwd)
                    origin = verify_worktree_origin(self._project or object(), isolated_cwd)
                    if not origin.trusted:
                        raise _IsolationError(
                            "toolchain origin does not belong to disposable review worktree",
                            notices=origin.notices,
                        )
                    prompt = sub.prompt.replace(str(source), str(isolated_cwd))
                    if work.cwd:
                        prompt = prompt.replace(work.cwd, str(isolated_cwd))
                    isolated_work = sub.model_copy(update={
                        "cwd": str(isolated_cwd),
                        "env": env,
                        # The rendered context names the task worktree. Redirect that routing
                        # hint too, or a well-behaved reviewer may `cd` back to the live path
                        # before running the exact command isolation was meant to contain.
                        "prompt": prompt,
                        "workspace_isolated": True,
                    })
                    raw = inner(isolated_work)
                    return replace(
                        raw,
                        execution_notices=raw.execution_notices + origin.notices,
                    )
            except _IsolationError as exc:
                return RawResult(
                    None,
                    exit_code=1,
                    error=f"review isolation failed: {exc}",
                    invocation="review isolation",
                    execution_notices=exc.notices,
                )

        try:
            yield isolated
        finally:
            for registry, key in reversed(allocations):
                with suppress(Exception):  # cleanup must not mask the review result
                    registry.release(work.run_id, key)

    def _allocate_ports(
        self, work: WorkItem, call_number: int
    ) -> tuple[PortRegistry, str, dict[str, str]] | None:
        project = self._project
        inherited_task_block = bool(work.env and ENV_PORT_BASE in work.env)
        if project is None or not project_needs_ports(project):
            if inherited_task_block:
                raise _IsolationError(
                    "project port configuration is unavailable for verifier isolation"
                )
            return None
        key = f"{work.task_id}:review:{work.id}:{call_number}"
        try:
            registry = registry_for_project(project)
            base = registry.allocate(work.run_id, key, pid=os.getpid())
        except Exception as exc:  # noqa: BLE001 - converted to a contained dispatch failure
            raise _IsolationError(f"could not allocate verifier ports: {exc}") from exc
        if base is None:
            raise _IsolationError("no verifier port block is available")
        # Preserve unrelated dispatch environment while replacing both the generic port names
        # and every project-specific mapping with values from the isolated block.
        env = {**(work.env or {}), **port_env_for(project, base, registry.block_size)}
        return registry, key, env

    @staticmethod
    def _copy_worktree(
        source: Path, destination: Path, project: ProjectConfig | None = None
    ) -> Path:
        if not (source / ".git").exists():
            raise _IsolationError(f"review cwd is not a Git worktree: {source}")
        clone = ReviewIsolation._run_git(
            [
                "git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout",
                str(source), str(destination),
            ]
        )
        if clone.returncode != 0:
            detail = clone.stderr.strip() or clone.stdout.strip() or "git clone failed"
            raise _IsolationError(detail[:300])

        # A local clone's origin points back at the live repository.  Remove it before the
        # model runs so an accidental `git push` cannot mutate refs outside the disposable
        # checkout.  The object database itself was copied by --no-hardlinks.
        remote = ReviewIsolation._run_git(
            ["git", "-C", str(destination), "remote", "remove", "origin"]
        )
        if remote.returncode != 0:
            raise _IsolationError((remote.stderr.strip() or "could not remove clone origin")[:300])

        try:
            excluded = {path.parts[0] for path in fresh_install_paths(project or object())}
            for entry in source.iterdir():
                if entry.name == ".git":
                    continue
                if entry.name in excluded:
                    continue
                target = destination / entry.name
                if entry.is_symlink():
                    target.symlink_to(os.readlink(entry), target_is_directory=entry.is_dir())
                elif entry.is_dir():
                    shutil.copytree(entry, target, symlinks=True)
                else:
                    shutil.copy2(entry, target, follow_symlinks=False)
        except (OSError, shutil.Error) as exc:
            raise _IsolationError(f"could not copy reviewed worktree: {exc}") from exc

        # Populate the independent index without changing the copied payload.  Git status/diff
        # now describe the exact dirty state copied from the live worktree.
        reset = ReviewIsolation._run_git(
            ["git", "-C", str(destination), "reset", "--mixed", "--quiet", "HEAD"]
        )
        if reset.returncode != 0:
            raise _IsolationError((reset.stderr.strip() or "could not initialize clone index")[:300])
        return destination

    def _prepare_toolchain(self, worktree: Path) -> None:
        """Create disposable dependencies fresh when the adapter declares copied artifacts."""
        project = self._project
        if project is None or not fresh_install_paths(project):
            return
        # Defense in depth for nested declarations: top-level artifacts were never copied,
        # and anything below a copied directory is removed before any project command runs.
        remove_fresh_install_paths(worktree, project)
        getter = getattr(project, "install_cmd", None)
        try:
            argv = getter() if callable(getter) else None
        except Exception as exc:  # noqa: BLE001 - fail the contained review, never escape it
            raise _IsolationError(f"could not resolve review install command: {exc}") from exc
        if not argv or argv == ["true"]:
            raise _IsolationError(
                "fresh review dependencies were requested but no install command is declared"
            )
        try:
            proc = subprocess.run(  # noqa: S603
                argv, cwd=worktree, capture_output=True, text=True, timeout=600
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _IsolationError(f"review dependency install failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-300:]
            raise _IsolationError(
                f"review dependency install failed (rc={proc.returncode}): {detail}"
            )

    @staticmethod
    def _run_git(argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _IsolationError(f"Git isolation command failed: {exc}") from exc
