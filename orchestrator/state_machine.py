"""Per-task state machine over the 6 collapsed stages (target.md §3 / §6.1).

Pure logic over a ``Task``: which stage runs next (honoring the execution lane's
skip-set), how to apply a ``StageResult``, and where to resume after a crash. The
``started_at``-always-present schema makes the crash marker unambiguous.
"""

from __future__ import annotations

from .schemas.enums import (
    LANE_STAGES,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
)
from .schemas.status import ResumeCursor, StageRecord, Task
from .schemas.work import StageResult


def plan_lane(task: Task) -> None:
    """Mark every stage not in the task's lane as ``skipped`` (idempotent)."""
    active = set(LANE_STAGES[task.execution_lane])
    for stage, rec in task.stages.items():
        if stage not in active and rec.status is StageStatus.PENDING:
            rec.status = StageStatus.SKIPPED


def _active_sequence(task: Task) -> tuple[Stage, ...]:
    return LANE_STAGES[task.execution_lane]


def next_stage(task: Task) -> Stage | None:
    """First in-lane stage whose status is not completed/skipped, else None."""
    plan_lane(task)
    for stage in _active_sequence(task):
        if task.stages[stage].status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
            continue
        return stage
    return None


def is_done(task: Task) -> bool:
    return next_stage(task) is None


def begin_stage(task: Task, stage: Stage, *, now: str, model: str, attempt: int = 0) -> None:
    """Mark a stage running and point the resume cursor at it.

    Clears completed_at/error from any prior attempt so a RUNNING record with a
    null completed_at is an unambiguous crash marker (resume_point relies on this).
    """
    rec = task.stages[stage]
    rec.status = StageStatus.RUNNING
    rec.started_at = now
    rec.completed_at = None
    rec.error = None
    rec.attempt = attempt
    rec.model = model
    task.current_stage = stage
    task.resume_cursor = ResumeCursor(stage=stage, hint=f"{stage.value} running (attempt {attempt})")
    task.updated_at = now


def apply_result(
    task: Task,
    result: StageResult,
    *,
    now: str,
    cost_usd: float | None,
    iteration: int | None = None,
) -> None:
    """Fold a StageResult into the task's stage record (status + attribution)."""
    rec: StageRecord = task.stages[result.stage]
    rec.completed_at = now
    rec.model = result.model
    rec.provider = result.lane_used.provider
    rec.lane = result.lane_used.execution_mode
    rec.cost_usd = cost_usd
    rec.input_tokens = result.token_usage.input
    rec.output_tokens = result.token_usage.output
    rec.output = result.structured_output
    rec.attempt = result.attempt
    if iteration is not None:
        rec.iteration = iteration

    if result.status is ResultStatus.SUCCESS:
        rec.status = StageStatus.COMPLETED
        rec.error = None
        _absorb_outputs(task, result)
    else:
        rec.status = StageStatus.FAILED
        rec.error = result.error or result.status.value
        task.last_error = rec.error

    task.current_stage = result.stage
    task.updated_at = now


def _absorb_outputs(task: Task, result: StageResult) -> None:
    """Lift well-known fields out of a stage's structured output onto the task."""
    out = result.structured_output or {}
    if result.stage is Stage.DELIVER:
        if "pr_number" in out:
            task.pr_number = out.get("pr_number")
        if "pr_url" in out:
            task.pr_url = out.get("pr_url")


def resume_point(task: Task) -> Stage | None:
    """Where to re-enter after a crash.

    A stage left ``running`` (started_at set, completed_at null) is a crash marker —
    re-run it. Otherwise resume at the first non-completed/skipped stage.
    """
    plan_lane(task)
    for stage in _active_sequence(task):
        rec = task.stages[stage]
        if rec.status is StageStatus.RUNNING and rec.started_at and not rec.completed_at:
            return stage
        if rec.status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
            continue
        return stage
    return None


def stage_lane_used(result: StageResult) -> tuple[ExecutionMode, Provider]:
    return (result.lane_used.execution_mode, result.lane_used.provider)
