"""Per-task state machine over the task's own pipeline (target.md §3 / §6.1).

Pure logic over a ``Task``: which stage runs next (walking ``task.pipeline`` — the
stage enum is a vocabulary, not a sequence; 2026-07-01 design pass §1), how to apply
a ``StageResult``, and where to resume after a crash. The ``started_at``-always-present
schema makes the crash marker unambiguous.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .schemas.enums import (
    STAGE_ORDER,
    Effort,
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


def begin_stage(
    task: Task,
    stage: Stage,
    *,
    now: str,
    model: str,
    attempt: int = 0,
    effort: Effort | None = None,
) -> None:
    """Mark a stage running and point the resume cursor at it.

    Clears completed_at/error from any prior attempt so a RUNNING record with a
    null completed_at is an unambiguous crash marker (resume_point relies on this).
    Also clears the task-level ``last_error`` (#213) so a stale prior-attempt error
    can't leak into a later reader across a review-fix cycle.

    Stamps ``effort`` (#96/#138/#139) alongside ``model`` so the dispatched reasoning
    effort is durable on the stage record even before the result returns — a crash
    between dispatch and result, or an abandoned dispatch (which has no runner-echoed
    StageResult), still attributes the effort the stage was running at.
    """
    rec = task.stages[stage]
    rec.status = StageStatus.RUNNING
    rec.started_at = now
    rec.completed_at = None
    rec.error = None
    # #213/#184: clear the TASK-level last_error at attempt start too. apply_result sets
    # task.last_error on a FAILED result and _apply_review_rejection sets it when a review
    # cycle parks the task; nothing cleared it, so across a review-fix cycle a later reader
    # (e.g. the BLOCKED_ON_HUMAN notification reason) could surface a stale prior error.
    # Resetting here — the start of every fresh stage attempt — kills that class at the
    # source; apply_result re-sets it if THIS attempt fails.
    task.last_error = None
    rec.attempt = attempt
    rec.model = model
    # #172 assignment convention: validate_assignment on the status models coerces a
    # bare "high" to Effort.HIGH right here (and rejects an invalid string) — no
    # explicit Effort(...) wrap needed. mypy sees only the declared Effort field type
    # and can't model the runtime coercion, so the str arm needs a targeted ignore.
    rec.effort = effort  # type: ignore[assignment]
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
) -> list[dict[str, str]]:
    """Fold a StageResult into the task's stage record (status + attribution).

    Returns the pr_* drop notices produced by ``_absorb_outputs`` (empty on the
    non-SUCCESS branch, which folds nothing) so the engine can emit a warning-grade
    ``pr_field_dropped`` audit event — the fold itself stays a pure, sink-free function
    and the caller owns the I/O (#201, honouring the 'never silent' convention)."""
    rec: StageRecord = task.stages[result.stage]
    rec.completed_at = now
    rec.model = result.model
    # #139: fold the ran-at effort, mirroring model; the #172 assignment convention
    # (validate_assignment) coerces the result's bare string to the Effort enum. mypy
    # can't model that runtime coercion, so the str→Effort write needs a targeted ignore.
    rec.effort = result.effort  # type: ignore[assignment]
    rec.provider = result.lane_used.provider
    rec.lane = result.lane_used.execution_mode
    rec.cost_usd = cost_usd
    rec.input_tokens = result.token_usage.input
    rec.output_tokens = result.token_usage.output
    rec.output = result.structured_output
    rec.attempt = result.attempt
    if iteration is not None:
        rec.iteration = iteration

    pr_field_notices: list[dict[str, str]] = []
    if result.status is ResultStatus.SUCCESS:
        rec.status = StageStatus.COMPLETED
        rec.error = None
        pr_field_notices = _absorb_outputs(task, result)
    else:
        rec.status = StageStatus.FAILED
        rec.error = result.error or result.status.value
        task.last_error = rec.error

    task.current_stage = result.stage
    task.updated_at = now
    return pr_field_notices


# Engine-owned context-fold whitelist (2026-07-01 context-plane design note). Per stage,
# the generic stage-contract keys folded into task.context for downstream prompts. Only
# these keys (present in the canonical schemas/stages/*.json) are folded — never whole
# blobs — so nothing project-specific leaks into the engine's context. The map is
# INJECTIVE across stages (no two stages write the same context key); enforced by test.
CONTEXT_KEYS: dict[Stage, tuple[str, ...]] = {
    Stage.INTAKE: ("branch", "worktree", "base_sha", "baseline_failures",
                   "port_base", "port_count", "composed_deps"),
    Stage.SCOPE: ("plan", "blocked_reason"),
    Stage.IMPLEMENT: ("files_changed", "summary"),
    Stage.TEST: ("failures", "tests_meaningful", "validation_notes", "change_class"),
    Stage.DELIVER: ("pr_number", "pr_url"),
    Stage.REVIEW: ("issues",),
}

# Engine-INJECTED context keys (#72): folded into task.context by the engine directly
# (not from a stage's structured output), so they are EXEMPT from the injective stage-write
# map above and rendered/framed separately. ``prior_learnings`` is the cross-run KB recall
# the engine folds at intake. Advisory ("may or may not apply"), so it is shed FIRST when
# the context ceiling is exceeded — durable stage-derived context always outranks it.
ENGINE_INJECTED_KEYS: tuple[str, ...] = ("prior_learnings",)

# Context keys only a DETERMINISTIC ENGINE-lane runner is trusted to fold (#41): the
# docs-only change tag gates downstream effort (lighter TEST, relaxed REVIEW criteria, no
# missing-tests rejection), so a MODEL claiming ``change_class: docs-only`` must be ignored —
# only a git-diff by the engine lane may set it. A non-ENGINE result carrying such a key has
# it dropped at the fold, closing the loophole at the single choke point.
DETERMINISTIC_ONLY_KEYS: frozenset[str] = frozenset({"change_class"})

# Bounds so the context (fed into every later prompt) stays bounded regardless of what a
# model returns. Deterministic (no wall-clock/random) → replay reproduces the same fold.
_MAX_STR = 2000  # a single string value
_MAX_ITEM_STR = 500  # a string element inside a list value
_MAX_LIST = 40  # elements kept from a list value
_MAX_CONTEXT_BYTES = 16_384  # whole-context ceiling
_MAX_DROPPED_VALUE = 200  # a dropped pr_* value / its reason, in a drop notice (#201)


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
    order within that stage's ``CONTEXT_KEYS`` tuple — downstream stages need the earliest
    stages' context most. Deterministic: only json byte-lengths and the fixed enum/tuple
    order decide, never context insertion order."""
    if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
        return

    # Advisory engine-injected context (prior_learnings, #72) is the first to shed under
    # pressure: it "may or may not apply", so durable stage-derived context outranks it.
    for key in ENGINE_INJECTED_KEYS:
        if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
            return
        task.context.pop(key, None)

    # A key's weight (json bytes of ``{key: value}``) depends only on that key's own value,
    # never on what else is in the context, so it is STABLE across evictions. Compute each
    # weight once and settle the eviction order up front, instead of re-serializing every
    # candidate on every pass — that was ~O(stages^2) serializations (#26). The candidate
    # list is built reverse-pipeline then intra-tuple, and ``list.sort`` is stable and keeps
    # that order for equal weights, so heaviest-first eviction breaks weight-ties by the
    # latest-pipeline stage's key first, then the first key in that stage's CONTEXT_KEYS
    # tuple — byte-for-byte the same order the old max()-per-pass produced.
    ordered = [
        key
        for stage in reversed(STAGE_ORDER)
        for key in CONTEXT_KEYS[stage]
        if key in task.context
    ]
    ordered.sort(key=lambda key: _context_bytes({key: task.context[key]}), reverse=True)

    for key in ordered:
        if _context_bytes(task.context) <= _MAX_CONTEXT_BYTES:
            return
        task.context.pop(key, None)


def _bound_dropped_value(value: object) -> str:
    """Bound a dropped pr_* value to a fixed-length repr for the drop notice, so an
    oversized/adversarial model value can't bloat the emitted audit event. ``repr`` keeps
    the type visible (``''`` vs ``0`` vs ``None``) and is deterministic for the scalars a
    pr_* field carries."""
    text = repr(value)
    if len(text) > _MAX_DROPPED_VALUE:
        return text[:_MAX_DROPPED_VALUE] + " … [truncated]"
    return text


def _short_validation_reason(exc: ValidationError) -> str:
    """A short, deterministic reason string from a field-validation failure (first error's
    message, bounded). pydantic error messages are stable, so this replays identically."""
    errors = exc.errors()
    reason = str(errors[0]["msg"]) if errors else "validation failed"
    if len(reason) > _MAX_DROPPED_VALUE:
        return reason[:_MAX_DROPPED_VALUE] + " … [truncated]"
    return reason


def _absorb_outputs(task: Task, result: StageResult) -> list[dict[str, str]]:
    """Fold a stage's well-known structured-output fields into task.pr_* and the
    engine-owned task.context plane (2026-07-01 design note). Fold is tolerant (a
    missing whitelisted key is skipped; a pr_* value that fails field validation is
    dropped rather than raised or stored, #172) and idempotent (a stage succeeds once;
    re-folding the same result yields the same values).

    Returns a list of drop notices (one ``{field, value, reason}`` dict per dropped pr_*
    value, empty when nothing was dropped) so the caller can surface each drop as an audit
    event instead of losing it silently (#201). The fold stays pure — it emits nothing
    itself."""
    out = result.structured_output or {}
    dropped: list[dict[str, str]] = []
    # Dedicated pr_* fields stay: other consumers read them (_on_task_completed, status()).
    # Tolerant here too, per the #172 assignment convention: validate_assignment rejects a
    # malformed model-produced value (e.g. pr_number="") AT this write — skip it rather
    # than crash record(). Before #172 the garbage landed silently and made the stored doc
    # unloadable on its next read; dropping the bad value is strictly safer. #201: record
    # each drop so the engine can emit a warning-grade event (drop is no longer invisible).
    if result.stage is Stage.DELIVER:
        for field in ("pr_number", "pr_url"):
            if field in out:
                try:
                    setattr(task, field, out[field])
                except ValidationError as exc:
                    dropped.append({
                        "field": field,
                        "value": _bound_dropped_value(out[field]),
                        "reason": _short_validation_reason(exc),
                    })
    # Generalized fold: every whitelisted key present in the result, bounded.
    engine_lane = result.lane_used.execution_mode is ExecutionMode.ENGINE
    for key in CONTEXT_KEYS.get(result.stage, ()):
        if key not in out:
            continue
        # #41 loophole guard: a deterministic-only key (change_class) folds ONLY from the
        # ENGINE lane — a model result claiming it is ignored (dropped here).
        if key in DETERMINISTIC_ONLY_KEYS and not engine_lane:
            continue
        task.context[key] = _cap_value(out[key])
    _enforce_context_ceiling(task)
    return dropped


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
