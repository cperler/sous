"""Per-task state machine over the task's own pipeline (target.md §3 / §6.1).

Pure logic over a ``Task``: which stage runs next (walking ``task.pipeline`` — the
stage enum is a vocabulary, not a sequence; 2026-07-01 design pass §1), how to apply
a ``StageResult``, and where to resume after a crash. The ``started_at``-always-present
schema makes the crash marker unambiguous.
"""

from __future__ import annotations

import json

from .schemas.enums import (
    STAGE_ORDER,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
)
from .schemas.status import ResumeCursor, StageRecord, Task
from .schemas.work import StageResult


def plan_pipeline(task: Task) -> None:
    """Mark every vocabulary stage not in the task's pipeline as ``skipped`` (idempotent)."""
    active = set(task.pipeline)
    for stage, rec in task.stages.items():
        if stage not in active and rec.status is StageStatus.PENDING:
            rec.status = StageStatus.SKIPPED


def _active_sequence(task: Task) -> tuple[Stage, ...]:
    return tuple(task.pipeline)


def next_stage(task: Task) -> Stage | None:
    """First in-pipeline stage whose status is not completed/skipped, else None."""
    plan_pipeline(task)
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


# Engine-owned context-fold whitelist (2026-07-01 context-plane design note). Per stage,
# the generic stage-contract keys folded into task.context for downstream prompts. Only
# these keys (present in the canonical schemas/stages/*.json) are folded — never whole
# blobs — so nothing project-specific leaks into the engine's context. The map is
# INJECTIVE across stages (no two stages write the same context key); enforced by test.
CONTEXT_KEYS: dict[Stage, tuple[str, ...]] = {
    Stage.INTAKE: ("branch", "worktree", "baseline_failures"),
    Stage.SCOPE: ("plan", "blocked_reason"),
    Stage.IMPLEMENT: ("files_changed", "summary"),
    Stage.TEST: ("failures", "tests_meaningful", "validation_notes"),
    Stage.DELIVER: ("pr_number", "pr_url"),
    Stage.REVIEW: ("issues",),
}

# Bounds so the context (fed into every later prompt) stays bounded regardless of what a
# model returns. Deterministic (no wall-clock/random) → replay reproduces the same fold.
_MAX_STR = 2000  # a single string value
_MAX_ITEM_STR = 500  # a string element inside a list value
_MAX_LIST = 40  # elements kept from a list value
_MAX_CONTEXT_BYTES = 16_384  # whole-context ceiling


def _cap_item(x: object) -> object:
    if isinstance(x, str) and len(x) > _MAX_ITEM_STR:
        return x[:_MAX_ITEM_STR] + " … [truncated]"
    return x


def _cap_value(v: object) -> object:
    """Bound one folded value (string/list); scalars pass through unchanged."""
    if isinstance(v, str):
        return v[:_MAX_STR] + " … [truncated]" if len(v) > _MAX_STR else v
    if isinstance(v, list):
        capped = [_cap_item(x) for x in v[:_MAX_LIST]]
        if len(v) > _MAX_LIST:
            capped.append(f"… ({len(v) - _MAX_LIST} more)")
        return capped
    return v  # bool / int / float / None


def _context_bytes(context: dict) -> int:
    return len(json.dumps(context, default=str, ensure_ascii=False).encode("utf-8"))


def _enforce_context_ceiling(task: Task) -> None:
    """Keep task.context under the whole-context ceiling by a per-KEY size-weighted
    sweep, heaviest-first: each pass evicts the single folded key that weighs the most
    bytes, so a fat key is shed while its small stage-siblings survive (dropping a
    near-ceiling ``test.failures`` no longer takes ``tests_meaningful`` /
    ``validation_notes`` down with it — whole-stage eviction was needlessly coarse).
    Ties break reverse-pipeline (review's keys first, intake's last) then the fixed key
    order within each stage's ``CONTEXT_KEYS`` — downstream stages need the earliest
    stages' context most. Deterministic: only json byte-lengths and the fixed enum/tuple
    order decide, never context insertion order."""
    if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
        return

    def _weight(key: str) -> int:
        return _context_bytes({key: task.context[key]})

    while _context_bytes(task.context) > _MAX_CONTEXT_BYTES:
        # context keys present, ordered so max() breaks weight-ties by
        # evicting the latest-pipeline stage's key first, then the first key in its tuple.
        candidates = [
            key
            for stage in reversed(STAGE_ORDER)
            for key in CONTEXT_KEYS[stage]
            if key in task.context
        ]
        if not candidates:
            return

        task.context.pop(max(candidates, key=_weight), None)


def _absorb_outputs(task: Task, result: StageResult) -> None:
    """Fold a stage's well-known structured-output fields into task.pr_* and the
    engine-owned task.context plane (2026-07-01 design note). Fold is tolerant (a
    missing whitelisted key is skipped) and idempotent (a stage succeeds once; re-folding
    the same result yields the same values)."""
    out = result.structured_output or {}
    # Dedicated pr_* fields stay: other consumers read them (_on_task_completed, status()).
    if result.stage is Stage.DELIVER:
        if "pr_number" in out:
            task.pr_number = out.get("pr_number")
        if "pr_url" in out:
            task.pr_url = out.get("pr_url")
    # Generalized fold: every whitelisted key present in the result, bounded.
    for key in CONTEXT_KEYS.get(result.stage, ()):
        if key in out:
            task.context[key] = _cap_value(out[key])
    _enforce_context_ceiling(task)


def reset_for_fix_cycle(task: Task, from_stage: Stage) -> list[Stage]:
    """Re-open the tail of the pipeline for a review-rejection fix cycle: every
    pipeline stage at/after ``from_stage`` gets a fresh PENDING record, so
    ``next_stage`` returns ``from_stage`` and the fix re-runs implement→…→review.

    History is not lost — every prior execution is already durable in the per-stage
    logs (``write_stage_log``); the stage RECORD is working state, not the audit trail.
    Returns the stages that were reset (empty when ``from_stage`` is not in the
    pipeline — the caller must handle that as "no fix cycle possible")."""
    if from_stage not in task.pipeline:
        return []
    idx = task.pipeline.index(from_stage)
    reset = list(task.pipeline[idx:])
    for stage in reset:
        task.stages[stage] = StageRecord()
    task.resume_cursor = ResumeCursor(
        stage=from_stage, hint=f"review fix cycle: re-running from {from_stage.value}"
    )
    return reset


def resume_point(task: Task) -> Stage | None:
    """Where to re-enter after a crash.

    A stage left ``running`` (started_at set, completed_at null) is a crash marker —
    re-run it. Otherwise resume at the first non-completed/skipped stage.
    """
    plan_pipeline(task)
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
