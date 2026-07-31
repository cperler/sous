"""The one-line ``git`` subprocess helper, owned by the engine layer (#273).

It used to live in ``adapters/execution/transport.py`` as a private ``_git``, which meant
the ``gc`` CLI subcommand reached into an execution adapter — and into a PRIVATE symbol of
one — just to list checkpoint tags. Git is not an execution-lane concern (no provider, no
model, no lane policy), so it sits here and the arrow points inward: ``transport.py``
imports it, not the reverse.

Deliberately thin: capture text output, never raise on a non-zero exit (every caller reads
``returncode``/``stderr`` itself and decides), and cap the wait so a stale ``index.lock``
or a hung credential prompt fails the stage instead of wedging the run forever.
"""

from __future__ import annotations

import subprocess

# Long enough for a worktree add / fetch-less merge on a large repo, short enough that a
# hung git (credential prompt, stale lock) surfaces as a stage failure rather than a wedge.
GIT_TIMEOUT_S = 60


def run_git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` and return the completed process (never raises on
    a non-zero exit — callers inspect ``returncode``)."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,  # noqa: S603, S607
                          text=True, timeout=GIT_TIMEOUT_S)
