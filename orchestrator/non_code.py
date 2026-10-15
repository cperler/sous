"""Is a task's change made only of non-code files? The pure half of the OPTIMIZE skip.

OPTIMIZE (#519) is inserted into EVERY task's pipeline once a project's roster names an
``optimize`` agent, so a docs-only task used to pay a high-effort model call just to hear
"impact: none" (prediction ``batch-400``, $0.47). The engine now skips the stage, with no
model call, when every file the task changed matches a non-code glob. This module is the
decision; ``Engine`` does the git read and records the skip.

Conservative by construction: a path that matches no glob counts as CODE, so an unknown
extension always gets the pass. The defaults are documentation and prose only — config and
data formats (json/yaml/toml), and ``*.txt`` (``requirements.txt``, ``CMakeLists.txt``),
are deliberately absent because changing them can change what the program does. An empty
change counts as non-code: there is nothing to optimize.

A project may replace the globs through the optional ``non_code_globs`` adapter hook (see
``orchestrator/ports/project.py``); to EXTEND them it returns
``[*DEFAULT_NON_CODE_GLOBS, "experiments/**"]``. Glob semantics are the
``test_global_paths`` ones (``test_selection.matches_global``): a pattern without ``/``
matches the file NAME at any depth, one with ``/`` matches the whole repo-relative path
with ``*`` crossing directories.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .test_selection import matches_global

DEFAULT_NON_CODE_GLOBS: tuple[str, ...] = (
    "*.md",
    "*.markdown",
    "*.rst",
    "*.adoc",
    "docs/**",
    "*/docs/**",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "COPYING",
    "NOTICE",
    "AUTHORS",
    "CHANGELOG",
    "CHANGELOG.txt",
)


@dataclass(frozen=True)
class NonCodeVerdict:
    """The classification of one changeset: ``all_non_code`` plus both partitions."""

    all_non_code: bool
    non_code: tuple[str, ...]
    code: tuple[str, ...]


def classify_non_code(changed: Sequence[str], globs: Sequence[str]) -> NonCodeVerdict:
    """Partition ``changed`` into non-code (matches a glob) and code (matches none).

    ``all_non_code`` is True when no path is code — including the empty changeset. Pure."""
    patterns = [g for g in globs if g]
    non_code: list[str] = []
    code: list[str] = []
    for raw in changed:
        path = str(raw).strip()
        if not path:
            continue
        (non_code if matches_global(path, patterns) else code).append(path)
    return NonCodeVerdict(all_non_code=not code, non_code=tuple(non_code), code=tuple(code))


def resolve_non_code_globs(project: object) -> tuple[tuple[str, ...], str | None]:
    """The project's non-code globs and a notice when its declaration was unusable.

    Reads the optional ``non_code_globs`` hook (a sequence attribute or a zero-arg
    callable). Absent → the defaults, no notice. A hook that raises or returns something
    other than a sequence of strings → the defaults plus a notice naming why, which the
    engine events (a broken declaration is never silently ignored). An explicitly empty
    list is honoured: the project says nothing is non-code, and the engine then never
    skips OPTIMIZE (not even over an empty change)."""
    value = getattr(project, "non_code_globs", None)
    if value is None:
        return DEFAULT_NON_CODE_GLOBS, None
    try:
        if callable(value):
            value = value()
    except Exception as exc:  # noqa: BLE001 - an adapter declaration must never crash dispatch
        return DEFAULT_NON_CODE_GLOBS, f"non_code_globs raised {type(exc).__name__}: {exc}"
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return DEFAULT_NON_CODE_GLOBS, (
            f"non_code_globs returned {type(value).__name__}, not a list of globs"
        )
    items = list(value)
    if not all(isinstance(g, str) for g in items):
        return DEFAULT_NON_CODE_GLOBS, "non_code_globs contained a non-string entry"
    return tuple(g for g in items if g), None
