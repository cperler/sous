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
    # Stages THIS task runs on the deterministic ENGINE lane (no model call) IN ADDITION
    # to the stages globally marked deterministic in their StageSpec (intake). This is the
    # per-task/per-lane knob that lets a pipeline opt TEST/DELIVER into the $0 shell runners
    # (#33) without flipping the global default for every project — the SAME routing
    # decision (→ ENGINE lane) as intake, just sourced from the task (mirrors how the
    # pipeline itself became per-task in schema v2). Empty by default: the stock full/lite/
    # micro presets keep model TEST/DELIVER.
    deterministic_stages: tuple[Stage, ...] = ()
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
    # Count of review-rejection fix cycles taken (review gate): each rejection that
    # re-opens implement→…→review increments this; at the engine's max_review_cycles
    # the task parks BLOCKED_ON_HUMAN instead of looping forever.
    review_cycles: int = 0
    # Fingerprints (file:description, normalized) of the LAST rejection's blocking
    # issues — the convergence key (#15): a re-review whose issues are a subset of
    # these (no net-new findings, none critical) has converged and auto-approves.
    last_review_rejection: list[str] = Field(default_factory=list)
    last_error: str | None = None
    # The WorkItem currently dispatched for this task (validates the returned result).
    pending_work_item_id: str | None = None
    pending_content_hash: str | None = None
    # Set when a rate-limited dispatch re-queues the current stage on a cheaper model;
    # consumed by the next next_work() for this stage (graceful fallback).
    pending_fallback_model: str | None = None
    # Rate-limit cooldown (the wait-out-the-window half of the old handle_rate_limit):
    # a floor-of-chain rate limit parks the task until this ISO timestamp instead of
    # burning attempts; next_work/dispatchable refuse earlier dispatch. Bounded by
    # rate_limit_waits vs the engine's max_rate_limit_waits.
    not_before: str | None = None
    rate_limit_waits: int = 0
    # Infra-failure reset loop (#14): how many environment resets this task has spent
    # re-running an infra-classified failure's attempt (vs the engine's max_infra_resets).
    infra_resets: int = 0
    # Salvage loop (#59): how many times the current stage's committed work was KEPT in
    # place across a salvageable failure (timeout/infra/rate-limit) instead of being reset
    # to the checkpoint. Bounded by the engine's max_salvage_keeps — past it, a repeat
    # salvageable failure resets fully (a salvaged pile that isn't converging is discarded,
    # never an infinite heap of half-work). Refreshed to 0 by a clean stage, like the
    # infra_resets / rate_limit_waits budgets.
    salvage_count: int = 0
    # Set by failure handling when the last attempt's COMMITTED work is being kept for the
    # retry (#59); consumed by next_work to SUPPRESS the pre-dispatch checkpoint reset so
    # the retry inherits the work. Transient — cleared when the dispatch is committed.
    salvage_in_place: bool = False
    # Provider session chaining (design pass §2): set from a SUCCESSFUL StageResult's
    # session_ref, cleared on failure (a failed attempt's context is as likely poisoned
    # as useful — warm retry is deliberately OFF; the eval bench can revisit). Routing
    # metadata only: correctness never depends on it.
    session_ref: str | None = None
    # Which provider OWNS the current session_ref (#9): a session id is provider-specific —
    # a claude conversation id means nothing to `codex exec resume` and vice versa. next_work
    # only chains session_ref into a stage whose lane provider matches (Provider.NONE, from a
    # deterministic ENGINE-lane stage that never mints a real session, is treated as an
    # untagged wildcard), so a claude ref is never fed to codex or the reverse. Set alongside
    # session_ref on SUCCESS; cleared with it on failure/reject.
    session_provider: Provider | None = None
    # Last successful stage checkpoint {"tag", "sha"} (design pass §3): the reset
    # anchor for a retry/crash-resume of a git-affecting stage. Absorbed from
    # StageResult.checkpoint on SUCCESS only, so a failed/vetoed attempt's commits
    # never become an anchor.
    last_checkpoint: dict | None = None

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
    closed_infeasible: int = 0


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
    # Per-run cost budget (#34). A soft warning fires once at budget_soft_fraction of
    # metered spend; a hard stop PAUSES the run (reusing the PAUSED/unpause machinery)
    # at/after the budget. None = no budget (default off). ONLY metered rows count —
    # unmetered interactive rows are $0 anyway. Additive fields: pre-#34 run docs load
    # with the defaults, so no SCHEMA_VERSION bump is needed.
    budget_usd: float | None = None
    # Once-only dedupe for the soft budget warning (mirrors the stale-alert dedupe): set
    # True when the warning fires, reset by `unpause --raise-budget` so a new ceiling
    # re-arms it.
    budget_warning_sent: bool = False
    # Cost-aware lane routing (#34): when True, add_task routes an un-pinned task to a
    # cheaper lane preset as the remaining budget thins (deterministic band table).
    # Explicit per-task pipeline pins are always honored. Default off.
    route_by_cost: bool = False
    # Capacity-aware model downgrade (#12): when True, next_work drops a FRESH dispatch to
    # a cheaper model while CURRENT utilization is in the high band (>= downgrade_threshold
    # but < the per-call gate that already blocks dispatch) — the old "run cheaper under
    # load" behavior. DISTINCT from route_by_cost (capacity headroom, not USD). A rate-limit
    # re-queue and a fallback-disallowing lane are never downgraded. Additive field: pre-#12
    # run docs load with the default. Default off.
    route_by_capacity: bool = False
    # Mid-run progress commentary (#64): when True, the engine upserts a living progress
    # comment/PR-body section on the driving issue/PR at each stage boundary (throttled),
    # so a human can follow a long run from GitHub. Outward-facing, so default OFF — a run
    # against a real repo only posts when explicitly opted in. Additive field: pre-#64 run
    # docs load with the default, no SCHEMA_VERSION bump.
    progress_comments: bool = False

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
            TaskState.CLOSED_INFEASIBLE: "closed_infeasible",
        }
        for ref in self.task_refs:
            field = bucket[ref.state]
            setattr(p, field, getattr(p, field) + 1)
        return p
