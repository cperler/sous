"""Frozen enums shared across the engine contracts (§4/§7 of target.md).

These names are load-bearing: the status-file schema, the WorkItem/StageResult
contract, and the state machine all key off them. Changing a value is a schema
change and must bump SCHEMA_VERSION.
"""

from __future__ import annotations

from enum import StrEnum

# v2: Task carries its own `pipeline` (ordered stage list); v1 docs derive it from
# execution_lane on load (2026-07-01 design pass §1).
SCHEMA_VERSION = "2"


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


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


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


# Terminal task states — the DAG/state machine treats these as "done".
# BLOCKED_ON_HUMAN is deliberately NOT terminal: a held task keeps its run open.
TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CASCADE_BLOCKED}
)


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


class ResultStatus(StrEnum):
    """Outcome of one model dispatch, as reported by a runner."""

    SUCCESS = "success"
    SCHEMA_VIOLATION = "schema_violation"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"  # transient: re-dispatch on a cheaper model (graceful fallback)


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


class FailureKind(StrEnum):
    """Failure-classifier taxonomy buckets (concrete patterns live in project-config)."""

    UNIT = "unit"
    E2E = "e2e"
    SHELL = "shell"
    INFRA = "infra"
    UNKNOWN = "unknown"
