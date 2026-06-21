"""Frozen enums shared across the engine contracts (§4/§7 of target.md).

These names are load-bearing: the status-file schema, the WorkItem/StageResult
contract, and the state machine all key off them. Changing a value is a schema
change and must bump SCHEMA_VERSION.
"""

from __future__ import annotations

from enum import StrEnum

SCHEMA_VERSION = "1"


class Stage(StrEnum):
    """The collapsed 6-stage pipeline (target.md §6.1)."""

    INTAKE = "intake"
    SCOPE = "scope"
    IMPLEMENT = "implement"
    TEST = "test"
    DELIVER = "deliver"
    REVIEW = "review"


# Canonical execution order. The state machine advances through this list.
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


# Terminal task states — the DAG/state machine treats these as "done".
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


class ExecutionLane(StrEnum):
    """Run modes — which collapsed stages execute (ported from full/lite/micro)."""

    FULL = "full"
    LITE = "lite"
    MICRO = "micro"


# Which stages run per lane (mode machinery, target.md §6.1).
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
