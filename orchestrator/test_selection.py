"""Impacted-test selection for the TEST stage (#558).

The TEST stage used to run the project's whole suite on every task, however small the
change. A project adapter may now map the task's changed files to the tests worth running
via the optional ``impacted_tests`` hook (see ``orchestrator/ports/project.py``); this
module is the engine-side decision around that hook. It stays project-agnostic — the
mapping is the adapter's, never the engine's.

The decision is conservative by construction. Anything the engine cannot vouch for falls
back to the FULL suite (never worse than before #558), and every choice carries a reason
so a green run on a subset is recorded as such rather than reading as a full green:

- the changed-file list is unknown (git error) or empty;
- the adapter has no ``impacted_tests`` hook, the hook raises, or it returns ``None``
  ("can't tell") or something that is not a list of strings;
- the diff touches a path the adapter declares global (``test_global_paths`` — conftest,
  dependency files, config) whose change can affect any test;
- the hook returns an empty set for a diff that reached this point (a docs-only diff is
  skipped before selection runs, so an empty set here means "no mapping", not "no tests").

``select_tests`` is pure apart from calling the adapter hook and never raises.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal

from .gitcmd import run_git

TestScope = Literal["impacted", "full"]


@dataclass(frozen=True)
class TestSelection:
    """Which tests the TEST stage should run, and why.

    ``files`` is empty for ``scope == "full"``; ``reason`` is a short human-readable cause
    recorded on the stage result as ``test_scope_reason``."""

    __test__ = False  # not a pytest test class, despite the name

    scope: TestScope
    files: tuple[str, ...]
    reason: str


def _full(reason: str) -> TestSelection:
    return TestSelection(scope="full", files=(), reason=reason)


def changed_files(cwd: str, base_sha: str) -> list[str] | None:
    """Files changed between ``base_sha`` and the current worktree — committed AND
    uncommitted tracked changes (``git diff --name-only <base>``) plus untracked new files
    (``git ls-files --others``). Returns None on any git error, so a read we can't do never
    narrows a test run (or tags a change docs-only)."""
    try:
        diff = run_git(cwd, "diff", "--name-only", base_sha)
    except (OSError, ValueError):
        return None
    if diff.returncode != 0:
        return None
    files = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    try:
        others = run_git(cwd, "ls-files", "--others", "--exclude-standard")
    except (OSError, ValueError):
        return files
    if others.returncode == 0:
        files += [ln.strip() for ln in others.stdout.splitlines() if ln.strip()]
    return files


def global_paths(project: object) -> list[str]:
    """The adapter's ``test_global_paths`` globs (attribute or zero-arg callable), or [] when
    absent or unreadable. Tolerant: a broken declaration must not crash the stage."""
    value = getattr(project, "test_global_paths", None)
    try:
        if callable(value):
            value = value()
    except Exception:  # noqa: BLE001 - an adapter declaration must never crash the stage
        return []
    if isinstance(value, str) or not isinstance(value, Iterable):
        return []
    return [str(p) for p in value if p]


def matches_global(path: str, patterns: list[str]) -> str | None:
    """The first pattern ``path`` matches, or None. A pattern without a ``/`` matches the
    file NAME at any depth (``conftest.py`` hits ``tests/conftest.py``); one with a ``/``
    matches the whole repo-relative path, ``*`` crossing directories (``.github/*``)."""
    p = PurePosixPath(path.strip())
    for pat in patterns:
        target = str(p) if "/" in pat else p.name
        if fnmatchcase(target, pat):
            return pat
    return None


def _takes_cwd(hook: object) -> bool:
    """Whether the hook accepts a ``cwd`` keyword. The worktree is offered, not imposed:
    a one-argument ``impacted_tests(changed)`` is a valid hook too."""
    try:
        params = inspect.signature(hook).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return "cwd" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def select_tests(
    project: object, changed: list[str] | None, *, cwd: str | None = None
) -> TestSelection:
    """Decide the TEST stage's scope for a change. Never raises; see the module docstring
    for every case that falls back to the full suite. ``cwd`` (the task worktree) is passed
    to the hook (when it accepts a ``cwd`` keyword) so it can resolve test files that exist
    there."""
    if changed is None:
        return _full("changed files unknown (git diff unavailable)")
    if not changed:
        return _full("no changed files")
    hook = getattr(project, "impacted_tests", None)
    if not callable(hook):
        return _full("project adapter has no impacted_tests hook")
    patterns = global_paths(project)
    for path in changed:
        if (pat := matches_global(path, patterns)) is not None:
            return _full(f"{path} matches global test input {pat!r}")
    try:
        selected = hook(list(changed), cwd=cwd) if _takes_cwd(hook) else hook(list(changed))
    except Exception as exc:  # noqa: BLE001 - an adapter hook must never crash the stage
        return _full(f"impacted_tests raised {type(exc).__name__}")
    if selected is None:
        return _full("impacted_tests could not map the change")
    if not isinstance(selected, list) or not all(isinstance(f, str) for f in selected):
        return _full("impacted_tests returned a non-list")
    files = tuple(dict.fromkeys(f.strip() for f in selected if f.strip()))
    if not files:
        return _full("impacted_tests selected no tests for a code change")
    return TestSelection(
        scope="impacted", files=files,
        reason=f"{len(files)} test file(s) mapped from {len(changed)} changed file(s)",
    )
