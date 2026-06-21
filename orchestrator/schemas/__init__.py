"""Frozen engine contracts (target.md §4 + §7)."""

from __future__ import annotations

from .enums import (
    LANE_STAGES,
    SCHEMA_VERSION,
    STAGE_ORDER,
    TERMINAL_TASK_STATES,
    ExecutionLane,
    ExecutionMode,
    FailureKind,
    Provider,
    ResultStatus,
    RunState,
    Stage,
    StageStatus,
    TaskState,
)
from .status import (
    Progress,
    ResumeCursor,
    Run,
    StageRecord,
    Task,
    TaskRef,
)
from .work import (
    LanePolicy,
    LaneUsed,
    StageResult,
    TokenUsage,
    WorkItem,
    compute_content_hash,
)

__all__ = [
    "SCHEMA_VERSION",
    "STAGE_ORDER",
    "LANE_STAGES",
    "TERMINAL_TASK_STATES",
    "Stage",
    "ExecutionMode",
    "ExecutionLane",
    "Provider",
    "ResultStatus",
    "RunState",
    "StageStatus",
    "TaskState",
    "FailureKind",
    "WorkItem",
    "StageResult",
    "LanePolicy",
    "LaneUsed",
    "TokenUsage",
    "compute_content_hash",
    "Run",
    "Task",
    "TaskRef",
    "StageRecord",
    "ResumeCursor",
    "Progress",
]
