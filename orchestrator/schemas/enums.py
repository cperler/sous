"""Frozen enums shared across the engine contracts (§4/§7 of target.md).

These names are load-bearing: the status-file schema, the WorkItem/StageResult
contract, and the state machine all key off them. Changing a value is a schema
change and must bump SCHEMA_VERSION.
"""

from __future__ import annotations

from enum import StrEnum

# v2: Task carries its own `pipeline` (ordered stage list); v1 docs derive it from
# execution_lane on load (2026-07-01 design pass §1).
# v3: deterministic stages (e.g. intake) run on the non-model ENGINE lane
# (ExecutionMode.ENGINE × Provider.NONE); additive — pre-v3 docs never name it.
SCHEMA_VERSION = "3"


class Stage(StrEnum):
    """The stage VOCABULARY (target.md §6.1): the dispatchable stage kinds, each with a
    StageSpec. The execution *sequence* is per-task (``Task.pipeline``) — this enum is
    not a sequence, and new node types extend the vocabulary additively."""

    INTAKE = "intake"
    SCOPE = "scope"
    IMPLEMENT = "implement"
    TEST = "test"
    DELIVER = "deliver"
    REVIEW = "review"


# The default FULL pipeline (and the canonical *display* order for stage records).
# Its ONLY sequencing duty is defining the FULL preset below — the state machine walks
# task.pipeline, never this constant.
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.INTAKE,
    Stage.SCOPE,
    Stage.IMPLEMENT,
    Stage.TEST,
    Stage.DELIVER,
    Stage.REVIEW,
)


class ExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    HEADLESS = "headless"
    ENGINE = "engine"  # deterministic, in-process, no model call (e.g. intake setup)


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    NONE = "none"  # no model provider — the ENGINE lane's deterministic runner


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    CASCADE_BLOCKED = "cascade_blocked"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    # Held at a human gate (design pass §4): NON-terminal, not dispatchable, does not
    # cascade. Exit is Engine.approve(), which writes a durable approval artifact —
    # the HARD-CHECKPOINT norm as a mechanism instead of prose.
    BLOCKED_ON_HUMAN = "blocked_on_human"
    # Human confirmed the held task is infeasible and closed it (Engine.reject(), #53):
    # a TERMINAL state distinct from FAILED. "a human decided this shouldn't be done" is
    # a deliberate close, NOT an execution failure — so status/cost retrospectives can
    # tell the two apart, and the batch circuit breaker never counts it as a failure.
    CLOSED_INFEASIBLE = "closed_infeasible"


# Terminal task states — the DAG/state machine treats these as "done".
# BLOCKED_ON_HUMAN is deliberately NOT terminal: a held task keeps its run open.
# CLOSED_INFEASIBLE IS terminal (a human closed the task): it counts for run-finalization
# and dispatchability exactly like the other terminals, but is semantically not a failure.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CASCADE_BLOCKED, TaskState.CLOSED_INFEASIBLE}
)


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # #67: every task terminal, NONE failed by execution, but at least one was deliberately
    # closed-infeasible by a human (reject → CLOSED_INFEASIBLE). Honest middle rollup: the
    # run is DONE and nothing broke (so not FAILED), but not everything shipped (so not
    # COMPLETED, which reads as "all delivered"). A terminal, non-failure end state.
    COMPLETED_WITH_REJECTIONS = "completed_with_rejections"
    FAILED = "failed"
    PAUSED = "paused"


# Terminal run states: the run has finalized and no task will move again.
TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.COMPLETED_WITH_REJECTIONS, RunState.FAILED}
)


class ResultStatus(StrEnum):
    """Outcome of one model dispatch, as reported by a runner."""

    SUCCESS = "success"
    SCHEMA_VIOLATION = "schema_violation"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"  # transient: re-dispatch on a cheaper model (graceful fallback)
    # The PROVIDER itself is unavailable (codex CLI missing / auth expired) — distinct from a
    # task FAILURE: the provider never ran the task, so retrying it in-provider is futile. The
    # engine may cross-provider-fall through to claude when the run opts in (#7); with the flag
    # off it degrades to a normal FAILURE (retry-then-fail), so pre-#7 behavior is unchanged.
    PROVIDER_UNAVAILABLE = "provider_unavailable"


class ExecutionLane(StrEnum):
    """Run modes — which collapsed stages execute (ported from full/lite/micro)."""

    FULL = "full"
    LITE = "lite"
    MICRO = "micro"


# Lane PRESETS: named pipelines (ported from full/lite/micro). A lane is resolved to a
# concrete task.pipeline at add_task; the engine sequences on the pipeline, not the lane.
LANE_STAGES: dict[ExecutionLane, tuple[Stage, ...]] = {
    ExecutionLane.FULL: STAGE_ORDER,
    ExecutionLane.LITE: (Stage.INTAKE, Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER, Stage.REVIEW),
    ExecutionLane.MICRO: (Stage.INTAKE, Stage.IMPLEMENT, Stage.DELIVER, Stage.REVIEW),
}

# Stages a preset runs on the $0 deterministic ENGINE lane BY DEFAULT (#68, promoting #33's
# opt-in): the cheaper micro/lite lanes adopt the deterministic TEST/DELIVER runners so the
# mechanical suite-run and PR-open don't each burn a model call — matching the cost-router's
# preference (cost_policy._CHEAP_DETERMINISTIC) as the standing default, not just under budget
# pressure. FULL keeps model TEST/DELIVER (it pays for the extra judgment). A pipeline that
# opts in KEEPS a model REVIEW (micro/lite do): the deterministic TEST runner never judges
# meaningfulness, so that veto still lives on a model. Resolved at add_task; an explicit
# --deterministic-stages (or a cost-routing decision) overrides it. Intersected with the
# stage set that actually runs, so MICRO (no TEST stage) gets DELIVER only.
_DETERMINISTIC_BY_DEFAULT: frozenset[Stage] = frozenset({Stage.TEST, Stage.DELIVER})
LANE_DETERMINISTIC_STAGES: dict[ExecutionLane, tuple[Stage, ...]] = {
    lane: (
        ()
        if lane is ExecutionLane.FULL
        else tuple(s for s in stages if s in _DETERMINISTIC_BY_DEFAULT)
    )
    for lane, stages in LANE_STAGES.items()
}


class FailureKind(StrEnum):
    """Failure-classifier taxonomy buckets (concrete patterns live in project-config)."""

    UNIT = "unit"
    E2E = "e2e"
    SHELL = "shell"
    INFRA = "infra"
    UNKNOWN = "unknown"
