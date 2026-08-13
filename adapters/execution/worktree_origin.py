"""Adapter-declared worktree provisioning and executable-origin verification.

The execution layer cannot infer how an arbitrary project's test command locates its
runner or source tree.  Projects may therefore declare two small, duck-typed hooks:

``fresh_install_paths() -> list[str]``
    Worktree-relative dependency artifacts which must never be copied into a disposable
    REVIEW checkout (for example ``.venv``).

``worktree_origin_probes() -> list[tuple[str, list[str], str]]``
    Named commands whose final non-empty stdout line is an absolute path used by the
    toolchain.  The third value is ``"launcher"`` for an in-worktree executable whose final
    symlink may target a shared interpreter, or ``"source"`` for imported code whose real
    path must live below the worktree.  Legacy two-value probes are treated as ``"source"``.

Omitting the probe hook is an explicit, warning-grade skip rather than a guessed pass.
Declaring a probe makes it fail closed: an unrunnable probe or an outside path means no test
result from that workspace may be trusted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_PROBE_TIMEOUT_S = 60


@dataclass(frozen=True)
class OriginVerification:
    """Result of checking every adapter-declared path against one worktree."""

    trusted: bool
    notices: tuple[dict[str, object], ...] = ()


def fresh_install_paths(project: object) -> tuple[PurePosixPath, ...]:
    """Return safe, relative artifact paths declared by ``project``.

    Invalid paths are ignored: cleanup must never be able to escape the provisioned
    worktree.  Adapters remain responsible for declaring every artifact that cannot be
    safely copied.
    """
    getter = getattr(project, "fresh_install_paths", None)
    if not callable(getter):
        return ()
    try:
        values = getter()
    except Exception:  # noqa: BLE001 - an optional adapter hook cannot break cleanup
        return ()
    paths: list[PurePosixPath] = []
    for value in values or ():
        if not isinstance(value, str):
            continue
        path = PurePosixPath(value)
        if not value or path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
            continue
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def remove_fresh_install_paths(worktree: Path, project: object) -> None:
    """Remove declared dependency artifacts without following them outside ``worktree``."""
    for relative in fresh_install_paths(project):
        target = worktree.joinpath(*relative.parts)
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target)


def verify_worktree_origin(project: object, worktree: Path) -> OriginVerification:
    """Run adapter probes and reject any resolved path outside ``worktree``.

    The final non-empty stdout line is the declared path so commands may emit ordinary
    setup noise before it.  Paths are resolved (including symlinks) before containment is
    checked.  A configured probe that cannot produce a trustworthy absolute path is a hard
    verification failure, while an absent hook is an observable skip.
    """
    root = worktree.resolve()
    getter = getattr(project, "worktree_origin_probes", None)
    if not callable(getter):
        return OriginVerification(True, (_skip_notice(root, "adapter-declared probes absent"),))
    try:
        probes = getter()
    except Exception as exc:  # noqa: BLE001 - translate adapter failure into a loud refusal
        return OriginVerification(False, (_error_notice(
            root, "adapter hook", f"{type(exc).__name__}: {exc}"
        ),))
    if not probes:
        return OriginVerification(True, (_skip_notice(root, "adapter declared no probes"),))

    notices: list[dict[str, object]] = []
    for value in probes:
        try:
            parts = list(value)
            if len(parts) == 2:
                name, argv = parts
                kind = "source"
            elif len(parts) == 3:
                name, argv, kind = parts
            else:
                raise ValueError
            command = list(argv)
        except (TypeError, ValueError):
            notices.append(
                _error_notice(root, "invalid probe", "expected (name, argv[, kind])")
            )
            continue
        if not isinstance(name, str) or not name or not command or not all(
            isinstance(arg, str) and arg for arg in command
        ):
            notices.append(_error_notice(root, str(name), "probe name/argv is invalid"))
            continue
        if not isinstance(kind, str) or kind not in {"launcher", "source"}:
            notices.append(
                _error_notice(root, name, f"probe kind must be launcher or source, got {kind!r}")
            )
            continue
        try:
            proc = subprocess.run(  # noqa: S603
                command, cwd=root, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S
            )
        except (OSError, subprocess.SubprocessError) as exc:
            notices.append(_error_notice(root, name, f"{type(exc).__name__}: {exc}"))
            continue
        lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if proc.returncode != 0 or not lines:
            detail = (proc.stderr or proc.stdout).strip()[-300:]
            reason = f"rc={proc.returncode}" + (f": {detail}" if detail else "")
            notices.append(_error_notice(root, name, reason))
            continue
        reported = Path(lines[-1])
        if not reported.is_absolute():
            notices.append(_error_notice(root, name, f"probe returned non-absolute path: {reported}"))
            continue
        normalized = Path(os.path.abspath(reported))
        # Only a launcher's FINAL component may point to a shared interpreter. Its parent is
        # still resolved so a copied `.venv` symlink cannot smuggle a sibling launcher under
        # an in-worktree lexical spelling. Imported source is dereferenced completely: an
        # intermediate package symlink executes its real sibling target, not its local alias.
        resolved = (
            normalized.parent.resolve() / normalized.name
            if kind == "launcher"
            else normalized.resolve()
        )
        belongs = resolved.is_relative_to(root)
        if not belongs:
            notices.append({
                "notice": "worktree_origin_mismatch",
                "probe": name,
                "probe_kind": kind,
                "expected_worktree": str(root),
                "reported_path": str(normalized),
                "resolved_path": str(resolved),
                "detail": f"{name} resolved outside the provisioned worktree",
            })
    return OriginVerification(not notices, tuple(notices))


def environment_reset_notice(worktree: Path, reason: str) -> dict[str, object]:
    """Record that declared artifacts were discarded before install.

    Emitted when provisioning itself is the reason an inherited environment cannot be
    trusted, so the discard is auditable even though no probe reported a mismatch.
    """
    return {
        "notice": "worktree_environment_reset",
        "expected_worktree": str(worktree.resolve()),
        "reason": reason,
        "detail": f"declared dependency artifacts were removed before install: {reason}",
    }


def _skip_notice(root: Path, reason: str) -> dict[str, object]:
    return {
        "notice": "worktree_origin_verification_skipped",
        "expected_worktree": str(root),
        "reason": reason,
        "detail": f"toolchain origin was not verified: {reason}",
    }


def _error_notice(root: Path, probe: str, reason: str) -> dict[str, object]:
    return {
        "notice": "worktree_origin_probe_failed",
        "probe": probe,
        "expected_worktree": str(root),
        "reason": reason,
        "detail": f"{probe} could not establish toolchain origin: {reason}",
    }
