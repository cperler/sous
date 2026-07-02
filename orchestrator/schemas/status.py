"""Versioned status-file schema (target.md §7): the resumability contract.

Normalized run / task / stage records. Task status is the single source of truth;
run-level ``progress`` counters are DERIVED, not stored. Every stage record has the
same shape with ``started_at`` ALWAYS present (null until running) — fixing the
as-built writer omission. model/provider/cost/tokens live on the stage record, so
cost is traceable to the exact stage+model (closes D6 at the schema level).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    LANE_STAGES,
    SCHEMA_VERSION,
    TERMINAL_TASK_STATES,
    ExecutionLane,
    ExecutionMode,
    Provider,
    RunState,
    Stage,
    StageStatus,
    TaskState,
)


class StageRecord(BaseModel):
    """One stage's record inside a task document. Uniform shape — no omissions."""

    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None  # ALWAYS present; null until the stage runs
    completed_at: str | None = None
    attempt: int = 0
    model: str | None = None
    provider: Provider | None = None
    lane: ExecutionMode | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    output: dict | None = None  # stage-specific payload
    iteration: int | None = None  # adaptive loops (implement/test/review)


class ResumeCursor(BaseModel):
    stage: Stage
    hint: str = ""


def _new_stage_map() -> dict[Stage, StageRecord]:
    # Keyed by the FULL vocabulary (not the task's pipeline): off-pipeline stages sit at
    # SKIPPED, keeping the doc shape uniform across tasks (2026-07-01 design pass §1).
    return {s: StageRecord() for s in Stage}


class Task(BaseModel):
    """Per-task document (status-<run>-<task>.json)."""

    model_config = ConfigDict(use_enum_values=False)

    schema_version: str = SCHEMA_VERSION
    document_type: str = "task"
    task_id: str
    run_id: str
    created_at: str
    updated_at: str
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    max_attempts: int = 3
    title: str = ""
    body: str = ""  # task-source description (e.g. the GitHub issue body) — feeds prompts
    provider_tag: str | None = None  # e.g. "codex" (the per-task :codex routing tag)
    issue_number: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    # Provenance: the lane preset this task was added under. Sequencing reads
    # ``pipeline``, never this field (kept for rendering/audit — design pass §1 Q1).
    execution_lane: ExecutionLane = ExecutionLane.FULL
    # The ordered stage list THIS task runs (schema v2). Set once at add_task and
    # immutable in spirit thereafter (mutating it mid-run would invalidate the resume
    # cursor's meaning). v1 docs without it derive it from execution_lane on load.
    pipeline: tuple[Stage, ...] = ()
    pr_number: int | None = None
    pr_url: str | None = None
    # Engine-owned task context plane: well-known fields folded out of each stage's
    # structured_output (bounded + injective per the 2026-07-01 context-plane design
    # note) and threaded into downstream prompts. Derived only from durable StageResults,
    # so it is reconstructible on replay — correctness never depends on it.
    context: dict = Field(default_factory=dict)
    current_stage: Stage | None = None
    stage_counter: int = 0  # monotonic count of recorded stage executions (log sequence)
    stages: dict[Stage, StageRecord] = Field(default_factory=_new_stage_map)
    resume_cursor: ResumeCursor | None = None
    error_signatures: list[str] = Field(default_factory=list)
    learnings: list[str] = Field(default_factory=list)  # appended per failed attempt
    last_error: str | None = None
    # The WorkItem currently dispatched for this task (validates the returned result).
    pending_work_item_id: str | None = None
    pending_content_hash: str | None = None
    # Set when a rate-limited dispatch re-queues the current stage on a cheaper model;
    # consumed by the next next_work() for this stage (graceful fallback).
    pending_fallback_model: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_pipeline(cls, data: object) -> object:
        """v1→v2 migration + construction default: a task without an explicit pipeline
        gets its lane preset. Lives on the model (not the store) so every load path —
        store, tests, hand-built docs — derives identically."""
        if isinstance(data, dict) and not data.get("pipeline"):
            lane = data.get("execution_lane") or ExecutionLane.FULL
            data["pipeline"] = LANE_STAGES[ExecutionLane(lane)]
        return data

    @model_validator(mode="after")
    def _validate_pipeline(self) -> Task:
        if not self.pipeline:
            raise ValueError("task pipeline must be non-empty")
        if len(set(self.pipeline)) != len(self.pipeline):
            raise ValueError(f"task pipeline has duplicate stages: {self.pipeline}")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TASK_STATES


class TaskRef(BaseModel):
    """Lightweight pointer in the run doc; ``state`` is an explicit derived cache."""

    task_id: str
    status_file: str
    state: TaskState = TaskState.PENDING


class Progress(BaseModel):
    """Derived view over task states — never stored."""

    total: int = 0
    pending: int = 0
    running: int = 0
    blocked: int = 0
    cascade_blocked: int = 0
    retrying: int = 0
    completed: int = 0
    failed: int = 0
    blocked_on_human: int = 0


class Run(BaseModel):
    """Run/batch document (status-<run>.json)."""

    model_config = ConfigDict(use_enum_values=False)

    schema_version: str = SCHEMA_VERSION
    document_type: str = "run"
    run_id: str
    created_at: str
    updated_at: str
    state: RunState = RunState.PENDING
    lane: ExecutionLane = ExecutionLane.FULL
    task_refs: list[TaskRef] = Field(default_factory=list)
    dependency_graph: dict[str, list[str]] = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)

    def progress(self) -> Progress:
        """Aggregate counters derived from task_refs (single source of truth)."""

        p = Progress(total=len(self.task_refs))
        bucket = {
            TaskState.PENDING: "pending",
            TaskState.RUNNING: "running",
            TaskState.BLOCKED: "blocked",
            TaskState.CASCADE_BLOCKED: "cascade_blocked",
            TaskState.RETRYING: "retrying",
            TaskState.COMPLETED: "completed",
            TaskState.FAILED: "failed",
            TaskState.BLOCKED_ON_HUMAN: "blocked_on_human",
        }
        for ref in self.task_refs:
            field = bucket[ref.state]
            setattr(p, field, getattr(p, field) + 1)
        return p
