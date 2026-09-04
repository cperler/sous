"""Worktree-origin SOURCE probes that resolve through the project's own test runner (#502).

A ``worktree_origin_probes()`` entry of kind ``"source"`` is supposed to prove that the code
a stage's tests import lives in the workspace under test. The reference probe established
that with ``<runner> python -c "import pkg; print(pkg.__file__)"`` — and that invocation
cannot prove it, because a bare ``python -c`` puts the *current directory* first on
``sys.path``. The local copy therefore always wins the import, whatever the installed
environment points at. A live REVIEW (family-finance ``ff-batch-20260903-1724``, task #201)
saw exactly that split: ``python -c`` resolved the module inside the review workspace while
``pytest``, in the same environment, imported the same module from a sibling worktree — and
the reviewer only noticed because a mutation it made never took effect.

So the probe has to go through the thing that will actually run the tests. This module
builds that probe once, for every adapter that wants it:

* write a throwaway test file WHERE THE PROJECT'S OWN TESTS LIVE (so the runner's rootdir,
  ini file, ``conftest.py`` chain and — decisively — the ``sys.path`` entry the runner
  prepends for a collected file are the REAL ones, not a stand-in's; a probe test dropped at
  the repo root would put the root on ``sys.path`` and hand back the same cwd-first lie),
* run it with the project's own unit-test argv,
* have the collected test record where the runner imported the module from, and
* print that path — the contract's "final non-empty stdout line".

Kind ``"runner-source"`` labels the result, so a notice says which flavour of evidence was
(or was not) available; containment is checked exactly like ``"source"``.

What the probe inherits, it inherits honestly: if the project's own runner puts the working
directory first on ``sys.path`` (``python -m pytest`` does, a bare ``pytest`` console script
does not), so do the project's real tests, and the probe reports what they would import.
The probe never adds a bias the suite does not have.

Fail-closed by construction: a runner that cannot run, a probe test that does not import,
or output the runner swallowed all leave the command non-zero or silent, and the execution
adapter treats both as an origin-verification failure rather than a pass.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

#: Placeholder an adapter puts where the probe test file belongs in its test argv, so the
#: probe runs through the SAME command the project's unit tests do (``test_unit_cmd`` may
#: put the file before its flags). Appended if the caller leaves it out.
PROBE_FILE = "__WORKTREE_ORIGIN_PROBE_FILE__"

_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

# The probe test. It writes to a file rather than stdout because a test runner CAPTURES
# stdout by default: a printed path would be swallowed on the (normal) passing run.
_PROBE_TEST = '''import os
import pathlib


def test_worktree_origin_probe() -> None:
    """Record where this runner really imports {module} from (#502)."""
    destination = os.environ.get("WORKTREE_ORIGIN_PROBE_OUT")
    if not destination:
        # Swept up by an unrelated run of the suite that overlapped this probe's brief
        # lifetime: report nothing rather than failing somebody else's test run. The probe
        # itself then finds no output file and fails closed, which is the safe direction.
        return

    import {module} as _probed_module

    pathlib.Path(destination).write_text(str(_probed_module.__file__))
'''

# `$$` keeps two concurrent probes apart; the trap removes both files on any exit, so a
# workspace is not left dirty. The runner's own output is held back unless it FAILS, where
# it becomes the failure detail the execution adapter reports.
_SCRIPT = '''set -eu
dir="."
for candidate in {test_dirs}; do
    if [ -d "$candidate" ]; then
        dir="$candidate"
        break
    fi
done
probe="$dir/test_worktree_origin_probe_$$.py"
out="$dir/.worktree-origin-probe-$$.txt"
WORKTREE_ORIGIN_PROBE_OUT="$out"
export WORKTREE_ORIGIN_PROBE_OUT
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE
trap 'rm -f "$probe" "$out"' EXIT HUP INT TERM
rm -f "$probe" "$out"
cat > "$probe" <<'WORKTREE_ORIGIN_PROBE_PY'
{test_body}
WORKTREE_ORIGIN_PROBE_PY
if ! runner_log="$({command} 2>&1)"; then
    printf '%s\\n' "$runner_log" >&2
    exit 1
fi
cat "$out"
'''


def runner_source_probe(
    module: str,
    test_argv: Sequence[str],
    *,
    name: str | None = None,
    test_dirs: Sequence[str] = ("tests", "test"),
) -> tuple[str, list[str], str]:
    """Build a ``"runner-source"`` probe resolving ``module`` through ``test_argv``.

    ``test_argv`` is the project's real unit-test invocation with :data:`PROBE_FILE` where
    the file argument goes (usually ``self.test_unit_cmd([PROBE_FILE])``); the placeholder is
    appended when absent. The returned triple is a ``worktree_origin_probes()`` entry whose
    reported path is what the TEST RUNNER imported — the only import that a later test result
    can be attributed to.

    ``test_dirs`` are the candidate directories the throwaway probe test is written into, in
    order, falling back to the workspace root. This matters: pytest prepends a collected
    file's own base directory to ``sys.path``, so a probe sitting somewhere the real tests do
    not sit can resolve the local copy while the suite resolves an installed one. Override it
    for a project whose tests live elsewhere.
    """
    if not _MODULE_RE.match(module):
        raise ValueError(f"probe module must be a dotted python name, got {module!r}")
    argv = [str(arg) for arg in test_argv]
    if not argv:
        raise ValueError("probe test argv must not be empty")
    if PROBE_FILE not in argv:
        argv.append(PROBE_FILE)
    command = shlex.join(argv).replace(shlex.quote(PROBE_FILE), '"$probe"')
    dirs = " ".join(shlex.quote(str(d)) for d in test_dirs) or "."
    script = _SCRIPT.format(
        test_dirs=dirs, test_body=_PROBE_TEST.format(module=module), command=command
    )
    return (name or f"{module} module (test-runner import)", ["sh", "-c", script], "runner-source")
