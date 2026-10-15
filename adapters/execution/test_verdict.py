"""The project's declared test commands decide a model-lane TEST stage's verdict.

A model TEST stage is told the project's test command and still runs whatever it likes:
on prediction run ``batch-s-420-233-421-422-423`` it ran ``pytest -q`` serially instead of
the declared ``pytest -q -n auto``, a ~40-minute suite against a 20-minute stage limit,
so a task whose code was green timed out twice and failed. A louder prompt line would not
hold (CLAUDE.md: a rule the model keeps breaking wants structure, not restatement), so the
in-process model lanes (headless×claude, any×codex) hand every TEST result to
:func:`verify_test_verdict`, which runs the ENGINE lane's own evaluation
(``DeterministicTestRunner.evaluate``) over the same worktree:

- a model result that claims success is kept only if the declared commands are green —
  ``passed``/``failures`` are replaced by that run's, and a red run downgrades the stage
  to a FAILURE carrying the real failures, so a misreported green can never pass TEST;
- a model result that TIMED OUT over a clean worktree is settled by the declared run: a
  green run becomes a SUCCESS with ``tests_meaningful`` null (not judged — REVIEW's
  independent test-validate judges it, #13/#261) instead of a retry that would time out
  the same way. A dirty worktree or a red run leaves the timeout as it was;
- every other status passes through untouched.

The model's tokens, session and stream evidence stay on the result: only the verdict
fields change. Each rewrite rides an execution notice so the event log says the verdict
came from the engine's run, not the model's report.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from orchestrator.ports.project import ProjectConfig
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.schemas.work import WorkItem

from .deterministic_test import DeterministicTestRunner, TestRun
from .transport import RawResult, _git, _salvageable_commits, _tag_head

# Kept with the model's notes so the verdict's source is readable in the stage log.
_VERIFIED_PREFIX = "Engine re-ran the project's declared test commands: "


def verify_test_verdict(
    work: WorkItem, raw: RawResult, status: ResultStatus, project: ProjectConfig | None,
) -> tuple[RawResult, ResultStatus]:
    """Return ``(raw, status)`` with the TEST verdict taken from the declared commands.

    A no-op for a non-TEST stage, a missing project, a worktree that is unset or absent on
    disk, and any status other than SUCCESS / TIMEOUT."""
    if work.stage is not Stage.TEST or project is None or not work.cwd:
        return raw, status
    if not Path(work.cwd).is_dir():  # no worktree on disk: nothing to run the commands in
        return raw, status
    if status is ResultStatus.SUCCESS and isinstance(raw.structured_output, dict):
        return _check_success(work, raw, DeterministicTestRunner(project).evaluate(work))
    if status is ResultStatus.TIMEOUT and _worktree_clean(work.cwd):
        return _settle_timeout(work, raw, DeterministicTestRunner(project).evaluate(work))
    return raw, status


def _check_success(work: WorkItem, raw: RawResult, run: TestRun) -> tuple[RawResult, ResultStatus]:
    model_out = dict(raw.structured_output or {})
    run_out = run.out or {}
    # The runner's own "meaningfulness not judged" line would contradict the model's
    # judgment, which this result keeps — drop it from the merged notes.
    run_notes = "; ".join(
        part for part in str(run_out.get("validation_notes") or run.error or "").split("; ")
        if not part.startswith("tests_meaningful not judged")
    )
    model_notes = str(model_out.get("validation_notes") or "").strip()
    merged = {
        **model_out,
        "passed": run.ok,
        "failures": list(run_out.get("failures") or []),
        "validation_notes": "\n\n".join(
            n for n in (model_notes, _VERIFIED_PREFIX + run_notes) if n
        ),
        **{k: run_out[k] for k in ("test_scope", "tests_selected", "test_scope_reason")
           if k in run_out},
    }
    if run.ok:
        return replace(raw, structured_output=merged,
                       execution_notices=raw.execution_notices + run.notices), ResultStatus.SUCCESS
    notice: dict[str, object] = {
        "notice": "test_verdict_overridden",
        "reason": "declared_commands_unverifiable" if run.refused else "declared_commands_red",
        "model_passed": model_out.get("passed"),
        "failures": merged["failures"][:10],
        "detail": "the model reported the TEST stage done, but the project's declared test "
                  f"commands did not pass: {run.error or 'red'}",
    }
    # Mirror the checkpoint wrapper's failure path: no checkpoint for a failed stage, and
    # report any commits the model made so the retry can keep them (#59).
    salvage = _salvageable_commits(work.cwd, work.salvage_anchor) if work.salvage_anchor else None
    failed = replace(
        raw, structured_output=merged, exit_code=1, checkpoint=None, salvage=salvage,
        error=f"engine test verification: {run.error or 'declared test commands failed'}",
        execution_notices=raw.execution_notices + run.notices + (notice,),
    )
    return failed, ResultStatus.FAILURE


def _settle_timeout(work: WorkItem, raw: RawResult, run: TestRun) -> tuple[RawResult, ResultStatus]:
    if not run.ok:
        return raw, ResultStatus.TIMEOUT
    run_out = dict(run.out or {})
    run_out["tests_meaningful"] = None  # nobody judged it; REVIEW's test-validate will
    run_out["validation_notes"] = (
        "The model TEST stage timed out; the verdict is the engine's run of the project's "
        "declared test commands over the worktree it left (clean, every change committed). "
        "Meaningfulness was not judged at TEST. " + _VERIFIED_PREFIX
        + str(run_out.get("validation_notes") or "")
    )
    run_out.pop("change_class", None)  # an ENGINE-lane fold key; this is a model-lane result
    notice: dict[str, object] = {
        "notice": "test_timeout_settled_by_declared_commands",
        "reason": "declared_commands_green",
        "detail": "the model TEST stage timed out over a clean worktree; the project's "
                  "declared test commands passed there, so the stage succeeds on that run "
                  "instead of retrying",
    }
    checkpoint = _tag_head(work.cwd, work.checkpoint_tag) if work.checkpoint_tag and work.cwd else None
    settled = replace(
        raw, structured_output=run_out, exit_code=0, error=None, checkpoint=checkpoint,
        salvage=None, execution_notices=raw.execution_notices + run.notices + (notice,),
    )
    return settled, ResultStatus.SUCCESS


def _worktree_clean(cwd: str) -> bool:
    """True when the worktree has no uncommitted or untracked changes (gitignored files do
    not count). Fail-closed: a git error reads as dirty, so the timeout stands."""
    try:
        proc = _git(cwd, "status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and not proc.stdout.strip()
