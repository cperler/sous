"""Versioned status-file schema (target.md §7): the resumability contract.

Normalized run / task / stage records. Task status is the single source of truth;
run-level ``progress`` counters are DERIVED, not stored. Every stage record has the
same shape with ``started_at`` ALWAYS present (null until running) — fixing the
as-built writer omission. model/provider/cost/tokens live on the stage record, so
cost is traceable to the exact stage+model (closes D6 at the schema level).

All models here inherit ``_StatusModel``, the typed-field assignment convention
(#172): enum-typed fields coerce bare strings at assignment time and reject invalid
ones at the write site. See its docstring for the migration recipe.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    LANE_STAGES,
    SCHEMA_VERSION,
    TERMINAL_TASK_STATES,
    Effort,
    ExecutionLane,
    ExecutionMode,
    Provider,
    RunState,
    Stage,
    StageStatus,
    TaskState,
)


class _StatusModel(BaseModel):
    """Shared base for every status-document model: the typed-field assignment
    convention (#172).

    ``validate_assignment=True`` makes enum-typed fields self-coercing at ASSIGNMENT
    time, not just on load: ``rec.effort = "high"`` stores ``Effort.HIGH`` and an
    invalid string raises immediately at the write site. Use sites therefore never
    need ad-hoc ``Effort(...)`` wraps or ``.value`` extractions — the #147
    (StageRecord.effort) and #161 (Task.effort_pin) migrations both grew those
    because assignment was lax. ``use_enum_values=False`` keeps the enum instance in
    memory, while StrEnum serialization keeps the stored JSON byte-identical (a pin
    still persists as ``"low"``).

    Convention for future open-vocabulary-string -> enum migrations (the design note
    #172 asks for) — each step is mechanical:

    1. Give the vocabulary a ``StrEnum`` in ``schemas/enums.py`` whose values are
       exactly today's stored strings.
    2. Retype the field here from ``str`` to the enum. Loads coerce stored strings;
       this base's ``validate_assignment`` coerces writes — so no ``.value`` calls
       or explicit ``Enum(...)`` wraps are needed at any use site (StrEnum members
       still compare and format as their string value).
    3. Leave serialization alone: a StrEnum dumps as its value, so stored docs and
       events stay byte-identical and no SCHEMA_VERSION bump is needed.
    4. Pin it with a regression test: string assignment coerces, an invalid string
       raises at assignment, and the JSON round-trip is unchanged (see
       tests/test_status_assignment.py).
    """

    model_config = ConfigDict(use_enum_values=False, validate_assignment=True)


class StageRecord(_StatusModel):
    """One stage's record inside a task document. Uniform shape — no omissions."""

    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None  # ALWAYS present; null until the stage runs
    completed_at: str | None = None
    attempt: int = 0
    model: str | None = None
    # Reasoning effort this stage was dispatched at (#96/#138/#139): an Effort value
    # ("low"/"medium"/"high"), the effort sibling of ``model``. Stamped at begin_stage
    # (the dispatched value) and folded from the result in apply_result, exactly like
    # ``model``, so it is durable before the result returns — a crash-before-result or an
    # abandoned dispatch (which has no runner-echoed StageResult) still attributes the
    # effort the stage ran at, and its cost-ledger row stays symmetric with model. Pure
    # audit: durable per-stage effort attribution alongside model/provider/cost. None on
    # effort-less dispatches (a spec without a default) and on deterministic ENGINE-lane
    # stages (no model, no effort). Additive field: pre-#138/#139 records load with
    # effort=None, so no SCHEMA_VERSION bump. Typed as the Effort enum (#147) — the sibling
    # attribution fields provider/lane already hold their enums; a stored "high" coerces to
    # Effort.HIGH on load, and the StrEnum still serializes/compares as its "high" value.
    effort: Effort | None = None
    provider: Provider | None = None
    lane: ExecutionMode | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    output: dict | None = None  # stage-specific payload
    iteration: int | None = None  # adaptive loops (implement/test/review)


class ResumeCursor(_StatusModel):
    stage: Stage
    hint: str = ""


def _new_stage_map() -> dict[Stage, StageRecord]:
    # Keyed by the FULL vocabulary (not the task's pipeline): off-pipeline stages sit at
    # SKIPPED, keeping the doc shape uniform across tasks (2026-07-01 design pass §1).
    return {s: StageRecord() for s in Stage}


class Task(_StatusModel):
    """Per-task document (status-<run>-<task>.json)."""

    schema_version: str = SCHEMA_VERSION
    document_type: str = "task"
    task_id: str
    run_id: str
    created_at: str
    updated_at: str
    state: TaskState = TaskState.PENDING
    attempt: int = 0
    max_attempts: int = 3
    # Per-task cap on how many non-blocking review findings are FILED as follow-up issues
    # (#191): the tunable sibling of the engine-wide default. A micro-pipeline for a small
    # fix and a full-pipeline for a large feature have very different expected review
    # surfaces, so an operator can raise/lower the cap per task type (via add_task) without
    # touching engine code, and adapters can surface it from the task doc. None = inherit the
    # engine's ``max_filed_followups`` default; an explicit value (>= 0) overrides it for this
    # task. Additive field: pre-#191 task docs load with None, so no SCHEMA_VERSION bump.
    max_filed_followups: int | None = None
    title: str = ""
    body: str = ""  # task-source description (e.g. the GitHub issue body) — feeds prompts
    provider_tag: str | None = None  # e.g. "codex" (the per-task :codex routing tag)
    # Per-task model pin (#84): a canonical model id (e.g. "claude-fable-5") that overrides the
    # role default on a model-lane stage so a brainstorm/heavy-architecture task runs on a higher
    # tier. Resolved + provider-validated at add_task (an alias like "fable" is normalized to the
    # id; a codex-tagged task may only pin a codex id, and vice versa). A pin is a STARTING tier,
    # not an anti-fallback lock — the rate-limit chain still degrades down from it (fable→opus→…)
    # when the lane allows, and a queued pending_fallback_model takes precedence for that dispatch.
    # Additive field: pre-#84 task docs load with None, so no SCHEMA_VERSION bump is needed.
    model_pin: str | None = None
    # Per-task reasoning-effort pin (#96): an Effort value ("low"/"medium"/"high") that
    # overrides the stage-spec default on model-lane stages — the effort sibling of
    # model_pin, validated at add_task via resolve_effort. A pin is honored by the
    # capacity effort-downshift (pins win, same rule as model_pin); deterministic
    # ENGINE-lane stages never carry effort regardless. Additive field: pre-#96 task
    # docs load with None, so no SCHEMA_VERSION bump is needed. Typed as the Effort enum
    # (#161, following #147's StageRecord.effort): the sibling attribution field
    # StageRecord.effort already holds its enum; a stored "low" coerces to Effort.LOW on
    # load, and the StrEnum still serializes/compares as its "low" value.
    effort_pin: Effort | None = None
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
    # pipeline itself became per-task in schema v2). Empty by default on the model; add_task
    # fills it from the lane preset (#68 — micro/lite default to deterministic TEST/DELIVER,
    # FULL keeps model TEST/DELIVER), so a hand-built/loaded doc keeps exactly its stored set.
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
    # #288: did the outstanding dispatch carry a multi-agent REVIEW ``plan``? The engine
    # needs this at RECORD time to tell "no panel was asked for" from "a panel was asked
    # for and the runner ignored it" — the WorkItem itself is not persisted, so without
    # this marker a plan-ignoring lane produces a review byte-indistinguishable from a
    # single-reviewer one. Set/cleared with the lease (never outlives it). Additive: a
    # pre-#288 task doc loads False, which reads as "no plan was dispatched".
    pending_plan: bool = False
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
    # Cross-provider fallthrough (#7): stages the engine has re-routed OFF codex onto claude
    # because the codex provider was persistently out (CLI missing / auth expired / floor
    # rate-limit with the wait budget spent). The Router routes any stage in this set to
    # claude, one-way (never back to codex — no ping-pong) and once per stage (a stage already
    # here never falls through again). Set only on an opted-in run (Run.cross_provider_fallback).
    # Additive field: pre-#7 task docs load with the empty default.
    fallthrough_stages: tuple[Stage, ...] = ()
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


class TaskRef(_StatusModel):
    """Lightweight pointer in the run doc; ``state`` is an explicit derived cache."""

    task_id: str
    status_file: str
    state: TaskState = TaskState.PENDING


class Progress(_StatusModel):
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


class Run(_StatusModel):
    """Run/batch document (status-<run>.json)."""

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
    # Cross-provider fallthrough (#7): when True, a codex-routed stage whose SAME-PROVIDER
    # options are exhausted (floor rate-limit with the wait budget spent) OR whose failure is
    # provider-unavailable-class (codex CLI missing / auth expired) is re-routed to the
    # equivalent claude lane on its NEXT dispatch, instead of parking/failing forever. One-way
    # (codex→claude, never the reverse — claude is the home provider), once per stage, and the
    # human's blanket consent: even a task that explicitly pinned :codex falls through under
    # this flag. DISTINCT from the within-provider model-chain/rate-limit fallback, which is
    # always on. Additive field: pre-#7 run docs load with the default. Default OFF.
    cross_provider_fallback: bool = False
    # Warm-retry session policy (#8): when True, a failed attempt's provider session is
    # REUSED on the retry instead of being discarded. Deliberately narrow — kept ONLY for a
    # mechanical/environmental failure (timeout / rate-limit / infra-classified), on the SAME
    # provider, and ONLY when the worktree state still matches the session (salvage kept the
    # committed work, OR the stage does no checkpoint reset). A content failure (schema
    # violation, real test failure, review rejection) always retries COLD — its context is as
    # likely poisoned as useful. Default OFF: the 2026-07-01 design pass (§2) decided
    # fresh-after-failure is the safe default; this flag is the explicit, bounded opt-in that
    # trades that safety for cost/latency on shallow failures. Additive field: pre-#8 run docs
    # load with the default, no SCHEMA_VERSION bump.
    warm_retry: bool = False
    # Mid-run progress commentary (#64): when True, the engine upserts a living progress
    # comment/PR-body section on the driving issue/PR at each stage boundary (throttled),
    # so a human can follow a long run from GitHub. Outward-facing, so default OFF — a run
    # against a real repo only posts when explicitly opted in. Additive field: pre-#64 run
    # docs load with the default, no SCHEMA_VERSION bump.
    progress_comments: bool = False
    # Review evidence-out filing cap (#191/#196): the run-wide DEFAULT number of non-blocking
    # findings a task files as follow-up issues, set once at run-create time so every task in
    # the run shares a non-default baseline without repeating --max-filed-followups per add.
    # The precedence at filing time is per-task (``Task.max_filed_followups``) > this run
    # default > the engine constructor default (``MAX_FILED_FOLLOWUPS_PER_TASK``). None = no
    # run-level override (fall through to the engine default). Additive field: pre-#196 run
    # docs load with the default, no SCHEMA_VERSION bump.
    max_filed_followups: int | None = None
    # Multi-agent find→verify REVIEW workflow (#73): when True, a model-lane REVIEW dispatch
    # on a plan-capable lane carries a ``ReviewPlan`` (independent finder lenses + adversarial
    # verify) instead of the single mega-prompt reviewer. Default OFF — the plan-less path is
    # the permanent fallback, not scaffolding, and the design gates defaulting-on behind live
    # eval evidence. MUST live here rather than on the Engine: every CLI subcommand rebuilds
    # the Engine from constructor defaults, so a create-time-only setting would be gone by the
    # next subcommand — ``next_work`` re-reads it off this doc at the dispatch boundary (#206).
    # Cost/capacity policy may still veto it per dispatch. Additive field: pre-#73 run docs
    # load with the default, no SCHEMA_VERSION bump.
    review_workflow: bool = False

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
