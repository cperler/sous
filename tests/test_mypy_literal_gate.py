"""The mypy Literal gate actually bites at the abandon call site (#194).

``Engine.abandon``'s ``disposition`` is typed ``Literal["failed", "rejected"]``. ruff does
no cross-function inference, so before #194 a caller passing an out-of-range string was
never flagged. #194 wired ``mypy`` into the gate AND narrowed the CLI's argparse-laundered
``args.disposition`` (an ``Any``) to the Literal at the call site, so the annotation turned
from documentation into a real analysis gate.

This test proves the gate is live: mypy rejects a bogus disposition literal and accepts a
valid one. Marked ``slow`` because it spawns a mypy subprocess (deselect with
``-m 'not slow'``); it still runs in the full CI ``pytest`` invocation that #194 added.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_CALLER = """
from orchestrator.engine import Engine
from orchestrator.schemas.status import Task


def call(eng: Engine) -> None:
    eng.abandon("r", "t", reason="x", disposition={disposition!r})
"""


def _run_mypy(target: Path) -> subprocess.CompletedProcess[str]:
    # --follow-imports=silent type-checks the caller against the real Engine signature
    # without re-reporting errors inside its (already-clean) transitive imports, keeping
    # the check to ~1s.
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--follow-imports=silent",
         "--no-error-summary", str(target)],
        capture_output=True, text=True,
    )


@pytest.mark.slow
def test_mypy_flags_out_of_range_disposition(tmp_path) -> None:
    bad = tmp_path / "bad_caller.py"
    bad.write_text(textwrap.dedent(_CALLER.format(disposition="bogus")))
    result = _run_mypy(bad)
    assert result.returncode != 0, f"mypy should reject a non-Literal disposition:\n{result.stdout}"
    assert "disposition" in result.stdout and "abandon" in result.stdout


@pytest.mark.slow
def test_mypy_accepts_valid_disposition(tmp_path) -> None:
    # The complement: a valid Literal is clean, so the failure above is the Literal biting,
    # not an unrelated import/type error in the caller.
    good = tmp_path / "good_caller.py"
    good.write_text(textwrap.dedent(_CALLER.format(disposition="rejected")))
    result = _run_mypy(good)
    assert result.returncode == 0, f"mypy should accept a valid disposition:\n{result.stdout}"
