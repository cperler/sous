"""The engine: ties the deterministic modules into the supervisor's operations.

CRITICAL INVARIANT: the engine NEVER calls a model. ``next_work`` emits a WorkItem;
the supervisor dispatches it on the execution lane; ``record`` ingests the returned
StageResult (cost ledger + status). Every model call therefore produces a ledger
row keyed by its actual lane — an unattributed call is structurally impossible
(closes as-built D6).
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .alerting import (
    NOTIFY_RUN_FINALIZED,
    NOTIFY_TASK_COMPLETED,
    NOTIFY_TASK_FAILED,
)
from .capacity import DEFAULT_CAPACITY, CapacityPolicy, DispatchBand
from .commit_attribution import scan_commits
from .cost_ledger import CostLedger
from .cost_policy import BUDGET_SOFT_FRACTION, DEFAULT_COST_ROUTER, CostRouter
from .dag import Dag
from .decomposition import (
    ChildTaskPlan,
    DecompositionError,
    leaf_ids,
    parse_subtasks,
    topological_order,
)
from .driver_log import liveness_from_log
from .errors import (
    CapacityExhausted,
    ContractError,
    NoRunnerError,
    RunExistsError,
    StatusNotFoundError,
    SupervisorParkDeferred,
)
from .learnings_kb import (
    append_learnings as append_kb_learnings,
)
from .learnings_kb import (
    harvest_from_task,
    harvest_process_retrospective,
    relevant_learnings,
    resolve_kb_path,
)
from .learnings_kb import (
    read_entries as read_kb_entries,
)
from .learnings_kb import (
    tokenize as _kb_tokenize,
)
from .meta_authoring import (
    append_filing,
    proposal_body,
    proposal_filing_guard,
    proposal_title,
    recurring_proposals,
)
from .model_table import (
    DEFAULT_MODEL_TABLE,
    ENGINE_MODEL,
    ModelTable,
    provider_for_model,
    resolve_model_alias,
)
from .port_registry import (
    port_env_for,
    project_needs_ports,
    registry_for_project,
)
from .ports.execution import Registry, default_registry
from .ports.project import ProjectConfig
from .render import (
    format_review_issue,
    render_completion_note,
    render_cost_report,
    render_cost_summary,
    render_progress,
    render_rejection_note,
    render_retrospective,
    render_stage,
    render_task_index,
    unfiled_findings,
)
from .retrospective import build_retrospective
from .retry import CircuitBreaker, error_signature
from .review_workflow import issue_fingerprint, synthesize
from .routing import DEFAULT_ROUTER, Router, engine_lane_required
from .schemas.enums import (
    LANE_DETERMINISTIC_STAGES,
    LANE_STAGES,
    SUPPORTED_WORK_VERSIONS,
    TERMINAL_RUN_STATES,
    TERMINAL_TASK_STATES,
    Effort,
    ExecutionLane,
    ExecutionMode,
    FailureKind,
    ImplementationBudget,
    ModelId,
    PermissionPosture,
    Provider,
    QualityTier,
    ResultStatus,
    RunState,
    Stage,
    StageStatus,
    TaskState,
    effort_below,
    resolve_effort,
)
from .schemas.status import ReviewFixup, Run, RunDriver, Task, TaskRef
from .schemas.work import (
    LanePolicy,
    LaneUsed,
    ReviewPlan,
    StageResult,
    TokenUsage,
    ToolPolicy,
    WorkItem,
)
from .spec_refresh import diff_summary as spec_diff_summary
from .spec_refresh import fingerprint as spec_fingerprint
from .stages import STAGE_SPECS, DiffStat, render_prompt, render_review_plan
from .state_machine import (
    apply_result,
    begin_stage,
    is_done,
    next_stage,
    no_model_test_surface,
    pr_not_opened,
    reset_for_fix_cycle,
    resume_point,
    unjudged_tests_notice,
)
from .status_store import StatusStore
from .stream_probe import probe_current_stream, prompt_filename, prompt_relpath
from .supervisor_context import DEFAULT_MIN_REMAINING_PCT, SupervisorContext


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_s(started_at: str | None) -> float | None:
    """Wall-clock seconds since an ISO stage-start stamp, for a cost-ledger row an
    operator path synthesizes for a dispatch that never reported back (``abandon``,
    ``retire --force``). ``None`` when the stamp is absent or unparsable — an unknown
    duration must read as unknown, never as 0.0."""
    if not started_at:
        return None
    try:
        return max(
            0.0,
            (datetime.now(UTC) - datetime.fromisoformat(started_at)).total_seconds(),
        )
    except ValueError:
        return None


_CHILD_PIPELINES: dict[QualityTier, tuple[Stage, ...]] = {
    QualityTier.FULL: (
        Stage.INTAKE,
        Stage.IMPLEMENT,
        Stage.SIMPLIFY,
        Stage.TEST,
        Stage.DELIVER,
        Stage.REVIEW,
    ),
    QualityTier.LIGHT: (
        Stage.INTAKE,
        Stage.IMPLEMENT,
        Stage.TEST,
        Stage.DELIVER,
        Stage.REVIEW,
    ),
    QualityTier.NONE: (
        Stage.INTAKE,
        Stage.IMPLEMENT,
        Stage.TEST,
        Stage.DELIVER,
    ),
}
_IMPLEMENT_TIMEOUTS: dict[ImplementationBudget, int] = {
    ImplementationBudget.STANDARD: 1800,
    ImplementationBudget.SHORT: 900,
}


def _hash_preview(value: str | None) -> str | None:
    """Short, comparable rendering of a content_hash for an audit event / error text (#311).

    Keeps the leading bytes AND the length: the failure this exists for is a TRUNCATED
    digest (a 16-char preview pasted where the 64-char hash belongs), which a prefix alone
    would render identical to the real one. ``None`` (no lease) stays ``None``."""
    if value is None:
        return None
    return f"{value[:12]}...(len {len(value)})"


# Cap on the completion-note prose folded into a ``task_completed`` alert payload (#359).
# The payload is appended verbatim to events.jsonl and handed to a sink that may put it in an
# email body, so an unbounded note would bloat both. Generous enough that a normal note (a
# few hundred lines of markdown at most) passes through whole.
NOTIFY_NOTE_MAX_CHARS = 8000


def _bounded(text: str | None, limit: int) -> str | None:
    """Truncate ``text`` to ``limit`` characters, SAYING SO in the text itself when it cuts
    (the "never silent" convention — a reader must never mistake a truncated note for the
    whole one). ``None`` stays ``None``."""
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… [truncated at {limit} chars — full note in the run log]"


def _in_future(iso: str | None) -> bool:
    """Is an ISO timestamp still ahead of now? Unparsable/absent => False (never
    let a corrupt cooldown stamp park a task forever)."""
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso) > datetime.now(UTC)
    except ValueError:
        return False


def _validated_budget(value: float | None, *, field: str, run_id: str) -> float | None:
    """Contract-check an explicit USD budget: ``None`` (no cap) or a finite amount > 0.

    A zero/negative/NaN/inf budget is a caller error, not a policy. It used to be stored
    verbatim and then divided by in the soft-warning branch of ``_budget_hard_stop``,
    so ``budget_usd=0`` crashed the first dispatch with ZeroDivisionError and a negative
    cap parked the run instantly (#274). Rejected at the write boundary instead, so no
    Run doc ever carries an unusable cap."""
    if value is None:
        return None
    if not math.isfinite(value) or value <= 0:
        raise ContractError(
            f"{field} must be a finite USD amount > 0, got {value!r} for run {run_id}"
        )
    return float(value)


# Bounded output tail for the trunk gate (#229): a failing verification command can spew
# thousands of lines, but the event/issue only needs the last few for the operator to see
# what broke. Truncation is never silent — the caller marks a trimmed tail as such.
_TRUNK_GATE_TAIL_LINES = 40

# Bound the post-commit attribution scan (#322). A stage produces a handful of commits; a
# three-figure range means the anchor was wrong (a stale checkpoint, a rebased branch), and
# walking it would turn a cheap audit into an unbounded git read. Truncation is never silent
# — the scan event carries ``capped``.
_ATTRIBUTION_SCAN_COMMIT_CAP = 50


def _tail(text: str, n_lines: int = _TRUNK_GATE_TAIL_LINES) -> tuple[str, bool]:
    """The last ``n_lines`` of ``text`` plus whether anything was dropped, so a
    truncated tail can be flagged (never-silent) rather than passed off as the whole."""
    lines = (text or "").splitlines()
    if len(lines) <= n_lines:
        return "\n".join(lines), False
    return "\n".join(lines[-n_lines:]), True


def _pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process on THIS host? (#313 — the driver-liveness sensor.)

    Deliberately fails SAFE (True) on anything it cannot answer: an EPERM signal-0 means
    the pid exists under another uid, and an unexpected OSError says nothing about
    death. A wrong "dead" would let a second driver steal a live driver's dispatch lease
    and double-dispatch the same stage; a wrong "alive" only leaves an orphan for the
    operator to resolve loudly. Mirrors ``port_registry._pid_alive``'s convention.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# Default liveness window for `abandon` (#82): a mid-dispatch abandon is refused when the
# task's provider stream grew within this many seconds of now (the dispatch may still be
# alive). Five minutes is comfortably longer than a normal inter-event gap on a live
# headless stream; `force=True` overrides it when the operator knows the process is dead.
DEFAULT_ABANDON_MIN_IDLE_S = 300

# Filing threshold for review evidence-out (#188): a review can surface any number of
# non-blocking findings, but task completion must not become a hydra (one task spawning
# a dozen follow-ups, as batch-queue-1 did). Only findings explicitly dispositioned
# `file` are filed, and only up to this cap per task; absent, empty, or unrecognized
# dispositions, `fix_now`/`drop` findings, and any `file` finding over the cap are
# surfaced in the completion note's "Noted, not filed" section instead, so nothing is
# silently dropped without ballooning the backlog. This is the engine-wide DEFAULT
# (#191): it is overridable at engine-construction / run-create time via
# ``max_filed_followups`` and, more granularly, per task via
# ``add_task(max_filed_followups=...)`` — a micro-pipeline and a full-pipeline have very
# different expected review surfaces.
MAX_FILED_FOLLOWUPS_PER_TASK = 2


# Failure kinds whose committed work is NOT implicated by the failure itself, so any
# commits the attempt made before dying are worth KEEPING for the retry (#59): the model
# ran out of wall-clock (TIMEOUT) or hit a transient provider wall (RATE_LIMITED) — the
# code it committed is unrelated to why the stage failed. An infra-classified failure
# (FailureKind.INFRA — a broken environment, not broken code) is salvageable too and is
# folded in separately (it is a classifier verdict, not a ResultStatus). A plain FAILURE
# — notably a genuine TEST failure — is DELIBERATELY excluded: the committed code may BE
# the defect, so the safe default (reset to the checkpoint) stands.
SALVAGEABLE_FAILURE_STATUSES = frozenset({ResultStatus.TIMEOUT, ResultStatus.RATE_LIMITED})

# #288: the statuses that mean the provider never actually ran the dispatched WorkItem —
# the stage goes back on the wire with its attempt intact. A plan-bearing REVIEW that comes
# back like this is NOT evidence that the runner ignored its plan, so the
# ``review_plan_not_executed`` marker is withheld for them (no crying wolf on a run whose
# retry does execute the panel).
_PLAN_UNRUN_STATUSES = frozenset({ResultStatus.RATE_LIMITED, ResultStatus.PROVIDER_UNAVAILABLE})

# The supervisor park/resume pair (#259). Their crash recovery reads the LAST event drawn
# from this set, so the two types must be listed together: a park that only looked for a
# prior park would treat a settled park→resume pair as an interrupted episode.
_SUPERVISOR_LIFECYCLE_EVENTS = frozenset({"supervisor_parked", "supervisor_resumed"})

# Minimum ledger rows a (stage, effort) group needs before its empirical retry/failure
# rate is trusted to move the adaptive downgrade band (#155). Below this, the observation
# is too noisy to act on, so the band falls back to the flat downgrade_threshold (today's
# behavior) — an empty ledger and sparse early runs are unaffected.
ADAPTIVE_BAND_MIN_SAMPLE = 5


def _ref_safe(s: str) -> str:
    """Make an id safe inside a git ref component (tags: design pass §3). Conservative:
    anything outside [word . -] becomes '-', which also rules out the refname-forbidden
    sequences ('..', '@{', '~', '^', ':', '?', '*', '[', space)."""
    return re.sub(r"[^\w.\-]", "-", s) or "x"


class Engine:
    """Deterministic orchestration over one run. Single-task flow for Phase 3a
    (the DAG/capacity machinery is wired and tested for the 3b scheduler)."""

    def __init__(
        self,
        store: StatusStore,
        ledger: CostLedger,
        project: ProjectConfig,
        *,
        model_table: ModelTable = DEFAULT_MODEL_TABLE,
        capacity: CapacityPolicy = DEFAULT_CAPACITY,
        router: Router = DEFAULT_ROUTER,
        cost_router: CostRouter = DEFAULT_COST_ROUTER,
        budget_soft_fraction: float = BUDGET_SOFT_FRACTION,
        registry: Registry | None = None,
        max_attempts: int = 3,
        breaker_threshold: int = 2,
        concurrency_ceiling: int = 1,
        max_review_cycles: int = 2,
        max_rate_limit_waits: int = 4,
        rate_limit_cooldown_s: int = 900,
        max_infra_resets: int = 2,
        max_salvage_keeps: int = 1,
        max_filed_followups: int = MAX_FILED_FOLLOWUPS_PER_TASK,
        progress_throttle_s: float = 60.0,
        use_learnings_kb: bool = True,
    ) -> None:
        # Process-boundary persistence (#206): the tuning knobs below are engine-DEFAULT
        # holders, not durable run config. Every CLI subcommand rebuilds the Engine from
        # these constructor defaults (``cli._engine`` passes only store/ledger/project/
        # router/registry), so a value chosen at run-create time is remembered only inside
        # the process that set it. Any setting consulted at a LATER stage boundary — dispatch,
        # retry, review-gate, filing, completion — MUST therefore be persisted on the Run
        # (or Task) doc and re-read there, NOT relied on from self.*. Today the two run-level
        # settings that cross that boundary do so correctly: ``max_attempts`` is stamped onto
        # Task at add_task (read as ``task.max_attempts``) and ``max_filed_followups`` onto
        # Run/Task (read via load_run at filing time — #196). The retry/gate budgets below
        # (max_review_cycles, max_rate_limit_waits, rate_limit_cooldown_s, max_infra_resets,
        # max_salvage_keeps, breaker_threshold, budget_soft_fraction) are ALSO consulted at
        # record()-time across the boundary — they are safe ONLY because no create_run/CLI
        # override exists yet, so they are always the default. The moment one gains a run-level
        # override it must land on Run first. See the audit:
        # docs/reviews/2026-07-18-run-level-settings-persistence-audit.md.
        self.store = store
        self.ledger = ledger
        self.project = project
        self.models = model_table
        # Single pricing source: the ledger prices with the engine's model table.
        self.ledger.model_table = model_table
        self.capacity = capacity
        self.router = router
        # Cost-aware lane routing (#34): deterministic budget-fraction -> lane-preset map,
        # consulted at add_task when the run enables route_by_cost. Default = the stock
        # band table; a project can inject its own.
        self.cost_router = cost_router
        # Soft budget-warning threshold: fraction of the per-run budget at which the
        # once-only budget_warning notification fires (the hard stop is at 1.0).
        self.budget_soft_fraction = budget_soft_fraction
        # The registry defines which (mode, provider) cells are sanctioned (for the
        # lane audit). Default = interactive×claude (3a/3b).
        self.registry = registry if registry is not None else default_registry()
        self.max_attempts = max_attempts
        self.breaker_threshold = breaker_threshold
        self.concurrency_ceiling = concurrency_ceiling
        # Review gate: how many rejection-triggered fix cycles (re-run implement→…→review
        # with the blocking issues as learnings) before the task parks BLOCKED_ON_HUMAN.
        self.max_review_cycles = max_review_cycles
        # Rate-limit cooldown at the fallback-chain floor: wait this long and retry the
        # ORIGINAL model (the old wait-until-reset behavior) instead of failing the
        # attempt; bounded so a permanently-limited account still fails out.
        self.max_rate_limit_waits = max_rate_limit_waits
        self.rate_limit_cooldown_s = rate_limit_cooldown_s
        # Infra-failure reset loop (#14): how many times an infra-classified TEST
        # failure may reset the environment and re-run the SAME attempt before it
        # degrades to a normal failure (the old MAX_CONSECUTIVE_INFRA_FAILURES halt).
        self.max_infra_resets = max_infra_resets
        # Salvage cap (#59): how many times the current stage may KEEP its committed work
        # across a salvageable failure before a repeat failure resets fully. 1 = keep once;
        # if the salvaged work didn't unstick the retry, the next salvageable failure
        # discards the pile and starts clean from the checkpoint.
        self.max_salvage_keeps = max_salvage_keeps
        # Review evidence-out filing cap (#188/#191): the engine-wide DEFAULT number of
        # non-blocking findings a task files as follow-up issues. A per-task
        # ``Task.max_filed_followups`` (set via add_task) overrides this; None there falls
        # back here. Validated non-negative at construction.
        if max_filed_followups < 0:
            raise ValueError(f"max_filed_followups must be >= 0, got {max_filed_followups}")
        self.max_filed_followups = max_filed_followups
        # Mid-run progress commentary (#64): don't hammer the GitHub API on rapid stage
        # boundaries — skip a publish if this task's last one was < this many seconds ago.
        # The last-publish stamp is per-PROCESS in-memory (record() is frequent and this is
        # best-effort audit UX, not durable state): a resumed run simply re-publishes, which
        # is harmless given the upsert (one living comment, never spam). Keyed by (run, task).
        self.progress_throttle_s = progress_throttle_s
        self._last_progress_at: dict[tuple[str, str], float] = {}
        # Cross-run learnings KB (#72): harvest a finished task's learnings into a durable,
        # per-project append-only KB, and fold relevant PRIOR learnings back into a fresh
        # task's intake context. Read-only context, low risk, so default ON; an
        # ``ORCHESTRATOR_NO_LEARNINGS_KB=1`` env escape hatch (checked live) disables it.
        self.use_learnings_kb = use_learnings_kb

    # --- run/task setup -------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        lane: ExecutionLane = ExecutionLane.FULL,
        *,
        budget_usd: float | None = None,
        route_by_cost: bool = False,
        route_by_capacity: bool = False,
        cross_provider_fallback: bool = False,
        warm_retry: bool = False,
        progress_comments: bool = False,
        max_filed_followups: int | None = None,
        review_workflow: bool = False,
    ) -> Run:
        """Create a run. ``budget_usd`` caps metered spend (soft warning at
        ``budget_soft_fraction``, hard PAUSE at/after the budget — #34; must be a finite
        amount > 0 when given, an unusable cap is rejected here — #274); ``route_by_cost``
        enables cost-aware lane routing for un-pinned tasks; ``route_by_capacity`` enables
        capacity-aware model downgrade of fresh dispatches under high utilization (#12);
        ``cross_provider_fallback`` enables codex→claude fallthrough when the codex provider is
        persistently out (#7 — the flag is the human's blanket consent, so even a :codex-pinned
        task falls through under it); ``warm_retry`` opts a failed attempt's session into being
        REUSED on the retry when the failure was mechanical (#8 — off by design per the
        2026-07-01 design pass §2, so this is the explicit, bounded opt-in); ``progress_comments``
        opts into mid-run progress commentary on the driving issue/PR (#64 — outward-facing, so
        default off). ``max_filed_followups`` (#196) sets a run-wide default cap on filed
        review follow-ups so every task in the run shares a non-default baseline without
        repeating it per add_task; None inherits the engine constructor default.
        ``review_workflow`` (#73) opts REVIEW into the multi-agent find→verify panel on lanes
        that can execute a plan — off by default, and cost/capacity policy can still veto it
        per dispatch. All default off; the routers are DISTINCT levers (USD vs rate-limit
        headroom vs provider outage vs failed-session reuse).

        Re-initializing an EXISTING run id is refused with ``RunExistsError`` (#280):
        replacing the run doc would orphan the run's task documents (they stay on disk
        while dropping out of ``task_refs``) and erase its dependency graph, state and
        settings. There is deliberately no force/overwrite variant — a new run id is the
        answer, and run logs are the human's to prune. Callers that genuinely want
        create-or-reuse (queue ingestion) call ``create_or_reuse_run``."""
        if max_filed_followups is not None and max_filed_followups < 0:
            raise ContractError(
                f"max_filed_followups must be >= 0, got {max_filed_followups} for run {run_id}"
            )
        budget_usd = _validated_budget(budget_usd, field="budget_usd", run_id=run_id)
        run = Run(
            run_id=run_id, created_at=_now(), updated_at=_now(), lane=lane,
            state=RunState.RUNNING, budget_usd=budget_usd, route_by_cost=route_by_cost,
            route_by_capacity=route_by_capacity,
            cross_provider_fallback=cross_provider_fallback,
            warm_retry=warm_retry,
            progress_comments=progress_comments,
            max_filed_followups=max_filed_followups,
            review_workflow=review_workflow,
        )
        self.store.create_run_doc(run)
        return run

    #: The run-level settings ``create_or_reuse_run`` treats as IMMUTABLE — a reuse that
    #: asks for different values is a different run, so it raises instead of silently
    #: handing back a run configured some other way. Kept in sync with ``create_run``'s
    #: parameters by the #206 persistence guard test.
    _REUSE_IMMUTABLE_SETTINGS = (
        "lane", "budget_usd", "route_by_cost", "route_by_capacity",
        "cross_provider_fallback", "warm_retry", "progress_comments",
        "max_filed_followups", "review_workflow",
    )

    def create_or_reuse_run(
        self,
        run_id: str,
        lane: ExecutionLane = ExecutionLane.FULL,
        *,
        budget_usd: float | None = None,
        route_by_cost: bool = False,
        route_by_capacity: bool = False,
        cross_provider_fallback: bool = False,
        warm_retry: bool = False,
        progress_comments: bool = False,
        max_filed_followups: int | None = None,
        review_workflow: bool = False,
    ) -> tuple[Run, bool]:
        """The EXPLICIT idempotent create (#280). Returns ``(run, created)``: the freshly
        created run with ``created=True``, or the already-persisted run with
        ``created=False`` — never a replacement, so an existing run's task refs,
        dependency graph, state and settings survive a repeated ingest.

        Reuse is only idempotent when it asks for the SAME run: the requested immutable
        settings are compared against the persisted ones and a mismatch raises
        ``ContractError`` listing the diffs, rather than handing back a run configured
        differently from what the caller asked for. A corrupt/unreadable run doc
        propagates as ``StatusStoreError`` — it is never treated as "absent" (#112)."""
        requested: dict[str, object] = dict(
            lane=lane, budget_usd=budget_usd, route_by_cost=route_by_cost,
            route_by_capacity=route_by_capacity,
            cross_provider_fallback=cross_provider_fallback, warm_retry=warm_retry,
            progress_comments=progress_comments,
            max_filed_followups=max_filed_followups, review_workflow=review_workflow,
        )
        try:
            created = self.create_run(
                run_id, lane, budget_usd=budget_usd, route_by_cost=route_by_cost,
                route_by_capacity=route_by_capacity,
                cross_provider_fallback=cross_provider_fallback, warm_retry=warm_retry,
                progress_comments=progress_comments,
                max_filed_followups=max_filed_followups, review_workflow=review_workflow,
            )
        except RunExistsError:
            pass
        else:
            return created, True
        # Existing doc — load it (a corrupt doc raises here, as it must) and verify the
        # caller is asking for the run that is actually on disk.
        existing = self.store.load_run(run_id)
        diffs = [
            f"{name}: requested {requested[name]!r} != persisted {getattr(existing, name)!r}"
            for name in self._REUSE_IMMUTABLE_SETTINGS
            if requested[name] != getattr(existing, name)
        ]
        if diffs:
            raise ContractError(
                f"run {run_id} already exists with different settings; "
                f"refusing to reuse it: {'; '.join(diffs)}"
            )
        return existing, False

    def add_task(
        self,
        run_id: str,
        task_id: str,
        lane: ExecutionLane | None = None,
        *,
        pipeline: tuple[Stage, ...] | list[Stage] | None = None,
        depends_on: list[str] | None = None,
        provider_tag: str | None = None,
        deterministic_stages: tuple[Stage, ...] | list[Stage] | None = None,
        estimate: str | float | None = None,
        model: str | None = None,
        effort: str | None = None,
        agent_role: str | None = None,
        quality_tier: str | QualityTier | None = None,
        implementation_budget: str | ImplementationBudget | None = None,
        max_filed_followups: int | None = None,
        hold_before: Stage | None = None,
    ) -> Task:
        """Register a task. ``pipeline`` is the ordered stage list this task runs;
        omitted, it resolves from the lane preset (design pass §1 — a lane is a named
        pipeline, not the sequencing mechanism). ``deterministic_stages`` marks which of
        those stages run on the $0 ENGINE lane in ADDITION to the globally-deterministic
        intake (#33 — a pipeline opting TEST/DELIVER into the shell runners). Omitted, a
        task resolved from a lane preset inherits that preset's default (#68): micro/lite
        run TEST/DELIVER deterministically, FULL keeps model TEST/DELIVER. An explicit
        pipeline pin opts out of the preset default. ``depends_on``/``provider_tag``
        override the task source's values — the supervisor's way to supply DAG edges
        and per-task provider routing when the source has no analysis step (the old
        ``82:codex`` tag / ralph dependency analysis, human-supplied). ``model`` pins a
        per-task model tier (#84); ``effort`` pins a per-task reasoning effort
        (low/medium/high, #96) that overrides the stage-spec defaults the same way.
        ``agent_role``/``quality_tier``/``implementation_budget`` carry SCOPE-authored
        child controls (#60). The role is resolved through the project adapter; the quality
        tier selects the child's explicit quality pipeline and the budget selects a 30- or
        15-minute IMPLEMENT timeout. ``max_filed_followups`` (#191) caps how many
        non-blocking review findings THIS task
        files as follow-up issues, overriding the engine-wide default for a task type whose
        expected review surface differs (a micro fix vs a full feature); None inherits the
        engine default, a negative value is rejected. ``hold_before`` parks the task at a
        human gate before that stage; source tasks carrying the ``meta-authoring`` label
        default to a pre-DELIVER hold. Validation (non-empty, duplicate-free)
        is the Task model's.

        Cost-aware lane routing (#34): when the run enables ``route_by_cost`` AND no
        ``pipeline`` is explicitly pinned, the deterministic ``cost_router`` picks the
        lane preset from the run's remaining budget fraction (refined by ``estimate`` /
        the task's ``size:``/``estimate:`` labels) and prefers $0 deterministic
        TEST/DELIVER — every such decision is emitted as a ``lane_routed`` event (never
        silent). An explicit ``pipeline`` is always honored.

        Registration is atomic and idempotent-on-crash (#278): the run-doc ref write and
        the task-doc write happen under one held task lock (see
        :meth:`StatusStore.with_task_lock`), so a concurrent ``add_task`` for the same
        ``task_id`` blocks rather than racing, and a process crash between the two writes
        leaves a repairable half-registration rather than a duplicate-reject deadend — the
        next call for the same ``task_id`` completes it in place (emitting a
        ``task_registration_repaired`` event) instead of raising. A ref whose document
        exists and matches raises ``ContractError`` ("already added"), as before. See
        :meth:`registered_task_ids` for the corresponding "is this task fully registered"
        check used by re-ingest.

        The resolved ``title``/``body`` are snapshotted onto the Task doc HERE and never
        re-read automatically (#271) — every stage prompt for the rest of the run renders
        from this copy, which is what makes a run reproducible and prompts byte-stable.
        The snapshot's provenance is stamped alongside it (``spec_captured_at``,
        ``spec_source_updated_at`` from ``spec.updated_at`` when the source reports one,
        ``spec_fingerprint``), so a later ``refresh_spec``/``spec_staleness`` call has
        something to compare against and date. See :meth:`refresh_spec` for the sanctioned,
        audited way to move the snapshot mid-run."""
        spec = self.project.task_source.resolve(task_id)
        if hold_before is None and any(
            str(label).strip().casefold() == "meta-authoring" for label in spec.labels
        ):
            hold_before = Stage.DELIVER
        deps = list(depends_on) if depends_on is not None else list(spec.depends_on)
        tag = provider_tag if provider_tag is not None else spec.provider_tag
        # Per-task model pin (#84): resolve the alias/id, then validate it against the task's
        # provider BEFORE any state is written — a codex-tagged task may only pin a codex id and
        # a claude task only a claude id (a mismatched pin would never dispatch). The task's
        # provider is codex only when tagged so; every other task is claude (the home provider).
        model_pin: str | None = None
        if model is not None:
            model_pin = resolve_model_alias(model)
            pin_provider = provider_for_model(model_pin)
            task_provider = Provider.CODEX if tag == "codex" else Provider.CLAUDE
            if pin_provider is not task_provider:
                raise ContractError(
                    f"model pin {model_pin!r} is a {pin_provider.value} model but task "
                    f"{task_id} is {task_provider.value}-provider "
                    f"(provider_tag={tag!r}) — pin a {task_provider.value} model instead"
                )
        # Per-task effort pin (#96): normalized/validated BEFORE any state is written,
        # exactly like the model pin. Provider-agnostic — every lane translates (or
        # ignores) the same low/medium/high vocabulary, so no provider check is needed.
        effort_pin: Effort | None = None
        if effort is not None:
            effort_pin = resolve_effort(effort)
        resolved_quality = QualityTier(quality_tier) if quality_tier is not None else None
        resolved_budget = (
            ImplementationBudget(implementation_budget)
            if implementation_budget is not None else None
        )
        # Per-task filing cap (#191): validated BEFORE any state is written, like the pins.
        # None inherits the engine default; a negative cap is nonsensical (a cap of 0 already
        # means "file nothing").
        if max_filed_followups is not None and max_filed_followups < 0:
            raise ContractError(
                f"max_filed_followups must be >= 0, got {max_filed_followups} for task {task_id}"
            )
        run = self.store.load_run(run_id)
        # Cost-aware lane routing (#34): only for an UN-pinned task on a route_by_cost run.
        # An explicit pipeline pin is always honored (never overridden). The decision is
        # deterministic (band table over the remaining budget fraction) and evented below.
        route_reason: dict | None = None
        effective_lane = lane or run.lane
        pipeline_pinned = pipeline is not None
        if pipeline is None and run.route_by_cost:
            est = estimate if estimate is not None else self._estimate_from_labels(spec.labels)
            decision = self.cost_router.route(self._remaining_budget_fraction(run), est)
            effective_lane = decision.lane if lane is None else effective_lane
            pipeline = LANE_STAGES[decision.lane]
            if deterministic_stages is None:
                deterministic_stages = decision.deterministic_stages
            route_reason = decision.reason
        # #68: a task resolved from a lane PRESET (no explicit pipeline pin) adopts that
        # preset's default deterministic stages — micro/lite run TEST/DELIVER on the $0
        # ENGINE lane, FULL keeps model TEST/DELIVER. An explicit ``deterministic_stages``
        # (--deterministic-stages) or a cost-routing decision above already set it and wins;
        # an explicit pipeline pin is a bespoke pipeline that states its own deterministic
        # stages, so the preset default does not reach it.
        if deterministic_stages is None and not pipeline_pinned:
            deterministic_stages = LANE_DETERMINISTIC_STAGES.get(effective_lane, ())
        # Register the task ref + dependency edge as a locked read-modify-write so a
        # concurrent add can't lose a ref or graph entry, and reject a duplicate add.
        # Done BEFORE writing the task doc so a duplicate never clobbers an existing
        # task's persisted progress.
        #
        # Registration is recoverable AS A UNIT (#278): a crash between the ref write and
        # the doc write below leaves a ref pointing at a missing document, which no retry
        # could ever repair while an existing ref was a bare duplicate reject. So an
        # existing ref is probed against its document: present-and-matching is the real
        # duplicate (rejected, no clobber), while a missing/mismatched document on a
        # still-PENDING ref is a partial registration this add completes in place.
        #
        # That repair branch is safe ONLY because both writes happen under ONE held task
        # lock (``with_task_lock`` below): the run lock alone is released between them, so
        # two concurrent adds of the same task_id would otherwise BOTH see "ref present,
        # doc absent, ref PENDING" — the crash shape — each take the repair path, and the
        # later doc write would silently clobber the earlier caller's task (differing lane /
        # pins / deps) with no error to either side. Serializing on the task lock makes the
        # loser observe the completed registration and get the duplicate ContractError, so
        # "ref present + doc absent" is a crash signature and never a live race.
        repair_reason: str | None = None

        def _register(r: Run) -> None:
            nonlocal repair_reason
            existing = next((ref for ref in r.task_refs if ref.task_id == task_id), None)
            if existing is None:
                r.task_refs.append(
                    TaskRef(task_id=task_id, status_file=f"status-{run_id}-{task_id}.json")
                )
                r.dependency_graph[task_id] = list(deps)
                return
            # Probe the document. The caller already holds this task's lock (outer) so the
            # read can't land mid-write and no concurrent add can change the answer under
            # us; no lock is taken here (that would block on the one we hold). A
            # corrupt/unreadable doc propagates as StatusStoreError — never absent (#112).
            reason = self._partial_registration_reason(run_id, task_id)
            if reason is None:
                raise ContractError(f"task {task_id} already added to run {run_id}")
            if existing.state is not TaskState.PENDING:
                # The ref records progress the missing doc can no longer back; re-creating a
                # fresh PENDING doc would invent state. Surface it instead of guessing.
                raise ContractError(
                    f"task {task_id} of run {run_id} has a {existing.state.value} ref whose "
                    f"status document is unusable ({reason}) — refusing to re-register it "
                    "over recorded progress"
                )
            # Complete the partial registration in place: no second ref, refreshed edge and
            # canonical status_file, and the doc write below finishes the unit.
            existing.status_file = f"status-{run_id}-{task_id}.json"
            r.dependency_graph[task_id] = list(deps)
            repair_reason = reason

        # ONE transaction boundary for the registration: the task lock spans the ref write
        # and the doc write, so a concurrent add of the same task_id is serialized behind it
        # (see the note above). ``write_task_locked`` writes under the lock we already hold —
        # ``save_task`` would re-take it and deadlock.
        with self.store.with_task_lock(run_id, task_id):
            self.store.update_run(run_id, _register)
            task = Task(
                task_id=task_id,
                run_id=run_id,
                created_at=_now(),
                updated_at=_now(),
                state=TaskState.PENDING,
                title=spec.title,
                body=spec.body,
                # #271: stamp the snapshot's provenance WITH the snapshot — when it was
                # captured, what the source called its own last-modified time, and the
                # content fingerprint a later staleness check compares against. Written
                # here (not lazily) so every task doc carries an origin for the copy its
                # prompts render from. ``spec.updated_at`` is duck-typed off the TaskSpec
                # so an external adapter pinned to an older contract still registers.
                spec_captured_at=_now(),
                spec_source_updated_at=getattr(spec, "updated_at", None),
                spec_fingerprint=spec_fingerprint(spec.title, spec.body),
                provider_tag=tag,
                agent_role=agent_role,
                quality_tier=resolved_quality,
                implementation_budget=resolved_budget,
                model_pin=model_pin,
                effort_pin=effort_pin,
                issue_number=spec.issue_number,
                depends_on=deps,
                execution_lane=effective_lane,
                pipeline=tuple(pipeline) if pipeline else LANE_STAGES[effective_lane],
                deterministic_stages=tuple(deterministic_stages or ()),
                hold_before=hold_before,
                max_attempts=self.max_attempts,
                max_filed_followups=max_filed_followups,
            )
            self.store.write_task_locked(task)
        if repair_reason is not None:
            # Recovery is never silent (#278): the mutator only reports what it repaired,
            # the engine call site events it — once the doc write above has actually closed
            # the registration.
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "task_registration_repaired", "run_id": run_id,
                 "task_id": task_id, "reason": repair_reason},
            )
        if route_reason is not None:
            # Routing is never silent (#34): record WHY this un-pinned task got its preset
            # (remaining budget fraction, estimate) so a downgraded run explains itself.
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "lane_routed", "run_id": run_id,
                 "task_id": task_id, **route_reason},
            )
        return task

    def _partial_registration_reason(self, run_id: str, task_id: str) -> str | None:
        """``None`` when ``task_id``'s status document exists AND agrees with the ref's
        identity (a genuinely registered task); otherwise a short reason describing why the
        registration is only half-written and is therefore repairable (#278).

        A corrupt or unreadable document raises ``StatusStoreError`` — an I/O or parse
        failure is never reported as "absent" (#112), because re-registering over it would
        destroy a document that may well hold real progress."""
        try:
            task = self.store.load_task(run_id, task_id)
        except StatusNotFoundError:
            return "status_document_missing"
        if task.run_id != run_id or task.task_id != task_id:
            return (
                f"status_document_identity_mismatch (doc claims run {task.run_id!r} "
                f"task {task.task_id!r})"
            )
        return None

    def registered_task_ids(self, run_id: str) -> set[str]:
        """The task ids of ``run_id`` that are FULLY registered: a task ref in the run doc
        AND a matching status document (#278).

        The set an idempotent re-ingest must skip. The raw ``task_refs`` ids are not that
        set: a crash between ``add_task``'s ref write and its doc write leaves a ref whose
        document never got written, and skipping on the ref alone would make that partial
        registration permanent. A corrupt/unreadable document is not "absent" — it
        propagates as ``StatusStoreError`` (#112) rather than inviting a re-add over it."""
        run = self.store.load_run(run_id)
        return {
            ref.task_id
            for ref in run.task_refs
            if self._partial_registration_reason(run_id, ref.task_id) is None
        }

    # --- dispatchable (DAG + lease) ------------------------------------------
    def dispatchable(self, run_id: str) -> list[str]:
        """The canonical dispatch-eligibility set: every non-terminal task whose deps
        are all COMPLETED and which holds no outstanding dispatch lease.

        This is the ONE eligibility predicate (the scheduler delegates here). It is
        deliberately NOT capacity-limited — the capacity cap (``dispatch_limit``) is a
        separate, orthogonal decision applied by the caller (Scheduler.tick), so the
        eligibility set and the throttle don't get tangled.

        Semantics chosen over the old ``ready()`` (PENDING/BLOCKED-only, lease-ignoring),
        which was wrong for the scheduler: it excluded RETRYING tasks (a retry must be
        re-dispatched) and ignored the dispatch lease (would re-pick an in-flight task).
        """
        self._resume_pending_decompositions(run_id)
        self._complete_ready_umbrellas(run_id)
        run = self.store.load_run(run_id)
        if run.state in (RunState.PAUSED, RunState.PARKED):
            return []
        states = {ref.task_id: ref.state for ref in run.task_refs}
        dag = Dag(run.dependency_graph)
        out: list[str] = []
        for ref in run.task_refs:
            if states[ref.task_id] in TERMINAL_TASK_STATES:
                continue
            # Held at a human gate: non-terminal (keeps the run open) but never
            # dispatched until Engine.approve() releases it (design pass §4).
            if states[ref.task_id] is TaskState.BLOCKED_ON_HUMAN:
                continue
            if dag.unmet_deps(ref.task_id, states):
                continue
            doc = self.store.load_task(run_id, ref.task_id)
            if doc.decomposition_children:
                continue
            # A task holding a dispatch lease (in-flight, or crashed mid-stage) is not
            # re-dispatchable on the normal path — it needs explicit resume, never a
            # silent re-pick that would overwrite the outstanding WorkItem.
            if doc.pending_work_item_id is not None:
                continue
            # Parked in a rate-limit cooldown: not dispatchable until the stamp elapses
            # (the scheduler sleeps on the soonest cooldown instead of spinning).
            if _in_future(doc.not_before):
                continue
            out.append(ref.task_id)
        return out

    def in_flight(self, run_id: str) -> list[str]:
        """Tasks with an outstanding dispatch lease (RUNNING, ``pending_work_item_id``
        set) — one Workflow invocation is currently out for each.

        Read-only. The per-task supervisor subtracts this from the capacity ``limit``
        to size remaining headroom (``limit - len(in_flight)``) so concurrency binds
        across concurrently-live background invocations, not just within one round —
        and correctly after a resume, since a crash leaves the lease held and this
        re-counts it. Complements ``dispatchable`` (which EXCLUDES leased tasks): the
        two sets are disjoint, and their split is the whole point — you cannot see
        remaining capacity from ``dispatchable`` alone once dispatches overlap.
        """
        run = self.store.load_run(run_id)
        out: list[str] = []
        for ref in run.task_refs:
            if ref.state in TERMINAL_TASK_STATES:
                continue
            doc = self.store.load_task(run_id, ref.task_id)
            if doc.pending_work_item_id is not None:
                out.append(ref.task_id)
        return out

    # --- dispatch / record ----------------------------------------------------
    def next_work(
        self,
        run_id: str,
        task_id: str,
        *,
        util_pct: float = 0.0,
        resume: bool = False,
        supervisor_context: SupervisorContext | None = None,
        supervisor_min_remaining_pct: float = DEFAULT_MIN_REMAINING_PCT,
        supervisor_resume_command: str | None = None,
    ) -> WorkItem | None:
        """Emit the task's next dispatchable WorkItem, or None when there is nothing to
        dispatch (terminal/parked task, decomposition umbrella, budget pause, or pipeline
        exhausted). Before selecting a stage, a normal dispatch resumes any approved,
        partially-filed SCOPE decomposition so its parent cannot advance into IMPLEMENT;
        completed umbrellas leave execution to their children on the run DAG.

        The single dispatch-resolution point: picks the stage, routes the lane
        (deterministic stages -> the in-process ENGINE lane), and resolves BOTH routing
        dimensions of a model-lane dispatch — the model (rate-limit fallback > model_pin >
        role default) and, since #96, the reasoning effort (effort_pin > stage-spec
        default > None = provider default). On a route_by_capacity run in the DOWNGRADE
        band it degrades a fresh dispatch by the cheaper lever first: effort down one step
        (emitting ``effort_downgraded``) and only then the model (``model_downgraded``);
        per-lever pins are exempt. The band edge is effort-aware (#155, closing the #96/#141
        loop): a (stage, effort) group whose ledger history retries/fails more gets a
        smaller, less-eager DOWNGRADE band (the ledger read is confined to the band-edge
        util region and gated on a minimum sample). Emitting stamps the dispatch lease
        (``pending_work_item_id``); ``resume=True`` re-emits an outstanding lease after a
        supervisor crash instead. When the resolved stage matches ``Task.hold_before``,
        dispatch parks until both the matching approval identity and durable artifact are
        present.

        For an interactive, fresh model dispatch, supplying ``supervisor_context`` adds a
        final pre-lease gate after the exact prompt has been rendered. It reserves
        ``supervisor_min_remaining_pct`` of the window plus a conservative prompt cost.
        Insufficient or unavailable context parks the run with
        ``supervisor_resume_command`` before any prompt artifact, event, or lease is
        written. This decision and each fresh lease commit are serialized per run, so a
        parked run cannot gain a concurrent lease. If other task leases are still live, it
        instead raises
        ``SupervisorParkDeferred`` so the caller can drain them to a safe boundary.
        ``resume=True`` never applies this gate because it recovers an existing lease.

        Raises ``CapacityExhausted`` at the per-call gate or during a rate-limit cooldown,
        ``ContractError`` on a lease conflict, and ``SupervisorParkDeferred`` when a
        requested park must wait for in-flight work.
        """
        # Budget backpressure (#34): consult the run's metered spend against its budget at
        # this dispatch point. Once spend >= budget, do NOT dispatch new work — PAUSE the
        # run (reusing the PAUSED/unpause machinery) and return None; in-flight recorded
        # state is untouched. Gated on a budget being set (default off) and skipped on a
        # resume (that recovers an ALREADY-dispatched, already-charged lease). Placed HERE,
        # at the single dispatch point, so it catches BOTH the scheduler loop AND the
        # single-task engine-lane drain (mirrors the alerting seam's one-layer rationale).
        run = self.store.load_run(run_id)
        if not resume and run.state in (RunState.PAUSED, RunState.PARKED):
            return None
        if not resume and self._budget_hard_stop(run_id):
            return None
        # Capacity backpressure: at the per-call gate, no new dispatch (the caller waits).
        # This gates EVERY path — including a rate-limit re-queue — so graceful fallback
        # never over-subscribes an already-saturated API.
        if self.capacity.at_capacity(util_pct):
            raise CapacityExhausted(f"at capacity ({util_pct}% >= per-call gate)")
        task = self.store.load_task(run_id, task_id)
        # A terminal task has no more work — never re-emit its (failed/completed) stage.
        # Without this, a caller that loops on next_work (the CLI `next` drain) would
        # re-dispatch a FAILED task forever; the scheduler is safe because `dispatchable`
        # filters terminal states, but next_work must be self-safe for direct callers.
        if task.state in TERMINAL_TASK_STATES:
            return None
        # A task parked at the human gate (approval hold or scope-not-feasible, issue #45)
        # is non-terminal but quiescent: it must not be dispatched until approve() releases
        # it to PENDING. The scheduler is safe because `dispatchable` filters it, but — as
        # with the terminal guard above — next_work must be self-safe for direct callers.
        if task.state is TaskState.BLOCKED_ON_HUMAN:
            return None
        # Direct callers (notably the CLI ``next`` drain) bypass ``dispatchable()``, which
        # normally reconciles a SCOPE decomposition saga before selecting work. Resume an
        # approved/partially-filed saga here as well, before ``next_stage`` can incorrectly
        # advance the umbrella into IMPLEMENT. A completed decomposition is never itself
        # dispatchable; only its children run on the task-level DAG.
        if not resume:
            scope = task.stages.get(Stage.SCOPE)
            output = scope.output if scope and scope.status is StageStatus.COMPLETED else None
            if (
                not task.decomposition_children
                and isinstance(output, dict)
                and output.get("subtasks") is not None
            ):
                task = self._apply_scope_decomposition(run_id, task, output)
            if task.decomposition_children or task.state is TaskState.BLOCKED_ON_HUMAN:
                return None
        # Rate-limit cooldown: the task is parked until the window resets — refuse
        # dispatch loudly (the caller waits/sleeps), never silently. Explicit resume
        # bypasses (a human who knows better can force it).
        if not resume and _in_future(task.not_before):
            raise CapacityExhausted(f"rate-limit cooldown until {task.not_before}")
        # Explicit resume is ONLY for recovering an outstanding dispatch lease (a crashed
        # supervisor re-emitting its held WorkItem, #50). With nothing leased there is
        # nothing to recover — refuse loudly rather than silently minting a fresh dispatch
        # that could double-run a stage whose original supervisor is still alive.
        if resume and task.pending_work_item_id is None:
            raise ContractError(
                f"task {task_id} has no outstanding dispatch to resume (nothing leased); "
                f"drop resume to dispatch the next stage normally"
            )
        # pending_work_item_id is a dispatch lease: while a WorkItem is outstanding the
        # task is NOT re-dispatchable on the normal path. A crash leaves the lease held,
        # so recovery is the explicit resume=True path — never a silent re-dispatch that
        # would overwrite the lease and make the in-flight result fail contract checks.
        if task.pending_work_item_id is not None and not resume:
            raise ContractError(
                f"task {task_id} has an outstanding dispatch {task.pending_work_item_id}; "
                f"record its result or re-dispatch with resume=True"
            )
        stage = next_stage(task)
        if stage is None:
            return None
        if task.hold_before is stage:
            gate = f"before:{stage.value}"
            approval = self.store.load_approval(run_id, task_id)
            gate_is_approved = (
                gate in task.approved_holds
                and isinstance(approval, dict)
                and approval.get("what") == gate
            )
            if not gate_is_approved:
                self.hold_for_approval(run_id, task_id, what=gate)
                self.emit_notification(
                    run_id, "task_blocked",
                    {"run_id": run_id, "task_id": task_id, "kind": "task_blocked",
                     "summary": f"task {task_id} is held before {stage.value}",
                     "stage": stage.value, "reason": gate},
                )
                return None

        spec = STAGE_SPECS[stage]
        rec = task.stages[stage]
        run = self.store.load_run(run_id)  # refresh after the budget gate may mutate it
        # Deterministic routing: a stage is run by the in-process ENGINE-lane shell runner
        # (no model call, $0) when it is globally deterministic (intake) OR the task/pipeline
        # opted it in via `deterministic_stages` (#33: TEST/DELIVER). ONE decision, two
        # sources — never a second selection mechanism.
        deterministic = spec.deterministic or stage in task.deterministic_stages
        # ...plus a VETO, which is a different kind of thing and so is not a third source of
        # the choice above: the model lane this stage would otherwise take cannot perform it
        # (#364 — codex's sandbox turns DELIVER's `git push` into an unanswerable GUI keychain
        # prompt). The caller cannot opt out of a capability it does not have, so the veto
        # overrides an explicitly model-pinned stage; it is evented rather than applied
        # silently, because a run that quietly stopped using the lane it was told to use is
        # exactly the kind of drift `lane_audit` exists to catch.
        reroute_reason: str | None = None
        if not deterministic:
            reroute_reason = engine_lane_required(stage, self.router.lane_for(stage, task))
            deterministic = reroute_reason is not None
        # Resolved to an Effort (from the task pin or stage spec) or downshifted via
        # effort_below below — always an Effort member or None (#161/#202 narrowed the
        # transitional ``str | Effort | None``). StrEnum, so it flows identically into the
        # WorkItem/hash/events (#172).
        effort: Effort | None = None
        if deterministic:
            # No model: route to the in-process ENGINE lane (a shell runner does the work).
            # Don't ask an LLM to run `git worktree add` / `gh pr create`.
            # No model also means no effort — the ENGINE lane has nothing to throttle.
            lane = LanePolicy(
                execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False
            )
            model = ENGINE_MODEL
            if reroute_reason is not None:
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "stage_rerouted_to_engine_lane", "run_id": run_id,
                     "task_id": task_id, "stage": stage.value, "level": "warning",
                     "reason": reroute_reason,
                     "from": self.router.lane_for(stage, task).provider.value},
                )
        else:
            lane = self.router.lane_for(stage, task)  # execution_mode × provider (§4)
            # Model resolution precedence for a model-lane stage (highest first):
            #   1. pending_fallback_model — a rate-limit re-queue already picked the cheaper
            #      model; it MUST win so the degrade is never undone (fable→opus stays opus).
            #   2. task.model_pin (#84) — an explicit per-task pin overrides the role default so
            #      a heavy-architecture task runs on its pinned tier (e.g. claude-fable-5). The
            #      pin is a STARTING tier, not an anti-fallback lock — the rate-limit chain still
            #      degrades down from it (fable→opus→…) when lane.allow_fallback permits.
            #   3. the role default for this provider (opus/sonnet/haiku).
            model = (
                task.pending_fallback_model
                or task.model_pin
                or self.models.model_for_role(spec.model_role, lane.provider)
            )
            # Effort resolution (#96), mirroring the model precedence: an explicit
            # per-task effort pin overrides the stage-spec default (scope/implement
            # high, test/review medium, deliver low). None (a spec without a default)
            # means "provider default" and emits exactly today's dispatch. No ``.value``
            # extraction (#172): Effort is a StrEnum, so it hashes/serializes/compares
            # as its "high" value everywhere downstream (WorkItem, events, argv).
            effort = task.effort_pin or spec.effort
            # Capacity-aware downgrade (#12/#96): on a route_by_capacity run, when CURRENT
            # util is in the high band (>= downgrade_threshold, and < the per-call gate that
            # already blocked dispatch above), degrade a FRESH dispatch to keep progressing
            # under load. DISTINCT from cost routing (headroom, not USD). Lever ORDERING
            # (#96): effort is the cheaper dial, so downshift effort ONE step first
            # (high -> medium -> low) and only downgrade the MODEL when the effort lever is
            # unavailable (already at the low floor, unset, or pinned). Pins are honored
            # per-lever: an effort_pin exempts effort, a model_pin exempts the model (#84 —
            # an explicit choice is honored, same rule as a pipeline/lane pin; the rate-limit
            # chain may still degrade a pinned model, capacity may not). Skipped for a
            # rate-limit re-queue (``pending_fallback_model`` already picked the cheaper
            # model — never double-drop; the rate-limit floor/cooldown logic stays intact)
            # and when the lane disallows fallback. Never silent: every applied drop emits
            # ``effort_downgraded`` / ``model_downgraded``.
            if (
                run.route_by_capacity
                and task.pending_fallback_model is None
                and lane.allow_fallback
            ):
                # Effort-aware adaptive band (#155, closes the #96/#141 loop): the flat
                # downgrade_threshold becomes a per-(stage, effort) edge driven by empirical
                # retry/failure history — a group that historically retries when downshifted
                # gets a SMALLER band (higher edge) so it keeps full effort longer. The
                # ledger read is confined to the band-edge region [base_edge, per_call): below
                # base_edge it's NORMAL and above per_call it's WAIT (blocked above) no matter
                # how the edge moves, so raising it can only flip DOWNGRADE->NORMAL there —
                # everywhere else the flat threshold suffices and we skip the read.
                threshold = self.capacity.downgrade_threshold
                observed_rate = sample_size = None
                if (
                    self.capacity.adaptive_band
                    and self.capacity.downgrade_threshold
                    <= util_pct
                    < self.capacity.per_call_threshold
                ):
                    observed_rate, sample_size = self._observed_downshift_rate(stage, effort)
                    if (
                        observed_rate is not None
                        and sample_size is not None
                        and sample_size >= ADAPTIVE_BAND_MIN_SAMPLE
                    ):
                        threshold = self.capacity.effort_downgrade_threshold(observed_rate)
                    else:  # sparse/no history -> fall back to today's flat-threshold behavior
                        observed_rate = sample_size = None
                if self.capacity.dispatch_band(util_pct, threshold) is DispatchBand.DOWNGRADE:
                    # Audit fields shared by both levers so an adaptive downshift is never
                    # silent and the loop is inspectable: the observed rate + sample that
                    # moved the edge (None when sparse/disabled) and the EFFECTIVE threshold.
                    _adaptive = {"observed_rate": observed_rate, "sample_size": sample_size,
                                 "downgrade_threshold": threshold}
                    lowered = effort_below(effort) if task.effort_pin is None else None
                    if lowered is not None:
                        self.store.append_event(
                            run_id,
                            {"ts": _now(), "type": "effort_downgraded", "run_id": run_id,
                             "task_id": task_id, "stage": stage.value, "util_pct": util_pct,
                             "from": effort, "to": lowered, **_adaptive},
                        )
                        effort = lowered
                    elif task.model_pin is None:
                        downgraded = self._capacity_downgrade(model)
                        if downgraded != model:
                            self.store.append_event(
                                run_id,
                                {"ts": _now(), "type": "model_downgraded", "run_id": run_id,
                                 "task_id": task_id, "stage": stage.value, "util_pct": util_pct,
                                 "from": model, "to": downgraded, **_adaptive},
                            )
                            model = downgraded
        # Attempt is derived from the persisted stage status, not rec.error:
        #  - RUNNING  -> a crash OR a rate-limit re-queue; re-dispatch the SAME attempt
        #  - FAILED   -> a real retry; bump
        #  - else     -> first attempt
        if rec.status is StageStatus.RUNNING:
            attempt = rec.attempt
        elif rec.status is StageStatus.FAILED:
            attempt = rec.attempt + 1
        else:
            attempt = 0
        # Checkpoint protocol (design pass §3): the engine only NAMES the tag and picks
        # the reset anchor — the runner-side wrapper does the git I/O. Tags include the
        # run id (open Q3: yes — bench replays will recur task ids across runs).
        checkpoint_tag = reset_to = salvage_anchor = None
        if spec.checkpoint:
            checkpoint_tag = (
                f"task/{_ref_safe(run_id)}/{_ref_safe(task_id)}/{stage.value}/{attempt}"
            )
            # Anchor = last SUCCESSFUL checkpoint. Always handed to the runner (salvage_anchor)
            # so a failed attempt can report the commits it made past it (#59), independent
            # of whether this dispatch resets.
            anchor = task.last_checkpoint.get("tag") if task.last_checkpoint else None
            salvage_anchor = anchor
            # Reset only a retry (FAILED) or crash-resume (RUNNING) — a first attempt
            # starts from a clean tree by construction — AND only when the prior attempt's
            # work isn't being salvaged. When salvage_in_place is set, the committed work is
            # KEPT (no reset) so the retry builds on it; the flag is consumed at commit.
            if (
                rec.status in (StageStatus.FAILED, StageStatus.RUNNING)
                and anchor
                and not task.salvage_in_place
            ):
                reset_to = anchor
        # Cross-run KB fold (#72): at the FIRST pipeline stage (intake — the honest point,
        # before any code exists), recall relevant PRIOR learnings and fold them into the
        # context plane under ``prior_learnings``. Once only per task (guarded on absence),
        # so a crash-resume/retry of the first stage doesn't re-query. Read-only advisory
        # context; it renders (hedged) into this and every later stage's prompt.
        prior_learnings: list[str] | None = None
        if (
            self._learnings_kb_enabled()
            and task.pipeline
            and stage is task.pipeline[0]
            and "prior_learnings" not in task.context
        ):
            prior_learnings = self._recall_prior_learnings(task, stage)
            if prior_learnings:
                task.context["prior_learnings"] = prior_learnings
        learnings = "\n".join(task.learnings)
        # Tool posture (#272): declared by the stage spec, attached on model lanes only (the
        # deterministic ENGINE lane runs no model, so there is no toolset to narrow). Purely
        # derived — nothing run-level to persist (#206), so re-reading the spec at every
        # dispatch boundary is the whole mechanism.
        tool_policy = None if deterministic else spec.tool_policy
        # Honesty gate (#272): a lane that cannot translate the posture into a real provider
        # restriction (the interactive shim — its `agent()` call takes no tool argument) still
        # gets the policy on the WorkItem, but declared-but-unenforced must never be silent.
        # Resolved BEFORE the prompt render because #302 made it prompt-affecting: on such a
        # lane the posture is stated in-band, which is the only enforcement available there.
        unenforced_policy = tool_policy is not None and not self._lane_enforces_tool_policy(lane)
        # Permission posture (#304): the lane's declared default, tightened to RESTRICTED by a
        # write-denying stage posture — so `--dangerously-skip-permissions` is a decision made
        # here from (lane, stage), not a constant in the transport. None on the deterministic
        # lane, which runs no model and therefore has no permission gate to set.
        permission_posture = None if deterministic else self._permission_posture(lane, tool_policy)
        prompt = render_prompt(
            stage,
            task_id=task_id,
            title=task.title,
            body=task.body,
            learnings=learnings,
            context=task.context,
            project_commands=self._project_commands(),
            tool_posture_unenforced=unenforced_policy,
        )
        role = task.agent_role if stage is Stage.IMPLEMENT and task.agent_role else spec.agent_role
        agent = self.project.agent_for(stage, role)
        # Multi-agent REVIEW (#73): one gate, consulted only for a model-lane REVIEW. It
        # re-reads the opt-in off the loaded Run doc and applies the lane/preset/capacity/
        # budget vetoes; None (the default, and every other stage) dispatches the byte-
        # identical plan-less path.
        plan = (
            None
            if deterministic
            else self._review_plan_for(task, stage=stage, run=run, lane=lane, util_pct=util_pct)
        )
        work = WorkItem.create(
            id=f"wi-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            task_id=task_id,
            stage=stage,
            prompt=prompt,
            schema_ref=spec.schema_ref,
            # #161 shipped ModelId (open NewType) but #194's mypy gate merged from a
            # sibling worktree that never saw it, so the trunk needs this cast (identity
            # at runtime). `model` is resolved from str sources (role default / pins).
            model=ModelId(model),
            effort=effort,
            agent=agent,
            lane_policy=lane,
            created_at=_now(),
            attempt=attempt,
            timeout_s=(
                _IMPLEMENT_TIMEOUTS[task.implementation_budget]
                if stage is Stage.IMPLEMENT and task.implementation_budget is not None
                else spec.timeout_s
            ),
            # Run in the task's worktree (folded from intake) so the headless lane stops
            # depending on process CWD. None on intake itself (it creates the worktree).
            cwd=task.context.get("worktree"),
            # Chain the task's provider session (design pass §2). None on the first
            # stage and after a failure (warm retry is deliberately off); a crash-
            # resume passes whatever ref survives — a dead session is the transport's
            # fallback-to-fresh, never a correctness problem. Provider-gated (#9): a
            # session id is provider-specific, so it only rides a stage whose lane
            # provider matches the ref's owner (NONE = untagged wildcard) — a claude ref
            # is never handed to codex or the reverse.
            session_ref=(
                task.session_ref
                if task.session_provider in (None, Provider.NONE, lane.provider)
                else None
            ),
            checkpoint_tag=checkpoint_tag,
            reset_to=reset_to,
            salvage_anchor=salvage_anchor,
            # Deterministic ENGINE-lane runners (intake/test/deliver) read task context
            # structurally rather than re-parsing their own rendered prompt; model lanes
            # get None (they read the prompt). Same durable state, so it is hash-excluded.
            context=(
                self._deterministic_context(task, stage=stage, run=run)
                if deterministic
                else None
            ),
            # #5: the task's per-task port block, exported into the stage subprocess (BOTH
            # the model CLI and the deterministic test runner) so parallel worktrees don't
            # collide on dev/test-server ports. None until intake has allocated a block (and
            # for projects that don't opt in) — a clean no-op there. Hash-excluded: derived
            # from the same durable context (port_base) the prompt is.
            env=self._port_env_for_task(task),
            # #73: CONTENT, so it folds into content_hash (unlike cwd/context/env above).
            # None on every non-workflow dispatch — which is what keeps the plan-less
            # WorkItem/prompt/hash byte-identical to the pre-#73 shape.
            plan=plan,
            # #272: dispatch metadata like cwd/env — hash-excluded (see the field docstring).
            tool_policy=tool_policy,
            permission_posture=permission_posture,  # #304, hash-excluded for the same reason
        )
        # #314: persist the prompt that produced this dispatch, and fingerprint it, BEFORE
        # the commit below — evidence-first, matching commit_task_events' events-before-doc
        # ordering, so a `stage_dispatched` never names a prompt file that isn't on disk.
        # (The reverse order could; a crash here instead leaves an orphan prompt file for a
        # dispatch that never committed, which is inert.) The hash is over the FULL prompt,
        # not the possibly-capped file, so cross-stage prefix-drift comparison — the whole
        # point of recording it — is unaffected by any truncation.
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        prompt_dropped: int | None = None
        prompt_file = prompt_relpath(task_id, stage.value, attempt)
        # Commit the dispatch as a locked read-modify-write: re-check the lease and
        # that the stage hasn't advanced under us, so two concurrent next_work calls
        # can't both claim the task — the loser sees the moved lease/stage and raises.
        # On a resume, the outstanding lease is superseded by this fresh WorkItem
        # (a new work.id); capture the old id under the lock (#142) so the timeline can
        # be made self-describing — its `stage_dispatched` will never get a matching
        # `stage_recorded`, so a naive counter would over-count dispatches without it.
        superseded_lease: str | None = None

        def _commit(t: Task) -> None:
            nonlocal superseded_lease
            if t.pending_work_item_id is not None and not resume:
                raise ContractError(
                    f"task {task_id} dispatch raced: lease {t.pending_work_item_id} taken"
                )
            if next_stage(t) is not stage:
                raise ContractError(f"task {task_id} stage advanced under dispatch of {stage.value}")
            if resume and t.pending_work_item_id is not None and t.pending_work_item_id != work.id:
                superseded_lease = t.pending_work_item_id
            begin_stage(t, stage, now=_now(), model=model, attempt=attempt, effort=effort)
            t.state = TaskState.RUNNING
            t.pending_work_item_id = work.id
            t.pending_content_hash = work.content_hash
            # #288: remember that THIS dispatch carried a panel plan, so record() can tell a
            # runner that ignored it (no sub_results) from a review nobody asked a panel of.
            t.pending_plan = work.plan is not None
            t.pending_fallback_model = None  # consumed into this dispatch's model
            t.not_before = None  # cooldown (if any) has elapsed — clear the stamp
            t.salvage_in_place = False  # consumed: this dispatch honored (or ignored) it
            # Persist the intake KB fold (#72) into the durable context so it survives on
            # disk and renders in the later stages' prompts too (not just this dispatch's).
            if prior_learnings and "prior_learnings" not in t.context:
                t.context["prior_learnings"] = prior_learnings

        # #174: commit the task mutation and its bookkeeping events transactionally —
        # events appended FIRST, the task doc written LAST as the single durable commit
        # point — so a crash can never leave the task claiming a dispatch (pending lease
        # set / lease superseded) without the matching `stage_dispatched` (and, on a
        # resume, `lease_superseded`) already on disk. Events are built after `_commit`
        # runs so they can read `superseded_lease`, which the mutator captures under the
        # lock.
        def _dispatch_events(_t: Task) -> list[dict]:
            evs: list[dict] = []
            # #142: when a resume supersedes an outstanding lease, retire the old
            # work_item_id with its own event FIRST — so a consumer scanning the timeline
            # sees the superseded dispatch closed out before the re-dispatch, and never
            # pairs a live `stage_recorded` against the stale lease.
            if superseded_lease is not None:
                evs.append(
                    {
                        "ts": _now(),
                        "type": "lease_superseded",
                        "run_id": run_id,
                        "task_id": task_id,
                        "stage": stage.value,
                        "attempt": attempt,
                        "work_item_id": superseded_lease,
                        "superseded_by": work.id,
                    }
                )
            event: dict = {
                "ts": _now(),
                "type": "stage_dispatched",
                "run_id": run_id,
                "task_id": task_id,
                "stage": stage.value,
                "attempt": attempt,
                "model": model,
                "effort": effort,
                "agent": agent,
                "work_item_id": work.id,
                # #314: the cost-shaping inputs of THIS call. `session_ref` is read off the
                # WorkItem, i.e. exactly what the transport is handed — so a #9
                # provider-mismatch suppression shows as null here rather than the ref the
                # task still holds, and the event describes what was SENT, not what was
                # computed. Unconditional (null when absent), unlike the #142/#288 markers:
                # a field that is only present sometimes cannot be aggregated, and its
                # absence is precisely what made continuity read as 0% across a whole run.
                "session_ref": work.session_ref,
                "prompt_sha256": prompt_sha256,
                "prompt_chars": len(prompt),
                "prompt_file": prompt_file,
            }
            # Self-describing re-dispatch (#142): stamp the resume marker and the lease it
            # supersedes so a reader can tell a genuine crash-recovery re-lease from a
            # fresh dispatch without joining on `work_item_id` against `stage_recorded`.
            if superseded_lease is not None:
                event["resume"] = True
                event["supersedes"] = superseded_lease
            # #288: a plan-bearing REVIEW dispatch says so in the timeline, with the lens set
            # it asked for — so "was a panel requested here?" is answerable from events.jsonl
            # alone, and the matching `review_plan_not_executed` (if any) has an opening half.
            # Conditional so every plan-less dispatch event stays byte-identical.
            if work.plan is not None:
                event["plan"] = True
                event["plan_lenses"] = [f.lens for f in work.plan.finders]
            evs.append(event)
            # #314: the persisted prompt was capped, so the `.prompt.txt` is not the whole
            # input. Warning-grade and explicit about how much was cut — the "never silent"
            # convention: the writer returned what it dropped, this call site emits it.
            if prompt_dropped is not None:
                evs.append(
                    {
                        "ts": _now(),
                        "type": "stage_prompt_truncated",
                        "severity": "warning",
                        "run_id": run_id,
                        "task_id": task_id,
                        "stage": stage.value,
                        "attempt": attempt,
                        "work_item_id": work.id,
                        "prompt_file": prompt_file,
                        "prompt_chars": len(prompt),
                        "written_chars": len(prompt) - prompt_dropped,
                        "dropped_chars": prompt_dropped,
                    }
                )
            # #272: ONE warning-grade notice per dispatch when the resolved lane does not
            # declare enforcement, so a read-only posture that is only a prompt convention on
            # that lane is visible in the event stream instead of quietly assumed.
            if unenforced_policy:
                evs.append(
                    {
                        "ts": _now(),
                        "type": "tool_policy_unenforced",
                        "severity": "warning",
                        "run_id": run_id,
                        "task_id": task_id,
                        "stage": stage.value,
                        "attempt": attempt,
                        "work_item_id": work.id,
                        "lane": f"{lane.execution_mode.value}:{lane.provider.value}",
                        "policy": tool_policy.model_dump() if tool_policy else None,
                    }
                )
            return evs

        def _locked_dispatch() -> bool:
            """Guard and commit one dispatch while excluding run-level parking."""
            nonlocal prompt_dropped

            # A caller may have rendered this prompt before another caller parked the run.
            # Re-check under the same run-level lock used by park_supervisor so no fresh
            # lease can commit after PARKED becomes durable.
            locked_run = self.store.load_run(run_id)
            if not resume and locked_run.state in (RunState.PAUSED, RunState.PARKED):
                return False

            # Interactive supervisor context gate (#259). The exact engine-rendered prompt
            # is already in memory, but no prompt artifact/event/WorkItem has crossed the
            # process boundary and no dispatch lease exists yet. The guard and possible
            # PARKED transition share this critical section with every fresh lease commit,
            # making this stage-boundary decision atomic across concurrent next_work calls.
            # Deterministic/headless dispatches do not traverse supervisor context, and a
            # crash resume already owns a lease, so neither is context-gated.
            if (
                supervisor_context is not None
                and not resume
                and lane.execution_mode is ExecutionMode.INTERACTIVE
            ):
                projection = supervisor_context.projected(
                    prompt, min_remaining_pct=supervisor_min_remaining_pct
                )
                if projection["should_park"]:
                    in_flight = self.in_flight(run_id)
                    if in_flight:
                        raise SupervisorParkDeferred(in_flight, projection)
                    reason = (
                        "supervisor context sensor unavailable"
                        if not projection["available"]
                        else "remaining supervisor context cannot safely carry the next "
                        "engine-rendered prompt"
                    )
                    self._park_supervisor_locked(
                        run_id,
                        reason=reason,
                        resume_command=(
                            supervisor_resume_command
                            or f"orchestrator --run {run_id} resume-supervisor"
                        ),
                        context=projection,
                    )
                    return False

            _, prompt_dropped = self.store.write_stage_prompt(
                task_id, prompt_filename(stage.value, attempt), prompt
            )
            self.store.commit_task_events(run_id, task_id, _commit, _dispatch_events)
            return True

        with self.store.with_dispatch_lock(run_id):
            committed = _locked_dispatch()
        if not committed:
            return None
        self._set_ref_state(run_id, task_id, TaskState.RUNNING)
        return work

    def _lane_enforces_tool_policy(self, lane: LanePolicy) -> bool:
        """Does the resolved lane translate a ``ToolPolicy`` into a real restriction (#272)?

        Read off the lane's ``CapabilityDescriptor`` (``enforces_tool_policy``), never
        hardcoded here — the transport that does the translation is the only honest source.
        An unregistered lane counts as NOT enforcing: the conservative reading, because it
        makes the gap visible rather than assuming protection the lane may not have."""
        try:
            return self.registry.describe(lane).enforces_tool_policy
        except NoRunnerError:
            return False

    def _permission_posture(
        self, lane: LanePolicy, tool_policy: ToolPolicy | None
    ) -> PermissionPosture:
        """The permission gate this dispatch runs under (#304): the LANE's declared default,
        tightened to RESTRICTED by a write-denying STAGE posture.

        Only ever tightened, never loosened — a lane that declares RESTRICTED keeps it for
        every stage, while a lane that declares BYPASS still loses blanket permission on a
        read-only stage. An unregistered lane falls back to BYPASS: it is today's behavior for
        a lane whose descriptor states nothing, and it cannot mask a gap, because such a lane
        has no runner and fails at dispatch anyway (unlike ``_lane_enforces_tool_policy``,
        where the conservative reading is what makes the gap visible)."""
        if tool_policy is not None and not tool_policy.allow_file_writes:
            return PermissionPosture.RESTRICTED
        try:
            return self.registry.describe(lane).permission_posture
        except NoRunnerError:
            return PermissionPosture.BYPASS

    # --- multi-agent REVIEW plan (#73) -----------------------------------------
    def _deterministic_diff_stat(self, task: Task) -> DiffStat | None:
        """Measure the change under review with a bounded, best-effort ENGINE-lane git read
        (``git diff --numstat <base_sha>..HEAD`` in the task's worktree).

        This is the ONLY size signal the finder-set relaxation ladder may consult, precisely
        because it is the engine's own measurement rather than anything a model reported about
        its own work (an implementer that under-reports ``files_changed`` must not be able to
        talk itself into a thinner review — design §1's ``DETERMINISTIC_ONLY_KEYS`` rule).

        Returns None on ANY error, missing input, or unreadable output, and the caller then
        applies NO relaxation — failing toward the full panel, i.e. more scrutiny. Called only
        while building a plan, so the plan-less path performs zero extra I/O."""
        base_sha = task.context.get("base_sha")
        worktree = task.context.get("worktree")
        if not (base_sha and worktree and Path(str(worktree)).is_dir()):
            return None
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", "diff", "--numstat", f"{base_sha}..HEAD"],  # noqa: S607
                cwd=str(worktree), capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        files = lines = 0
        changed_files: list[str] = []
        for row in proc.stdout.splitlines():
            cols = row.split("\t")
            if len(cols) < 3:
                continue
            files += 1
            changed_files.append("\t".join(cols[2:]))
            # A binary file reports "-\t-\t<path>": it counts as a touched FILE but adds no
            # line count — never inflate, never crash on the sentinel.
            for col in cols[:2]:
                if col.isdigit():
                    lines += int(col)
        return DiffStat(files=files, lines=lines, changed_files=tuple(changed_files))

    def _review_plan_for(
        self,
        task: Task,
        *,
        stage: Stage,
        run: Run,
        lane: LanePolicy,
        util_pct: float,
    ) -> ReviewPlan | None:
        """The single gate deciding whether THIS dispatch carries a multi-agent REVIEW plan.

        Consulted only for a model-lane REVIEW stage. The opt-in is re-read from the LOADED
        Run doc (``run.review_workflow``), never from engine memory — every CLI subcommand
        rebuilds the Engine from constructor defaults, so a create-time-only setting would be
        gone by the next subcommand (#206).

        Vetoes, in order (each emits ``review_workflow_skipped`` with its reason, so a run
        that opted in never silently gets the single-reviewer path):

        - the resolved lane's descriptor does not declare ``supports_plan`` (codex has no
          sub-agent primitive) — the plan is in ``content_hash``, so lane and hash must agree;
        - the task's lane preset is MICRO/LITE (they exist to be cheap);
        - capacity says this dispatch is not in the NORMAL band (under load, one reviewer);
        - the run's remaining budget has thinned past the cost router's top band.

        Returns the rendered plan, or None to dispatch the byte-identical plan-less path."""
        if stage is not Stage.REVIEW or not run.review_workflow:
            return None

        def _veto(reason: str, **extra: object) -> None:
            self.store.append_event(
                run.run_id,
                {"ts": _now(), "type": "review_workflow_skipped", "run_id": run.run_id,
                 "task_id": task.task_id, "stage": stage.value, "reason": reason, **extra},
            )

        try:
            supports_plan = self.registry.describe(lane).supports_plan
        except NoRunnerError:
            supports_plan = False
        if not supports_plan:
            _veto("lane_cannot_execute_plan",
                  lane=f"{lane.execution_mode.value}:{lane.provider.value}")
            return None
        if task.execution_lane in (ExecutionLane.MICRO, ExecutionLane.LITE):
            _veto("cheap_lane_preset", execution_lane=task.execution_lane.value)
            return None
        if self.capacity.dispatch_band(util_pct, self.capacity.downgrade_threshold) is not (
            DispatchBand.NORMAL
        ):
            _veto("capacity_band", util_pct=util_pct)
            return None
        remaining = self._remaining_budget_fraction(run)
        if self.cost_router.route(remaining).lane is not ExecutionLane.FULL:
            _veto("budget_thinning", remaining_fraction=round(remaining, 4))
            return None
        return render_review_plan(
            task_id=task.task_id,
            title=task.title,
            body=task.body,
            learnings="\n".join(task.learnings),
            context=task.context,
            project_commands=self._project_commands(),
            agent_for=self.project.agent_for,
            # ENGINE-lane deterministic relaxation signals ONLY, passed explicitly so
            # render_review_plan never has to reach into the model-writable context for them.
            change_class=(
                "docs-only" if task.context.get("change_class") == "docs-only" else None
            ),
            diff_stat=self._deterministic_diff_stat(task),
        )

    @staticmethod
    def _lease_mismatch(
        task: Task, run_id: str, result: StageResult
    ) -> tuple[str, str] | None:
        """PURE lease check: ``(reason_code, message)`` when ``result`` does not answer the
        task's outstanding dispatch, else ``None``. No I/O, no events (#311).

        The check RETURNS its rejection instead of raising it so the refusal can be BOTH
        raised and audited: the pure function reports what it rejected and the engine call
        site emits the ``result_rejected`` event (the CLAUDE.md "pure fold returns, only
        the engine caller emits" convention). ``reason_code`` is the stable machine-readable
        field on that event; the message is the human/CLI text of the ``ContractError``.

        Called TWICE per ``record`` (#277): once lock-free as the cheap early reject
        (BEFORE the first side effect, the ledger row), and again on the freshly-loaded
        doc inside the locked commit — the authoritative check that makes two concurrent
        duplicate records mutually exclusive (the loser sees the cleared lease under the
        lock and raises). Both call sites emit ``result_rejected``, so a refused result is
        loud in the run's durable log and not just an exception on the caller's stderr.

        The schema_version check comes FIRST (#275): if the runner and the engine do not
        agree on what a StageResult *is*, every field-level comparison below is reading a
        contract it cannot trust, and the actionable error is the version mismatch rather
        than whichever field happened to move. There is no migration ladder for the work
        plane — a WorkItem/StageResult is in-flight wire traffic, not an archive, so exactly
        one version is supported and an off-version result is refused rather than guessed
        at. Refusing here (not in a Pydantic validator) is deliberate: this is the boundary
        that already emits ``result_rejected``, so an off-version runner shows up in the
        run's durable log with a reason code instead of as a parse error on someone's
        stderr, and the raw result JSON still deserializes for a human to inspect.

        A result is only valid against the WorkItem currently outstanding for this
        task. No outstanding dispatch (pending is None) => a replay/duplicate; reject
        so a stale result can never be re-folded into an already-advanced stage.
        content_hash is an echoed string — on the interactive lane the supervisor
        hand-assembles the WorkItem JSON, so a truncated/wrong-item paste is entirely
        possible (#311, live in ``batch-next5b``) and must never be folded silently. The
        result also carries its OWN stage/model/attempt/run_id, and those drive pricing
        (result.model) and which stage record we fold into (result.stage). Bind them all to
        what was actually dispatched so a buggy runner or hand-edited result can't complete
        the wrong stage or price the wrong model. task.current_stage is the dispatched stage
        (begin_stage set it; a dispatch is outstanding).
        """
        if result.schema_version not in SUPPORTED_WORK_VERSIONS:
            return ("schema_version_unsupported", (
                f"result schema_version {result.schema_version!r} is not one this engine "
                f"speaks ({', '.join(sorted(SUPPORTED_WORK_VERSIONS))}) — the runner that "
                "produced it is built against a different StageResult contract. Update the "
                "runner (or the engine) so both sides agree; the result was NOT recorded."
            ))
        if task.pending_work_item_id is None:
            return ("no_dispatch_outstanding", (
                f"no dispatch outstanding for task {result.task_id} — refusing replayed result "
                f"{result.work_item_id}"
            ))
        if result.work_item_id != task.pending_work_item_id:
            return ("work_item_id_mismatch", (
                f"result work_item_id {result.work_item_id} != pending {task.pending_work_item_id}"
            ))
        if result.content_hash != task.pending_content_hash:
            return ("content_hash_mismatch", (
                "result content_hash does not match the dispatched WorkItem "
                f"(dispatched {_hash_preview(task.pending_content_hash)}, "
                f"received {_hash_preview(result.content_hash)}) — echo the WorkItem's "
                "content_hash verbatim; never retype or abbreviate it"
            ))
        if result.run_id != run_id:
            return ("run_id_mismatch", f"result run_id {result.run_id} != dispatched {run_id}")
        dispatched_stage = task.current_stage
        if result.stage is not dispatched_stage:
            return ("stage_mismatch", f"result stage {result.stage} != dispatched "
                                      f"{dispatched_stage}")
        dispatched = task.stages[dispatched_stage]
        if result.model != dispatched.model:
            return ("model_mismatch",
                    f"result model {result.model!r} != dispatched {dispatched.model!r}")
        if result.attempt != dispatched.attempt:
            return ("attempt_mismatch",
                    f"result attempt {result.attempt} != dispatched {dispatched.attempt}")
        return None

    def _emit_result_rejected(
        self, run_id: str, task: Task, result: StageResult, mismatch: tuple[str, str]
    ) -> None:
        """Audit a refused StageResult (#311) — warning-grade, engine-emitted.

        The refusal itself is a ``ContractError`` the caller sees; this makes it survive in
        ``events.jsonl`` too, so a supervisor that garbles a hash (or pastes one from
        another in-flight WorkItem) leaves a trace in the run's durable log instead of a
        run that "looks clean". Hashes are previewed, not dumped, so the truncation that
        motivated this is visible without two 64-char digests per line."""
        reason, message = mismatch
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "result_rejected", "level": "warning", "run_id": run_id,
             "task_id": result.task_id, "stage": result.stage.value,
             "attempt": result.attempt, "reason": reason,
             "work_item_id": result.work_item_id,
             "dispatched_work_item_id": task.pending_work_item_id,
             "content_hash": _hash_preview(result.content_hash),
             "dispatched_content_hash": _hash_preview(task.pending_content_hash),
             "detail": message},
        )

    def _audit_commit_attribution(
        self, run_id: str, task: Task, result: StageResult, *, since_sha: str | None
    ) -> None:
        """Post-hoc check that the commits a checkpoint stage produced carry NO model/agent
        attribution trailer (#322) — the verification half of #317's prompt directive.

        WHY it exists: the directive is the only thing standing between a harness's standing
        "sign your commits" instruction and a trailer in permanent history, and a directive
        is not a guarantee. It has already failed twice here — ``batch-headless-1`` and
        ``batch-headless-2`` each merged a commit signed ``Claude Opus 4.5``, a model NEITHER
        run dispatched — and both times the only detector was a human reading ``git log``.

        WHICH commits: ``<previous checkpoint or base_sha>..<this stage's checkpoint sha>``
        in the task's own worktree — exactly the commits this stage added, so each new commit
        is scanned once rather than the whole branch being re-scanned every stage.

        REPORT-ONLY, never amend — that is how the "rewrite already-pushed history" hazard is
        avoided rather than merely survived. DELIVER pushes its branch BEFORE its checkpoint
        lands, so by the time any engine-side check can see the commit it is already remote;
        amending would fork the PR's history under the reviewer. The honest scope is a
        warning-grade ``commit_attribution_trailer_found`` event (one per offending commit,
        with the offending line as evidence) that a human or CI can act on.

        Never-silent about ITSELF: every call emits one ``commit_attribution_scanned``
        carrying the range, how many commits it read, how many it flagged, whether the cap
        truncated it, and — when it could not run at all — a ``skipped`` reason. "Clean" and
        "never looked" must not read alike in the log, which is the same defect class the
        check exists to close.

        Best-effort (ENGINE lane, no model): any git or filesystem failure degrades to a
        recorded skip. This is an audit; it must never fail a stage that otherwise succeeded.
        """
        stage_ctx = {
            "run_id": run_id, "task_id": result.task_id, "stage": result.stage.value,
            "attempt": result.attempt,
        }

        def _scanned(**fields: object) -> None:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "commit_attribution_scanned", **stage_ctx,
                 "commits": 0, "flagged": 0, "capped": False, "range": None,
                 "skipped": None, **fields},
            )

        worktree = task.context.get("worktree")
        if not worktree or not Path(str(worktree)).is_dir():
            _scanned(skipped="no_worktree")
            return
        head = (result.checkpoint or {}).get("sha") or "HEAD"
        # The lower bound: this stage's own starting point. base_sha is the fallback for the
        # FIRST checkpoint stage, which has no previous checkpoint to diff against.
        since = since_sha or task.context.get("base_sha")
        if not since:
            # No anchor means no bounded range, and an unbounded ``git log <head>`` would
            # scan the project's entire history and flag every pre-existing human commit.
            # Refusing is the correct answer; recording the refusal is what keeps it honest.
            _scanned(skipped="no_base")
            return
        rev = f"{since}..{head}"
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", "log", f"-n{_ATTRIBUTION_SCAN_COMMIT_CAP + 1}",  # noqa: S607
                 "--format=%H%x1f%B%x1e", rev],
                cwd=str(worktree), capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _scanned(range=rev, skipped=f"git_error: {type(exc).__name__}")
            return
        if proc.returncode != 0:
            # An unresolvable anchor (a checkpoint tag pruned, a rebased branch) is the
            # common cause: report it, don't guess a wider range.
            _scanned(range=rev, skipped="git_error: rev_unreadable")
            return

        commits: list[tuple[str, str]] = []
        for record_text in proc.stdout.split("\x1e"):
            entry = record_text.strip("\n")
            if not entry.strip():
                continue
            sha, _, message = entry.partition("\x1f")
            commits.append((sha.strip(), message))
        capped = len(commits) > _ATTRIBUTION_SCAN_COMMIT_CAP
        commits = commits[:_ATTRIBUTION_SCAN_COMMIT_CAP]

        flagged = scan_commits(commits)
        for offender in flagged:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "commit_attribution_trailer_found", "level": "warning",
                 **stage_ctx, **offender},
            )
        _scanned(range=rev, commits=len(commits), flagged=len(flagged), capped=capped)

    def record(self, run_id: str, result: StageResult) -> dict:
        """Fold a runner's ``StageResult`` into the task and charge the cost ledger.

        Crash-idempotent (#277) — replaying the same result after a crash at any
        point converges instead of double-counting. Persistence ORDER: (1) lock-free
        lease pre-validation (cheap replay reject before any side effect); (2) the
        idempotent ledger charge (keyed on ``(work_item_id, phase)`` — a replay
        answers from the on-disk rows, and the append boundary self-heals a torn
        trailing line from its own interrupted write, see ``CostLedger.record_rows``);
        (3) ONE locked transaction via
        ``commit_task_events``: authoritative lease re-validation on the fresh doc,
        the task transition, per-stage log/markdown (atomic overwrites on the
        just-claimed stage counter), audit events appended events-first (the
        ``stage_recorded`` batch is deduped on replay), and the task doc written
        LAST — the single durable commit point, because clearing the dispatch lease
        is what makes a replay rejectable, so everything observable must already be
        on disk when it clears. Everything after (index/ref-state/progress/alerts)
        is re-derivable best-effort.

        Returns a summary dict (outcome, task_state, stage, cost_usd,
        lane_attributed, next_stage). Raises ``ContractError`` when ``result`` does
        not match the outstanding dispatch lease (stale/duplicate/wrong content-hash-
        stage-model-attempt-run) — of two concurrent duplicate records exactly one
        commits. Every such refusal also emits a warning-grade ``result_rejected``
        event (#311) so it is never silent in the run's durable log.
        """
        task = self.store.load_task(run_id, result.task_id)
        # Optimistic pre-validation OUTSIDE the lock (#277): reject an ordinary stale/
        # replayed result cheaply BEFORE the first side effect (the ledger row). The
        # authoritative re-validation runs on the freshly-loaded doc inside the locked
        # commit below. #311: the pure check RETURNS the mismatch so the engine can audit
        # it here before raising — the refusal has to be visible to a human reading the
        # run, not only to whoever caught the exception.
        if (mismatch := self._lease_mismatch(task, run_id, result)) is not None:
            self._emit_result_rejected(run_id, task, result, mismatch)
            raise ContractError(mismatch[1])
        # No mismatch => result.stage IS task.current_stage (the check above binds them),
        # so this is the dispatched stage record.
        dispatched = task.stages[result.stage]
        # #288: read the dispatched-plan marker BEFORE the locked commit clears it with the
        # lease. Safe off the pre-lock doc precisely because the lease matched: the fresh-doc
        # re-validation under the lock rejects anything that is not this same dispatch.
        plan_dispatched = task.pending_plan
        # #322: likewise read the PREVIOUS checkpoint before the commit absorbs this stage's
        # own — it is the lower bound of "the commits THIS stage produced", so the
        # attribution scan below flags each new commit once instead of re-flagging the whole
        # branch at every later stage. None on the first checkpoint (falls back to base_sha).
        prior_checkpoint_sha = (task.last_checkpoint or {}).get("sha")

        # Cost ledger: EVERY call recorded, single authoritative pricing (the ledger
        # and the engine share one model table; compute once, in the ledger). Wall time
        # is engine-measured (dispatch begin_stage -> now) so reports can show duration.
        duration_s: float | None = None
        if dispatched.started_at:
            try:
                duration_s = max(
                    0.0,
                    (datetime.now(UTC) - datetime.fromisoformat(dispatched.started_at))
                    .total_seconds(),
                )
            except ValueError:
                duration_s = None
        # #277 crash-idempotency: a non-empty ``existing_rows_for`` answer means a PRIOR
        # record attempt already charged this dispatch and then crashed before the
        # task-doc commit (the lease is still held, so pre-validation passed) — this
        # call is a replay that must CONVERGE, not duplicate. The ledger itself is
        # idempotent on (work_item_id, phase), so ``record`` answers from the on-disk
        # rows without appending; the flag additionally gates the stage_recorded
        # event dedupe at the commit below.
        replayed_after_charge = bool(self.ledger.existing_rows_for(result.work_item_id))
        # #319: keep the row's ``metered`` flag with its ``cost_usd`` — a 0.0 from an
        # unrecoverable usage report is an UNKNOWN, not a measured zero, and dropping the
        # flag here is exactly what let a $4.25 failed attempt render as a confident $0.0000
        # on the task doc, the progress comment and the completion note.
        cost_row = self.ledger.record(result, duration_s=duration_s)
        cost = cost_row["cost_usd"]
        cost_metered = bool(cost_row.get("metered", True))

        # Attributed/clean iff the lane actually used is a sanctioned (registered) cell.
        lane_clean = (
            result.lane_used.execution_mode,
            result.lane_used.provider,
        ) in self.registry.sanctioned()

        # Deterministic stage gate (the "verify" half of the collapsed test stage,
        # target.md §6.1): a runner-reported SUCCESS can still be vetoed by the engine.
        # Today: the test stage must affirm its tests are meaningful — a green run that
        # doesn't is downgraded to a failure so it retries-with-learnings instead of
        # shipping vacuous tests. Cost is still recorded above (the model did run).
        gate_error = self._stage_gate(result) if result.status is ResultStatus.SUCCESS else None
        effective = (
            result if gate_error is None
            else result.model_copy(update={"status": ResultStatus.FAILURE, "error": gate_error})
        )
        # #277: every audit event this record produces is COLLECTED here and appended
        # events-first inside the single locked commit below (``commit_task_events``),
        # with the task doc written last as the one durable commit point — so a crash
        # can never persist a task transition whose events are not already on disk.
        events: list[dict] = []
        # Multi-agent REVIEW synthesis (#73 design §3): a plan-bearing dispatch returns the
        # raw panel output in ``sub_results`` and leaves ``structured_output`` to the engine.
        # Fold it into canonical review.json HERE — deterministically, with no model call —
        # so everything downstream (policy merge, verdict, convergence, evidence-out) reads
        # the same shape it always has and needs no knowledge of the workflow.
        synthesized: dict | None = None
        # #285: the fold's per-dispatch panel telemetry, persisted beside the raw
        # sub_results below. None (and therefore ABSENT from the payload) whenever the fold
        # did not run — a single-reviewer or failed-panel review has no panel to summarize,
        # and the key's presence is exactly that honest marker.
        panel_summary: dict | None = None
        if (
            effective.stage is Stage.REVIEW
            and effective.status is ResultStatus.SUCCESS
            and effective.sub_results is not None
        ):
            folded = synthesize(effective.sub_results)
            synthesized, notices, panel_summary = folded
            if effective.structured_output is not None:
                # The fold owns ``output`` for a plan-bearing review (design §2). A runner
                # that ALSO self-reported a verdict does not get to keep it — that is the
                # synthesizer-model hole the fold exists to close — but the override is
                # never silent.
                notices = (*notices, {
                    "notice": "runner_output_superseded",
                    "detail": "the runner returned both sub_results and a structured_output; "
                              "the deterministic fold owns review.json",
                })
            effective = effective.model_copy(update={"structured_output": synthesized})
            # Pure fold, engine-owned I/O (#235/#201): the fold returns what it dropped or
            # coerced; the call site is what emits (into the atomic commit batch, #277).
            events.extend(
                {"ts": _now(), "type": "review_synthesis_notice", "run_id": run_id,
                 "task_id": effective.task_id, "stage": effective.stage.value, **notice}
                for notice in notices
            )
            # #268: the RUNNER's own notices (verifier cap hit, an inconclusive verifier, an
            # unknown dedupe rule) were persisted in the stage log and nowhere else, so
            # "this review only verified 8 of 12 blocking findings" was invisible to status,
            # the dashboard and alerting. They ride the SAME events list — hence the same
            # #277 commit boundary and the same stage_recorded replay dedupe; no second
            # dedupe, no write outside the transaction. An unknown notice kind still emits.
            events.extend(
                {"ts": _now(), "type": "review_panel_notice", "level": "warning",
                 "run_id": run_id, "task_id": effective.task_id,
                 "stage": effective.stage.value, **notice}
                for notice in panel_summary["notices"]
            )
        # #288: the fold gate above is silent about its OWN complement. A lane whose
        # descriptor claims ``supports_plan`` but whose runner ignores ``work.plan`` returns
        # no ``sub_results``, so the fold is skipped, the runner's self-reported review is
        # accepted verbatim, and nothing anywhere records that a panel was requested and not
        # delivered — a run configured ``review_workflow: true`` produced reviews
        # byte-indistinguishable from single-reviewer ones (the #261 defect class: a skipped
        # check that leaves no trace is a false record). ``task.pending_plan`` is the durable
        # half of the dispatch that answers it; the lease pre-validation above already proved
        # this result belongs to that dispatch. Warning-grade and status-neutral: a degraded
        # review is still a review, so we fail toward honesty, not toward failing the stage.
        #
        # RATE_LIMITED / PROVIDER_UNAVAILABLE are excluded because the provider never ran
        # this WorkItem at all (the stage goes back on the wire, attempt intact) — the plan's
        # non-execution there is not a fact about the runner, and flagging it would make the
        # audit cry wolf on a run whose retry does execute the panel.
        review_plan_not_executed = (
            plan_dispatched
            and effective.stage is Stage.REVIEW
            and effective.sub_results is None
            and effective.status not in _PLAN_UNRUN_STATUSES
        )
        if review_plan_not_executed:
            events.append(
                {"ts": _now(), "type": "review_plan_not_executed", "level": "warning",
                 "run_id": run_id, "task_id": result.task_id, "stage": result.stage.value,
                 "attempt": result.attempt, "work_item_id": result.work_item_id,
                 "lane": f"{result.lane_used.execution_mode.value}:"
                         f"{result.lane_used.provider.value}",
                 "status": effective.status.value}
            )
        # Deterministic project policy gates (#65): merge the adapter's review_findings
        # into a completed REVIEW result BEFORE the verdict is read — the old
        # merge_e2e_policy_review_finding semantics (a blocking deterministic finding
        # overrides the model's approval; the model can't skip a policy gate).
        if effective.stage is Stage.REVIEW and effective.status is ResultStatus.SUCCESS:
            effective = self._merge_policy_findings(run_id, task, effective)
        # #227: capture the fixup request from the canonical review output (including a
        # synthesized panel output) before entering the locked transition.  The helper is
        # pure; whether this is a fresh request, a repeated failed fixup, or a request that
        # can piggyback on a blocking-review cycle depends on the fresh task under the lock.
        review_fixup = self._review_fixup(effective)
        # A rate-limit with a cheaper model still available is transient — re-queue the
        # SAME stage+attempt on that model rather than burning a retry or tripping the
        # breaker. Gated on the lane's allow_fallback (so the flag is honored, not dead).
        # At the floor / with fallback disabled, it degrades to a normal failure (bounded:
        # the worst case walks the chain once then fails out via the attempt counter).
        lane_allows_fallback = self.router.lane_for(result.stage, task).allow_fallback
        fallback_model = (
            self.models.fallback_after(result.model)
            if effective.status is ResultStatus.RATE_LIMITED and lane_allows_fallback else None
        )
        # At the floor (or with fallback disabled) the rate limit can't be dodged with a
        # cheaper model — wait it out (the old handle_rate_limit's wait-until-reset) and
        # retry the ORIGINAL model, bounded by max_rate_limit_waits; only past that budget is
        # the provider's same-provider budget exhausted. ``provider_out_reason`` captures the
        # two "the (codex) provider is out" signals #7 falls through on: a runner-reported
        # PROVIDER_UNAVAILABLE (CLI missing / auth), OR a floor rate-limit whose wait budget is
        # spent. It stays None while a cooldown wait remains (that is not exhaustion yet).
        # Decided inside the locked commit below, where the wait budget reads the FRESH doc.
        # The run doc is loaded lock-free HERE, before the task lock — the run LOCK is
        # never taken inside the task lock (established ordering).
        run = self.store.load_run(run_id)

        # Decision/mutation state the locked commit captures (nonlocal) for the
        # post-commit effects and the return value (#277).
        outcome = ""
        scope_blocked_reason: str | None = None
        review_verdict: dict | None = None
        test_validation: dict[str, object] | None = None
        review_fixup_action: dict[str, object] | None = None
        fixups_applied: list[ReviewFixup] = []
        cooldown_until: str | None = None
        provider_out_reason: str | None = None
        # #311: what the under-lock re-validation rejected (fresh doc + mismatch), captured
        # for the caller to audit AFTER the transaction aborts — the event is emitted
        # outside the task lock, keeping "only the engine caller emits" true of the locked
        # mutator too. Nothing is mutated before the check, so the doc is pristine.
        lease_rejection: tuple[Task, tuple[str, str]] | None = None

        def _commit(t: Task) -> None:
            nonlocal effective, outcome, scope_blocked_reason, review_verdict
            nonlocal test_validation, review_fixup_action, fixups_applied
            nonlocal cooldown_until, provider_out_reason, lease_rejection
            # Authoritative lease validation under the task lock (#277): the whole
            # validate→transition sequence is one read-modify-write on the fresh doc,
            # so of two concurrent duplicate records exactly one clears the lease —
            # the loser sees it already cleared here and gets the ContractError replay
            # rejection (after the idempotent ledger call, before any task mutation).
            if (m := self._lease_mismatch(t, run_id, result)) is not None:
                lease_rejection = (t, m)
                raise ContractError(m[1])
            if effective.status is ResultStatus.PROVIDER_UNAVAILABLE:
                provider_out_reason = effective.error or "provider reported unavailable"
            elif effective.status is ResultStatus.RATE_LIMITED and fallback_model is None:
                if t.rate_limit_waits < self.max_rate_limit_waits:
                    cooldown_until = (
                        datetime.now(UTC) + timedelta(seconds=self.rate_limit_cooldown_s)
                    ).isoformat()
                else:
                    provider_out_reason = (
                        "rate-limited with no cheaper fallback available and the "
                        f"cooldown budget exhausted ({t.rate_limit_waits} waits)"
                    )

            # Cross-provider fallthrough (#7): opt-in, one-way (codex→claude), once per
            # stage. When a CODEX dispatch's same-provider options are exhausted
            # (provider_out_reason set) and the run consented (cross_provider_fallback),
            # re-route this stage's NEXT dispatch to the equivalent claude lane instead
            # of failing/parking. Keyed off the lane ACTUALLY used (ground truth), so a
            # claude result never falls through — no ping-pong.
            do_fallthrough = (
                provider_out_reason is not None
                and run.cross_provider_fallback
                and result.lane_used.provider is Provider.CODEX
                and result.stage not in t.fallthrough_stages
            )
            if provider_out_reason is not None and not do_fallthrough:
                # No fallthrough (flag off / not codex / already fell through once): a
                # provider-out signal degrades to a normal FAILURE — retry within the
                # provider, then fail out, exactly as before #7 existed. Idempotent for a
                # rate-limit already FAILURE-shaped; the meaningful conversion is
                # PROVIDER_UNAVAILABLE → FAILURE.
                effective = effective.model_copy(update={
                    "status": ResultStatus.FAILURE, "error": provider_out_reason,
                })

            t.pending_work_item_id = None
            t.pending_content_hash = None
            t.pending_plan = False  # #288: the plan marker never outlives its lease

            if do_fallthrough:
                # do_fallthrough implies provider_out_reason is not None (see above);
                # assert it so the reason: str parameter type-checks without laundering
                # the None through.
                assert provider_out_reason is not None
                outcome = self._apply_fallthrough(t, result, provider_out_reason)
            elif effective.status is ResultStatus.RATE_LIMITED:
                # Transient: re-queue the stage (RUNNING marker keeps the attempt) — either
                # immediately on a cheaper model, or after a cooldown on the original one.
                # No apply_result/learnings/breaker, but cost is recorded.
                rec = t.stages[result.stage]
                rec.status = StageStatus.RUNNING
                rec.completed_at = None
                rec.error = None
                t.state = TaskState.RETRYING
                t.updated_at = _now()
                # Rate-limit is a salvageable KIND (SALVAGEABLE_FAILURE_STATUSES): if the
                # attempt committed real work before the 429, keep it in place so the seamless
                # re-queue (cheaper model / post-cooldown) builds on it instead of resetting to
                # the checkpoint. Same cap as the failure path (#59).
                self._apply_salvage(t, effective)
                if cooldown_until is None:
                    t.pending_fallback_model = fallback_model
                    outcome = "stage_rate_limited_fallback"
                else:
                    t.rate_limit_waits += 1
                    t.not_before = cooldown_until
                    outcome = "stage_rate_limited_cooldown"
            else:
                fold_notices = apply_result(
                    t, effective, now=_now(), cost_usd=cost, metered=cost_metered
                )
                # #201: a malformed model pr_number/pr_url that validate_assignment dropped at
                # the fold is no longer invisible — surface each drop as a warning-grade audit
                # event, mirroring effort_downgraded/model_downgraded ('never silent').
                events.extend(
                    {"ts": _now(), "type": "pr_field_dropped", "run_id": run_id,
                     "task_id": effective.task_id, "stage": effective.stage.value, **notice}
                    for notice in fold_notices.pr_fields
                )
                # #289: the context plane's own bounding is no longer silent either. A value
                # the per-field cap truncated, or a key the whole-context ceiling evicted,
                # degrades every LATER stage's prompt (the SCOPE plan a truncated fold handed
                # the implementer read as implementer error, since nothing in events.jsonl
                # said the plan had been cut). Each notice names the field and how much went.
                events.extend(
                    {"ts": _now(), "type": "context_value_truncated", "run_id": run_id,
                     "task_id": effective.task_id, "stage": effective.stage.value, **notice}
                    for notice in fold_notices.truncations
                )
                events.extend(
                    {"ts": _now(), "type": "context_key_evicted", "run_id": run_id,
                     "task_id": effective.task_id, "stage": effective.stage.value, **notice}
                    for notice in fold_notices.evictions
                )
                if effective.status is ResultStatus.SUCCESS:
                    t.error_signatures = []  # streak resets on a clean stage
                    t.rate_limit_waits = 0  # a clean stage refreshes the cooldown budget
                    t.infra_resets = 0  # ... and the infra-reset budget (#14)
                    t.salvage_count = 0  # ... and the salvage-keep budget (#59)
                    t.salvage_in_place = False  # a clean stage leaves nothing to keep
                    # Session chaining (design pass §2): reuse across SUCCESSFUL stage
                    # transitions only. A runner that reports no ref leaves the prior one
                    # in place (resuming a slightly-stale session is safe: prompts are
                    # self-contained and a dead session cold-starts in the transport).
                    if effective.session_ref:
                        t.session_ref = effective.session_ref
                        # Tag the ref with the provider that produced it (#9) so a later stage
                        # on the other provider won't try to resume a foreign session.
                        t.session_provider = effective.lane_used.provider
                    # Checkpoint anchor (design pass §3): SUCCESS only — a failed or
                    # gate-vetoed attempt's commits must never become a reset target.
                    if effective.checkpoint:
                        t.last_checkpoint = effective.checkpoint
                    # Feasibility gate (issue #45): a completed SCOPE stage that explicitly
                    # reports feasible=false parks the task at the human approval gate rather
                    # than advancing to implement a no-op. apply_result already folded
                    # blocked_reason into task.context (CONTEXT_KEYS[SCOPE]); we reuse the
                    # non-terminal BLOCKED_ON_HUMAN state (park-for-human) — an autonomous
                    # hard-close is deferred to its own issue. Fail-open (see helper).
                    scope_blocked_reason = self._scope_not_feasible(effective)
                    # Review gate: a completed REVIEW that explicitly reports approved=false
                    # must never fall through to task_completed (the old system's strongest
                    # quality loop — restored as a bounded fix cycle; issue #15 keeps the
                    # convergence-auto-approval refinement).
                    review_verdict = self._review_verdict(effective, t)
                    # #261: did ANYTHING judge test-meaningfulness? The pure helper decides
                    # (nothing judged it, or an explicit verdict the #41/#168 exemption
                    # discarded); the event is emitted at the call site in _stage_events,
                    # per the "pure fold returns what it dropped" convention. Observability
                    # only — it never gates completion (fail-OPEN stays fail-OPEN).
                    test_validation = unjudged_tests_notice(t, effective)
                    if scope_blocked_reason is not None:
                        t.state = TaskState.BLOCKED_ON_HUMAN
                        outcome = "scope_not_feasible_held"
                    elif review_verdict is not None and review_verdict["kind"] == "rejected":
                        outcome = self._apply_review_rejection(t, review_verdict)
                        if review_fixup is not None:
                            if outcome == "review_rejected_fix_cycle":
                                reason = self._review_fixup_tail_ineligibility(t)
                                if reason is not None:
                                    review_fixup_action = {
                                        "disposition": "held",
                                        "reason": reason,
                                    }
                                else:
                                    fresh = self._remember_review_fixup(t, review_fixup)
                                    review_fixup_action = {
                                        "disposition": "scheduled",
                                        "reason": "combined with blocking-review fix cycle",
                                        "already_scheduled": not fresh,
                                    }
                            else:
                                review_fixup_action = {
                                    "disposition": "held",
                                    "reason": "blocking review exhausted the rework budget",
                                }
                    elif review_fixup is not None:
                        outcome, reason = self._apply_review_fixup(t, review_fixup)
                        review_fixup_action = {
                            "disposition": (
                                "scheduled" if outcome == "review_fixup_cycle" else "held"
                            ),
                            "reason": reason,
                        }
                    else:
                        # A later REVIEW that reaches completion without asking for the
                        # fixup again is the engine-observable proof that the re-implement,
                        # re-test and re-deliver pass satisfied it.  Mark the durable records
                        # in the approving review's transaction (even when a bespoke
                        # pipeline has later stages) so disposition text can never get ahead
                        # of evidence.
                        if effective.stage is Stage.REVIEW:
                            fixups_applied = [f for f in t.review_fixups if not f.applied]
                            for fixup in fixups_applied:
                                fixup.applied = True
                        if is_done(t):
                            t.state = TaskState.COMPLETED
                            outcome = "task_completed"
                        else:
                            t.state = TaskState.RUNNING
                            outcome = "stage_completed"
                else:
                    # Session fate on a failure is decided inside _handle_failure (it has the
                    # infra classification + the salvage decision the warm-retry policy needs).
                    # Default: clear (design pass §2 fresh-after-failure). Warm retry (#8) keeps
                    # it only when the run opted in AND the failure was mechanical AND the
                    # worktree still matches the session — see _settle_failed_session.
                    outcome = self._handle_failure(t, effective, run=run)
            # The per-stage log sequence is claimed under the same lock as the mutation,
            # so a replay recomputes the SAME seq (the counter only persists with the doc).
            t.stage_counter += 1

        def _stage_events(t: Task) -> list[dict]:
            # Runs under the SAME task lock, after the mutation and before the task-doc
            # write. The durable per-stage log (JSON contract) + human-readable Markdown
            # are (re)written first — atomic overwrites keyed on the just-claimed stage
            # counter, so a crash-replay converges on the same paths — then the audit
            # batch is returned for commit_task_events' events-first append (task doc
            # LAST, the single durable commit point).
            payload = {
                "work_item_id": result.work_item_id,
                "stage": result.stage.value,
                "task_id": result.task_id,
                "attempt": result.attempt,
                "status": effective.status.value,
                "outcome": outcome,
                "model": result.model,
                "effort": result.effort,  # #96: the reasoning effort the dispatch ran at
                "lane_used": result.lane_used.model_dump(),
                "cost_usd": cost,
                "session_ref": result.session_ref,
                "checkpoint": result.checkpoint,
                "salvage": result.salvage,
                "salvage_kept": t.salvage_in_place,
                "stream_files": result.stream_files,  # #56: raw provider stdout/stderr on disk
                "persona_injected": result.persona_injected,  # #74: codex worktree AGENTS.md persona
                "structured_output": result.structured_output,
                "raw_output": result.raw_output,
                "error": effective.error,
                "completed_at": result.completed_at,
            }
            # #73: the panel's RAW, unfolded output is the evidence (which lens found what,
            # what each verifier argued); the folded review is the verdict everything else
            # consumed, so the durable record carries both. Both writes are conditional — a
            # plan-less review's payload stays byte-identical, and a plan-bearing result the
            # fold did NOT run on (a FAILED review) keeps its own structured_output.
            if result.sub_results is not None:
                payload["sub_results"] = result.sub_results
            if synthesized is not None:
                payload["structured_output"] = synthesized
            # #285: the panel's deterministic telemetry (per-lens totals, cross-lens
            # agreement, verdict tallies, runner notices) — computed by the fold, persisted
            # here, rendered by render_stage. Conditional for the same reason as the two
            # above: a plan-less review's payload stays byte-identical.
            if panel_summary is not None:
                payload["panel_summary"] = panel_summary
            # #288: pair ``panel_summary``'s ABSENCE with a positive marker. Absence alone is
            # ambiguous (no panel asked for vs. asked for and not delivered), which is exactly
            # how a plan-ignoring lane produced a review indistinguishable from a
            # single-reviewer one. Conditional like the three above: a review nobody planned a
            # panel for keeps a byte-identical payload.
            if review_plan_not_executed:
                payload["review_plan_not_executed"] = True
            seq = t.stage_counter
            self.store.write_stage_log(result.task_id, seq, result.stage.value, payload)
            self.store.write_stage_markdown(
                result.task_id, seq, result.stage.value, render_stage(payload)
            )
            # Audit the cross-provider fallthrough (#7): the run consented and codex was
            # out, so the stage's NEXT dispatch is re-routed to claude — from→to + why.
            if outcome == "provider_fallthrough":
                events.append(
                    {"ts": _now(), "type": "provider_fallthrough", "run_id": run_id,
                     "task_id": result.task_id, "stage": result.stage.value,
                     "from": Provider.CODEX.value, "to": Provider.CLAUDE.value,
                     "reason": provider_out_reason,
                     "attempt": result.attempt}
                )
            # Audit a cooldown park: when the task may dispatch again, and how much of the
            # wait budget is spent — so a stalled-looking run explains itself in the events.
            if outcome == "stage_rate_limited_cooldown":
                events.append(
                    {"ts": _now(), "type": "rate_limit_cooldown", "run_id": run_id,
                     "task_id": result.task_id, "stage": result.stage.value,
                     "not_before": t.not_before,
                     "waits_used": t.rate_limit_waits,
                     "waits_budget": self.max_rate_limit_waits}
                )
            # Audit the review verdict alongside the generic stage record: what the reviewer
            # rejected (or auto-approved as suggestions-only) and how it was disposed of.
            if review_verdict is not None:
                events.append(
                    {"ts": _now(), "type": "review_verdict", "run_id": run_id,
                     "task_id": result.task_id, "kind": review_verdict["kind"],
                     "disposition": review_verdict.get("disposition"),
                     "issues": review_verdict["issues_text"],
                     "review_cycles": t.review_cycles}
                )
            # #227: the in-place improvement loop has its own evidence rather than
            # masquerading as a filed/not-filed decision.  These events share the review
            # result's atomic commit boundary, so a record replay cannot schedule or apply
            # the same fixup twice.
            if review_fixup is not None and review_fixup_action is not None:
                disposition = str(review_fixup_action["disposition"])
                events.append(
                    {"ts": _now(), "type": f"review_fixup_{disposition}",
                     "run_id": run_id, "task_id": result.task_id,
                     "stage": result.stage.value, "title": review_fixup.title,
                     "fingerprint": review_fixup.fingerprint,
                     "reason": review_fixup_action["reason"],
                     **(
                         {"already_scheduled": review_fixup_action["already_scheduled"]}
                         if "already_scheduled" in review_fixup_action else {}
                     )}
                )
            for fixup in fixups_applied:
                events.append(
                    {"ts": _now(), "type": "review_fixup_applied", "run_id": run_id,
                     "task_id": result.task_id, "stage": result.stage.value,
                     "title": fixup.title, "fingerprint": fixup.fingerprint}
                )
            # #261: nobody judged whether the tests are meaningful (or the reviewer's verdict
            # was suppressed by the no-model-test-surface exemption). Warning-grade, so a run
            # that skipped the #13 guarantee SAYS so instead of carrying a `tests_meaningful:
            # true` nobody earned. Never blocking — the gates stay fail-OPEN.
            if test_validation is not None:
                events.append(
                    {"ts": _now(), "type": "test_validation_skipped", "level": "warning",
                     "run_id": run_id, "task_id": result.task_id,
                     "stage": result.stage.value, **test_validation}
                )
            # Audit the WHY of a feasibility park alongside the generic stage record, so the
            # event stream shows the blocked_reason that routed the task to the human gate.
            if scope_blocked_reason is not None:
                events.append(
                    {"ts": _now(), "type": "scope_not_feasible", "run_id": run_id,
                     "task_id": result.task_id, "stage": result.stage.value,
                     "blocked_reason": scope_blocked_reason}
                )
            # The generic stage record goes LAST in the batch (#277): its presence in
            # events.jsonl therefore proves the whole batch landed, making it the replay
            # dedupe key below — a crash mid-batch re-appends (duplicating at worst an
            # auxiliary audit line) rather than ever losing an event.
            events.append(
                {
                    "ts": _now(),
                    "type": "stage_recorded",
                    "run_id": run_id,
                    "task_id": result.task_id,
                    "stage": result.stage.value,
                    "attempt": result.attempt,
                    # #175: stamp the closed lease so the events-balance audit can join a
                    # `stage_recorded` back to its opening `stage_dispatched` by work_item_id
                    # (lease_superseded/dispatch_abandoned already carry it) — the join key
                    # that turns the #142 hand-count into an automated orphan check.
                    "work_item_id": result.work_item_id,
                    "effort": result.effort,  # #96: audit alongside model/lane
                    "status": effective.status.value,
                    "outcome": outcome,
                    "lane": result.lane_used.execution_mode.value,
                    "provider": result.lane_used.provider.value,
                    "cost_usd": cost,
                    "task_state": t.state.value,
                }
            )
            # #277 replay convergence: when a PRIOR record attempt both charged the
            # ledger AND appended this dispatch's `stage_recorded` (it crashed at the
            # task-doc write, the only boundary left), the batch is already durable —
            # append nothing and let the doc commit converge the task state. The scan
            # runs only on the replay-candidate path, so the common case pays nothing.
            if replayed_after_charge and any(
                ev.get("type") == "stage_recorded"
                and ev.get("work_item_id") == result.work_item_id
                for ev in self.store.read_events(run_id)
            ):
                return []
            return events

        # #277: ONE transaction boundary — the lease re-check + task transition run under
        # the task lock, the audit events append FIRST (each atomic), and the task doc is
        # written LAST as the single durable commit point (the commit_task_events pattern,
        # #174/#199). The idempotent ledger row and the idempotent per-stage artifacts
        # land before that point; everything after it (index/ref-state/progress and the
        # run-level effects below) is re-derivable best-effort.
        try:
            task = self.store.commit_task_events(run_id, result.task_id, _commit, _stage_events)
        except ContractError:
            # #311: the loser of a concurrent-record race (or a mismatch that only the fresh
            # doc could see) is audited too — outside the lock, where events belong. Nothing
            # was mutated, so the raise below is the whole effect the caller sees.
            if lease_rejection is not None:
                self._emit_result_rejected(run_id, lease_rejection[0], result, lease_rejection[1])
            raise
        self.store.write_task_index(result.task_id, render_task_index(task))
        # cost-summary.md is written at run finalization and on status() — NOT on
        # every record (that re-read the whole ledger each time: O(N^2) on long runs).
        self._set_ref_state(run_id, result.task_id, task.state)
        # A valid child graph turns this task into a DAG umbrella after its SCOPE result is
        # durably recorded. Filing is deliberately outside the task-result transaction: it
        # is an external side effect, while the mapping persisted after each child makes a
        # retry/reconciliation resume from the last acknowledged ref.
        # A parent the transaction just parked at the human gate files NOTHING. A SCOPE
        # result carrying both feasible=false and subtasks otherwise reached this branch and
        # turned the feasibility hold into a decomposition umbrella — filing external issues
        # before the human ever saw the "not feasible" verdict they were being held for. The
        # hold wins; ``_resume_pending_decompositions`` picks the saga up from the persisted
        # SCOPE output once ``approve`` releases the task.
        if (
            result.stage is Stage.SCOPE
            and effective.status is ResultStatus.SUCCESS
            and (effective.structured_output or {}).get("subtasks") is not None
            and task.state is not TaskState.BLOCKED_ON_HUMAN
        ):
            task = self._apply_scope_decomposition(run_id, task, effective.structured_output)
            outcome = (
                "task_decomposed"
                if task.decomposition_children else "scope_decomposition_held"
            )
        # #322: verify, after the fact, that the commits this stage actually produced carry
        # no model/agent attribution trailer. #317's directive is an instruction, not a
        # guarantee; this is the deterministic check that says so in ``events.jsonl`` when it
        # was ignored. Runs outside the lock (it shells git) and best-effort — it never
        # changes the transition the transaction just committed.
        if effective.status is ResultStatus.SUCCESS and STAGE_SPECS[result.stage].checkpoint:
            self._audit_commit_attribution(
                run_id, task, result, since_sha=prior_checkpoint_sha
            )
        # Mid-run progress commentary (#64): upsert the living progress comment/PR-body
        # section on the driving issue/PR at this stage boundary (opt-in, throttled,
        # best-effort). A rate-limit re-queue and a cross-provider fallthrough are NOT
        # boundaries (the same stage/attempt goes back on the wire) — skip them so the
        # running/next picture doesn't flicker.
        if not outcome.startswith("stage_rate_limited") and outcome != "provider_fallthrough":
            self._maybe_publish_progress(run_id, task)
        # Alerting (#55) + post-transition run-level effects. A terminal FAILURE routes
        # through the SHARED ``_finalize_task_terminal`` helper (#133) — the same one the
        # operator finalize paths (``reject``/``abandon``) use — so ALL post-terminal
        # effects (operator-invoked AND model-result-driven) have a single source of truth
        # and cannot drift: the helper emits the ``task_failed`` alert (#55/#107),
        # cascade-blocks dependents, releases the port block (#5), harvests learnings (#72),
        # and finalizes the run. ``outcome.startswith("task_failed")`` ⟺ ``task.state is
        # FAILED`` (the only apply path that sets FAILED here returns a ``task_failed_*``
        # outcome), so this covers exactly the branch that fired those effects inline before.
        # The helper's failed-path notification reason is the ``reason`` passed here (#184:
        # it no longer prefers ``task.last_error``, which can carry a stale value from an
        # earlier review-rejection cycle — see _apply_review_rejection). Passing
        # ``effective.error or outcome`` reproduces the prior inline payload's reason (e.g.
        # the gate/runner error, else the outcome slug).
        if task.state is TaskState.FAILED:
            self._finalize_task_terminal(
                run_id, task, disposition="failed", reason=effective.error or outcome
            )
        else:
            # Non-failure transitions keep their own emissions + terminal handling. An
            # AUTONOMOUS park at the human gate (scope-infeasible / review-rejected-held —
            # NOT the human-initiated hold_for_approval, which the human already knows about)
            # is exactly the unattended-run event the old monitor alerted on.
            if task.state is TaskState.BLOCKED_ON_HUMAN:
                self.emit_notification(
                    run_id, "task_blocked",
                    {"run_id": run_id, "task_id": result.task_id, "kind": "task_blocked",
                     "summary": f"task {result.task_id} BLOCKED_ON_HUMAN at "
                                f"{result.stage.value} ({outcome}) — needs a human decision",
                     "stage": result.stage.value,
                     "reason": scope_blocked_reason or task.last_error or outcome},
                )
            # #5: audit the port block intake just allocated (it folded port_base into the
            # context plane via CONTEXT_KEYS[INTAKE]); the engine is the event authority, the
            # adapter does the allocation. Only on a fresh INTAKE success that carries a block
            # (never on the FAILED path above — this is guarded on a SUCCESS result).
            if (
                result.stage is Stage.INTAKE
                and effective.status is ResultStatus.SUCCESS
                and (pb := task.context.get("port_base")) is not None
            ):
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "ports_allocated", "run_id": run_id,
                     "task_id": result.task_id, "port_base": pb,
                     "port_count": task.context.get("port_count")},
                )
            # Post-transition run-level effects for the non-failure branch: mark_complete on
            # a COMPLETED task, then finalize the run when everything is terminal.
            if task.state is TaskState.COMPLETED:
                self._on_task_completed(run_id, task)
            # #5: a terminal task's worktree is done with its ports — release the block so a
            # later task can reuse it. Best-effort (wrapped in the helper); never breaks
            # finalize.
            if task.state in TERMINAL_TASK_STATES:
                self._release_ports(run_id, task)
                # #72: distil this finished task's learnings into the durable cross-run KB so
                # a later run doesn't re-pay to learn the same lesson. Skips a clean task
                # (empty learnings); best-effort — never breaks the terminal transition.
                self._harvest_task_learnings(run_id, task)
                self._harvest_process_retrospective(run_id, task)
            self._complete_ready_umbrellas(run_id)
            self._maybe_finalize_run(run_id)
        return {
            "recorded": True,
            "outcome": outcome,
            "task_state": task.state.value,
            "stage": result.stage.value,
            "cost_usd": cost,
            "lane_attributed": lane_clean,
            "next_stage": (
                None
                if task.decomposition_children
                else (s.value if (s := next_stage(task)) else None)
            ),
        }

    def _apply_scope_decomposition(
        self, run_id: str, parent: Task, output: dict | None
    ) -> Task:
        """File/register a validated child DAG and park ``parent`` as its umbrella.

        Validation runs before the lock, so a malformed child graph stays an ordinary
        retryable SCOPE failure rather than something a lock waiter can observe half-done.
        The saga itself — every external ``create_task`` and every durable write — runs
        under the parent's decomposition lock; see :meth:`_file_decomposition_children`.
        """
        tasks = parse_subtasks(output)
        if not tasks:
            return parent
        # Cheap pre-check on the caller's snapshot; the authoritative one is the in-lock
        # re-read, which is the only version that can see a concurrent racer's work.
        if parent.decomposition_children:
            return parent
        with self.store.with_decomposition_lock(run_id, parent.task_id):
            return self._file_decomposition_children(run_id, parent.task_id, tasks)

    def _file_decomposition_children(
        self, run_id: str, parent_id: str, tasks: list[ChildTaskPlan]
    ) -> Task:
        """Run the decomposition saga; the CALLER holds the parent's decomposition lock.

        External issue creation cannot share the status store's filesystem transaction.
        The durable local-id mapping is therefore advanced after every acknowledged
        ``create_task`` call; a resumed call skips mapped children and continues with the
        first unacknowledged one. Registration itself is already crash-idempotent.

        That resume is the whole reason the lock exists (#354). The lookup→create window is
        not atomic, so two reconcilers — ``record`` and any scheduler process calling
        ``dispatchable`` — could each read a mapping without local id X and each file their
        own external issue for it. Writing the mapping afterwards only records which one
        won; the loser's issue is real, assigned to nobody and referenced by nothing. Hence
        the FRESH re-read below: entering the saga is decided on the parent as it stands
        inside the lock, never on the snapshot the caller brought in, so a late entrant sees
        the winner's children and mapping and does nothing.
        """
        parent = self.store.load_task(run_id, parent_id)
        if parent.decomposition_children:
            return parent
        # Waiting on the lock can outlast the human gate closing — a losing racer's filing
        # failure parks the parent via ``_hold_decomposition``. Filing against a parent an
        # operator has just been asked to look at is the same mistake as filing for an
        # infeasible SCOPE: the hold wins, and ``approve`` resumes the saga.
        if parent.state is TaskState.BLOCKED_ON_HUMAN:
            return parent
        source = self.project.task_source
        create = getattr(source, "create_task", None)
        if not callable(create):
            return self._hold_decomposition(
                run_id, parent.task_id,
                "task source cannot create child tasks (missing create_task hook)",
            )

        by_id = {task.id: task for task in tasks}
        mapping = dict(parent.decomposition_mapping)
        try:
            for local_id in topological_order(tasks):
                child = by_id[local_id]
                dep_refs = [mapping[dep] for dep in child.depends_on]
                if local_id not in mapping:
                    marker = f"Decomposition-key: {parent.task_id}/{local_id}"
                    controls = (
                        f"Agent-role: {child.agent or 'default'}\n"
                        f"Quality-tier: {child.quality_tier.value}\n"
                        f"Implementation-budget: {child.implementation_budget.value}"
                    )
                    deps = f"\nDepends-on: {', '.join(dep_refs)}" if dep_refs else ""
                    body = (
                        f"Decomposed from {parent.task_id} ({local_id}).\n\n"
                        f"{marker}\n{controls}{deps}\n\n{child.description}"
                    )
                    ref = self._find_decomposition_child(source, marker)
                    if ref is None:
                        ref = create(
                            f"{parent.title or parent.task_id}: {local_id}", body, []
                        )
                    if not ref:
                        raise DecompositionError(
                            f"task source returned no ref for child {local_id!r}"
                        )
                    mapping[local_id] = str(ref)

                    # Snapshot per child: the mapping keeps growing, but what this
                    # acknowledgement persists is the refs confirmed so far.
                    saved = dict(mapping)

                    def _save_mapping(task: Task, saved: dict[str, str] = saved) -> None:
                        task.decomposition_mapping = saved

                    self.store.update_task(run_id, parent.task_id, _save_mapping)

            registered = self.registered_task_ids(run_id)
            for local_id in topological_order(tasks):
                child = by_id[local_id]
                ref = mapping[local_id]
                if ref in registered:
                    continue
                self.add_task(
                    run_id,
                    ref,
                    pipeline=_CHILD_PIPELINES[child.quality_tier],
                    depends_on=[mapping[dep] for dep in child.depends_on],
                    provider_tag=parent.provider_tag,
                    agent_role=child.agent,
                    quality_tier=child.quality_tier,
                    implementation_budget=child.implementation_budget,
                )
                registered.add(ref)

            leaves = [mapping[local_id] for local_id in leaf_ids(tasks)]
            # Graph first: once the parent is parked, its unmet leaf dependencies are the
            # durable reason it cannot be dispatched. All children are registered, so the
            # graph remains known-node and acyclic at every read after this write.
            self.store.update_run(
                run_id,
                lambda run: run.dependency_graph.__setitem__(parent.task_id, leaves),
            )

            def _park(task: Task) -> None:
                task.decomposition_mapping = dict(mapping)
                task.decomposition_children = [mapping[t.id] for t in tasks]
                task.state = TaskState.BLOCKED

            parent = self.store.update_task(run_id, parent.task_id, _park)
            self._set_ref_state(run_id, parent.task_id, parent.state)
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "task_decomposed", "run_id": run_id,
                 "task_id": parent.task_id, "children": parent.decomposition_children,
                 "leaf_children": leaves, "mapping": mapping},
            )
            return parent
        except Exception as exc:  # noqa: BLE001 - park partial external work for recovery
            return self._hold_decomposition(run_id, parent.task_id, str(exc))

    @staticmethod
    def _find_decomposition_child(source: object, marker: str) -> str | None:
        """Find a previously filed child by its deterministic body marker.

        Sources without a usable ``list_tasks`` hook return ``None``.  Lookup is
        deliberately best-effort because creation remains the source's responsibility; it
        covers only the CRASH window (the process died between an acknowledged create and
        the mapping write). Deduplication between LIVE reconcilers is the decomposition
        lock plus the in-lock mapping re-read, which hold with or without this hook.

        The marker must match a WHOLE body line. A substring test made local ids collide by
        prefix — searching ``…/a`` matched a child filed as ``…/ab``, so two valid subtasks
        collapsed onto one ref and the umbrella could finish having executed only one of
        them. ``ChildTaskPlan.id`` is also constrained to single-line, non-space characters,
        which keeps the marker itself one line and this comparison meaningful.
        """
        list_tasks = getattr(source, "list_tasks", None)
        if not callable(list_tasks):
            return None
        try:
            tasks = list_tasks(limit=100)
        except Exception:  # noqa: BLE001 - absence/failure falls back to create_task
            return None
        for task in tasks or []:
            if marker in str(getattr(task, "body", "")).splitlines():
                return str(task.task_id)
        return None

    def _resume_pending_decompositions(self, run_id: str) -> None:
        """Resume a scope-filing saga after a crash or an operator approval.

        A human-held failure remains quiescent. ``approve`` moves it to PENDING; the next
        scheduler eligibility pass then continues from its persisted mapping.

        The state read here is a pre-filter on a snapshot, not a guarantee: several
        schedulers may reach the same unfinished saga at once. Each per-parent saga is
        serialized and re-decided inside its own lock, so concurrent passes converge on one
        set of children rather than one set each.
        """
        run = self.store.load_run(run_id)
        for ref in list(run.task_refs):
            if ref.state in TERMINAL_TASK_STATES or ref.state is TaskState.BLOCKED_ON_HUMAN:
                continue
            task = self.store.load_task(run_id, ref.task_id)
            if task.decomposition_children:
                continue
            scope = task.stages.get(Stage.SCOPE)
            output = scope.output if scope and scope.status is StageStatus.COMPLETED else None
            if isinstance(output, dict) and output.get("subtasks") is not None:
                self._apply_scope_decomposition(run_id, task, output)

    def _hold_decomposition(self, run_id: str, task_id: str, reason: str) -> Task:
        """Park an incomplete decomposition for explicit operator recovery.

        Any already persisted local-id mapping is retained, the run-level task ref is
        synchronized, and a warning event records why automatic filing could not continue.
        """

        def _hold(task: Task) -> None:
            task.state = TaskState.BLOCKED_ON_HUMAN
            task.last_error = f"scope decomposition: {reason}"

        task = self.store.update_task(run_id, task_id, _hold)
        self._set_ref_state(run_id, task_id, task.state)
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "scope_decomposition_held", "level": "warning",
             "run_id": run_id, "task_id": task_id, "reason": reason,
             "mapping": task.decomposition_mapping},
        )
        return task

    def _complete_ready_umbrellas(self, run_id: str) -> None:
        """Complete umbrella parents whose leaf children all completed successfully.

        Pending parent stages are marked skipped, completion is propagated to the task
        source, and resources are released without dispatching implementation work for
        the umbrella itself.  A failed leaf is handled by normal DAG failure cascading.
        """
        run = self.store.load_run(run_id)
        states = {ref.task_id: ref.state for ref in run.task_refs}
        for ref in list(run.task_refs):
            if ref.state in TERMINAL_TASK_STATES:
                continue
            parent = self.store.load_task(run_id, ref.task_id)
            if not parent.decomposition_children:
                continue
            leaves = run.dependency_graph.get(parent.task_id, [])
            if not leaves or any(states.get(child) is not TaskState.COMPLETED for child in leaves):
                continue

            def _complete(task: Task) -> None:
                for rec in task.stages.values():
                    if rec.status is StageStatus.PENDING:
                        rec.status = StageStatus.SKIPPED
                task.state = TaskState.COMPLETED

            parent = self.store.update_task(run_id, parent.task_id, _complete)
            self._set_ref_state(run_id, parent.task_id, TaskState.COMPLETED)
            self.store.write_task_index(parent.task_id, render_task_index(parent))
            self._on_task_completed(run_id, parent)
            self._release_ports(run_id, parent)
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "decomposition_parent_completed",
                 "run_id": run_id, "task_id": parent.task_id,
                 "children": parent.decomposition_children},
            )

    def _stage_gate(self, result: StageResult) -> str | None:
        """Deterministic per-stage gate over a SUCCESS result; returns a veto reason or None.

        The collapsed test stage folds in the as-built 'test-validate' step (§6.1): a
        green run is necessary but not sufficient — the tests must actually exercise the
        change. The runner is asked to self-report ``tests_meaningful``; if it explicitly
        reports ``false`` we veto (retry-with-learnings) rather than ship vacuous tests.

        Fail-OPEN on a MISSING or NULL field: nothing enforces this soft field on the
        interactive/headless lanes (no JSON schema), so a model that simply omits it — or a
        runner that honestly reports ``null`` because it cannot judge meaningfulness (the
        deterministic ENGINE lane, #261) — must not dead-end otherwise-green work. Only an
        explicit ``false`` is a veto. An abstention here is not free, though: if REVIEW
        abstains too, ``record`` emits a warning-grade ``test_validation_skipped`` event so
        the run never *claims* a verification nobody performed. (A stronger, schema- or
        independent-reviewer-enforced version is tracked as issue #13.)

        The veto downgrades the result to a FAILURE, so ``apply_result`` never absorbs its
        outputs — which is why ``_failure_learning`` carries the vetoed attempt's
        ``validation_notes`` forward into the retry's prompt explicitly (#298), the way a
        REVIEW rejection carries its blocking ``issues``. The veto string below is
        load-bearing (``result.error`` → ``error_signature`` → the breaker's identical-
        failure streak → ``task.last_error``): enrich the learning, not this reason."""
        if result.stage is Stage.SCOPE:
            try:
                parse_subtasks(result.structured_output)
            except DecompositionError as exc:
                return f"scope decomposition gate: {exc}"
            return None
        if result.stage is Stage.DELIVER:
            return pr_not_opened(result.structured_output)
        if result.stage is not Stage.TEST:
            return None
        out = result.structured_output or {}
        if out.get("tests_meaningful") is False:  # explicit self-report only
            return (
                "test-validate gate: the runner reported the tests do not meaningfully "
                "exercise the change (tests_meaningful=false). Add/adjust assertions so "
                "the tests would fail if this change regressed."
            )
        return None

    def _scope_not_feasible(self, result: StageResult) -> str | None:
        """Feasibility gate over a completed SCOPE stage (issue #45); returns the
        blocked_reason to park on, or None to proceed.

        The scope contract reports ``feasible`` / ``blocked_reason``. When a green scope
        result explicitly reports ``feasible=false`` the task is genuinely blocked, so
        advancing to implement would only produce a no-op — instead we park it for a
        human (BLOCKED_ON_HUMAN).

        Fail-OPEN on a MISSING/true field, mirroring ``_stage_gate``: only an explicit
        ``feasible=false`` parks; a result that omits the field (or reports true) advances
        as before, so a soft/unschema'd field never dead-ends otherwise-feasible work."""
        if result.stage is not Stage.SCOPE:
            return None
        out = result.structured_output or {}
        if out.get("feasible") is False:  # explicit self-report only
            reason = out.get("blocked_reason")
            if isinstance(reason, str) and reason.strip():
                return reason
            return "scope reported the task is not feasible (feasible=false)"
        return None

    def _merge_policy_findings(self, run_id: str, task: Task, result: StageResult) -> StageResult:
        """Fold the project's deterministic ``review_findings`` into a REVIEW result
        (#65 — the seam the old e2e-policy gate / API-contract trigger / TSC gate
        family lost in the rebuild). Duck-typed and best-effort: no hook, an empty
        list, or a raising hook (evented) leaves the result untouched.

        Finding shape: ``{description, severity?, file?, line?, suggested_fix?,
        blocking?=True}``. Blocking findings join ``issues`` and force
        ``approved=false`` (severity defaults to critical so a repeated policy finding
        can never convergence-auto-approve past the gate); non-blocking ones join
        ``non_blocking`` and get filed as follow-up issues at finalize."""
        hook = getattr(self.project, "review_findings", None)
        if not callable(hook):
            return result
        try:
            findings = [f for f in (hook(worktree=task.context.get("worktree")) or [])
                        if isinstance(f, dict) and str(f.get("description") or "").strip()]
        except Exception as exc:  # noqa: BLE001 - a policy hook must never break record()
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "policy_findings_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
            return result
        if not findings:
            return result
        out = dict(result.structured_output or {})
        blocking = [f for f in findings if f.get("blocking", True)]
        advisory = [f for f in findings if not f.get("blocking", True)]
        if blocking:
            issues = list(out.get("issues") or [])
            for f in blocking:
                issue = {k: v for k, v in f.items() if k != "blocking"}
                issue.setdefault("severity", "critical")  # a policy gate is a hard gate
                issues.append(issue)
            out["issues"] = issues
            out["approved"] = False  # deterministic override of the model's approval
        if advisory:
            nb = list(out.get("non_blocking") or [])
            nb.extend(
                {"title": str(f.get("description"))[:80],
                 "detail": format_review_issue({k: v for k, v in f.items() if k != "blocking"}),
                 "disposition": "file"}
                for f in advisory
            )
            out["non_blocking"] = nb
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "policy_findings_merged", "run_id": run_id,
             "task_id": task.task_id, "blocking": len(blocking), "advisory": len(advisory),
             "findings": [format_review_issue(f)[:200] for f in findings[:10]]},
        )
        return result.model_copy(update={"structured_output": out})

    @staticmethod
    def _issue_fingerprint(issue: object) -> str:
        """Stable convergence key for one blocking issue (#15, ports the as-built
        ``file:description`` fingerprint, OC:993-999): normalized so cosmetic rewording
        of the same finding still matches.

        The rule itself now lives in ``review_workflow.issue_fingerprint`` (named
        ``fingerprint-v1`` on a ``ReviewPlan``) because the multi-agent panel must
        normalize identically to address a verdict at a finding (#73); this stays as the
        engine's spelling of the same call so convergence math reads unchanged."""
        return issue_fingerprint(issue)

    def _review_verdict(self, result: StageResult, task: Task) -> dict | None:
        """Interpret a completed REVIEW stage's verdict; None when there is nothing to act
        on (not the review stage, or approved / field omitted — fail-OPEN like the other
        soft gates: only an explicit ``approved=false`` triggers).

        Convergence auto-approval (#15, ports OC:985-1022): a re-review AFTER a fix
        cycle whose blocking issues are a SUBSET of the previous rejection's (no
        net-new findings) has converged — the loop is no longer finding new problems,
        so it auto-approves rather than parking. Guarded: never with a critical-severity
        issue, never over a vacuous-tests verdict, never on the first rejection.

        Restores the old severity gate (as-built ``orchestrator-common.sh:965``): when every
        blocking issue is a structured object explicitly marked ``severity=suggestion``,
        the rejection auto-approves (kind="auto_approved") instead of cycling — suggestions
        must not hold up an otherwise-approved PR.

        Also the independent test-validate half (#13): the reviewer — a different agent
        from the one that wrote/ran the tests — reports ``tests_meaningful``; an explicit
        ``false`` REJECTS even an approved review (vacuous-green tests are exactly what
        the self-graded TEST gate can't catch about itself). Fail-open when omitted.

        The #13 gate is suppressed for tasks with NO model test surface (#41/#168): i.e.
        when ``change_class == "docs-only"`` (ENGINE-detected, not model-asserted), when
        ``Stage.TEST`` is absent from the task's pipeline (micro tasks), or when TEST ran
        on the deterministic ENGINE lane (opted in via ``deterministic_stages``). A model
        cannot self-exempt: all three signals are fixed at ``add_task`` time. An explicit
        ``approved=false`` still rejects normally — the exemption covers only the vacuous-
        tests criterion, never a substantive reviewer rejection. When the exemption discards
        an EXPLICIT reviewer ``false``, that suppression is no longer silent: ``record``
        emits a warning-grade ``test_validation_skipped`` event for it (#261). An OMITTED /
        null ``tests_meaningful`` still fails open here — and is likewise evented when the
        TEST stage did not judge it either."""
        if result.stage is not Stage.REVIEW:
            return None
        out = result.structured_output or {}
        approved_false = out.get("approved") is False  # explicit self-report only
        # #41/#168: a task with NO model test surface has nothing for the #13 independent
        # test-validate gate to bite on, so a `tests_meaningful=false` verdict must NOT reject
        # it for lacking meaningful new tests. Two engine-side, non-fabricatable signals qualify
        # (both derived from add_task-time state, never from a model's output — so a model can't
        # self-exempt): (a) #41 a deterministically-detected docs-only change (ENGINE-lane-only
        # `change_class` tag); (b) #168 the TEST stage did not run on a model lane — absent from
        # the pipeline (micro) or opted into the deterministic $0 ENGINE runner (#33/#68), so no
        # model wrote/graded the tests the reviewer would be judging. An explicit `approved=false`
        # still rejects normally: this relaxes only the tests criterion, never a substantive reject.
        # The predicate itself is the pure ``state_machine.no_model_test_surface`` so the
        # #261 test_validation_skipped notice reads the SAME exemption (it reports when this
        # suppresses an explicit reviewer `false` — no second spelling to drift).
        tests_vacuous = (
            out.get("tests_meaningful") is False and not no_model_test_surface(task)
        )
        if not approved_false and not tests_vacuous:
            return None
        raw = out.get("issues")
        issues = raw if isinstance(raw, list) else []
        issues_text = [format_review_issue(i)[:300] for i in issues[:10]]
        if tests_vacuous:
            issues_text.append(
                "independent test-validate (#13): the reviewer judged the tests do not "
                "meaningfully exercise this change — add/adjust assertions so they "
                "would fail if the change regressed"
            )
        suggestions_only = (
            not tests_vacuous
            and bool(issues)
            and all(
                isinstance(i, dict) and str(i.get("severity", "")).lower() == "suggestion"
                for i in issues
            )
        )
        fingerprints = [self._issue_fingerprint(i) for i in issues]
        has_critical = any(
            isinstance(i, dict) and str(i.get("severity", "")).lower() == "critical"
            for i in issues
        )
        converged = (
            not tests_vacuous
            and not has_critical
            and task.review_cycles > 0  # only a re-review after a fix can converge
            and bool(fingerprints)
            and set(fingerprints) <= set(task.last_review_rejection)
        )
        if suggestions_only:
            kind = "auto_approved"
        elif converged:
            kind = "converged_auto_approved"
        else:
            kind = "rejected"
        return {"kind": kind, "issues_text": issues_text, "fingerprints": fingerprints}

    @staticmethod
    def _review_fixup(result: StageResult) -> ReviewFixup | None:
        """Return a bounded in-place improvement request from a successful REVIEW.

        The interactive lane is intentionally tolerated at this seam, so model fields are
        coerced and bounded just like evidence-out fields instead of assuming schema-perfect
        strings.  Only the explicit ``fixup`` disposition acts; every other improvement
        keeps the existing filing/completion-note behavior.
        """
        if result.stage is not Stage.REVIEW or result.status is not ResultStatus.SUCCESS:
            return None
        output = result.structured_output or {}
        improvement = output.get("improvement")
        if not isinstance(improvement, dict):
            return None
        disposition = str(improvement.get("disposition") or "").strip().casefold()
        title = str(improvement.get("title") or "").strip()
        if disposition != "fixup" or not title:
            return None
        title = title[:200]
        detail = str(improvement.get("detail") or "").strip()[:2000]
        return ReviewFixup(
            title=title,
            detail=detail,
            fingerprint=issue_fingerprint(title),
        )

    @staticmethod
    def _fixup_learning(fixup: ReviewFixup, *, repeated: bool = False) -> str:
        prefix = "review improvement fixup still requested" if repeated else "review improvement fixup"
        detail = f" — {fixup.detail}" if fixup.detail else ""
        return f"{prefix}: {fixup.title}{detail}"

    def _remember_review_fixup(self, task: Task, fixup: ReviewFixup) -> bool:
        """Put a fixup into the durable task/history plane and IMPLEMENT learnings.

        Returns True for a newly-recorded request.  A blocking rejection may revisit an
        already-scheduled fixup while independently earning another normal review cycle;
        carry that reminder into IMPLEMENT but do not duplicate the durable record.
        """
        existing = next(
            (saved for saved in task.review_fixups if saved.fingerprint == fixup.fingerprint),
            None,
        )
        task.learnings.append(self._fixup_learning(fixup, repeated=existing is not None))
        if existing is not None:
            return False
        task.review_fixups.append(fixup.model_copy(deep=True))
        return True

    def _apply_review_fixup(self, task: Task, fixup: ReviewFixup) -> tuple[str, str]:
        """Schedule one approved-review fixup or hold an unsafe/repeated request.

        Reuses the existing bounded review-cycle budget and tail reset.  The standard
        pipelines all contain an IMPLEMENT→DELIVER→REVIEW tail; a bespoke pipeline
        without that order cannot honestly update and re-check the PR, so it parks instead
        of publishing the old false "applied" completion note.  A repeated fingerprint
        likewise parks immediately: the first pass already had this exact instruction and
        another blind loop would be lossy.
        """
        if any(saved.fingerprint == fixup.fingerprint for saved in task.review_fixups):
            reason = "same fixup was requested again after its re-implement pass"
            return self._hold_review_fixup(task, fixup, reason), reason
        ineligible = self._review_fixup_tail_ineligibility(task)
        if ineligible is not None:
            return self._hold_review_fixup(task, fixup, ineligible), ineligible
        if task.review_cycles >= self.max_review_cycles:
            reason = f"review rework budget exhausted ({task.review_cycles} cycles)"
            return self._hold_review_fixup(task, fixup, reason), reason

        # Same session boundary as a rejecting review: an implementer must act from the
        # durable instruction, not continue inside the reviewer context that proposed it.
        task.session_ref = None
        task.session_provider = None
        reset = reset_for_fix_cycle(task, Stage.IMPLEMENT)
        if not reset:  # defensive counterpart to the pipeline membership guard above
            reason = "could not reset the pipeline from IMPLEMENT"
            return self._hold_review_fixup(task, fixup, reason), reason
        self._remember_review_fixup(task, fixup)
        task.review_cycles += 1
        task.state = TaskState.RETRYING
        return "review_fixup_cycle", "re-running implement through review in place"

    @staticmethod
    def _review_fixup_tail_ineligibility(task: Task) -> str | None:
        """Return a hold reason unless a fixup can be reimplemented and re-delivered.

        A fixup becomes durable application evidence only after the pipeline can run
        IMPLEMENT, DELIVER, and REVIEW in that order.  Both standalone and combined
        rejection/fixup paths use this result so bespoke pipelines surface an audit hold
        instead of later claiming an un-delivered change was applied.
        """
        required_tail = (Stage.IMPLEMENT, Stage.DELIVER, Stage.REVIEW)
        if not all(stage in task.pipeline for stage in required_tail):
            return "task pipeline has no IMPLEMENT→DELIVER→REVIEW tail for an in-place fixup"
        positions = tuple(task.pipeline.index(stage) for stage in required_tail)
        if positions != tuple(sorted(positions)):
            return "task pipeline does not order IMPLEMENT→DELIVER→REVIEW for a fixup"
        return None

    @staticmethod
    def _hold_review_fixup(task: Task, fixup: ReviewFixup, reason: str) -> str:
        """Park a fixup the engine cannot safely auto-apply, leaving REVIEW resumable."""
        rec = task.stages[Stage.REVIEW]
        rec.status = StageStatus.FAILED
        rec.error = f"review fixup held: {fixup.title} — {reason}"[:500]
        task.last_error = rec.error
        task.state = TaskState.BLOCKED_ON_HUMAN
        return "review_fixup_held"

    def _apply_review_rejection(self, task: Task, verdict: dict) -> str:
        """Dispose of a rejected review: a bounded fix cycle (re-open implement→…→review
        with the blocking issues as learnings) while cycles remain, else park the task at
        the human gate with the REVIEW record re-opened as FAILED — so an approve() leads
        to a re-review, never a zombie task with no next stage."""
        summary = "; ".join(verdict["issues_text"]) or "no issues listed"
        task.learnings.append(
            f"review rejected (cycle {task.review_cycles + 1}) — blocking issues: {summary}"
        )
        # The convergence key (#15): the NEXT re-review compares its issues against
        # this rejection's fingerprints — a subset (no net-new findings) auto-approves.
        task.last_review_rejection = list(verdict.get("fingerprints") or [])
        # Fix work must not inherit the reviewer's session (same rationale as warm-retry
        # OFF: a rejecting session's context is as likely poisoned as useful).
        task.session_ref = None
        task.session_provider = None
        if task.review_cycles < self.max_review_cycles:
            reset = reset_for_fix_cycle(task, Stage.IMPLEMENT)
            if reset:
                task.review_cycles += 1
                task.state = TaskState.RETRYING
                verdict["disposition"] = "fix_cycle"
                return "review_rejected_fix_cycle"
        # Cycles exhausted (or no implement stage in this pipeline to fix with): park.
        # Flip the (apply_result-completed) REVIEW record to FAILED so the pipeline still
        # has a next stage — after approve(), next_work re-dispatches REVIEW.
        rec = task.stages[Stage.REVIEW]
        rec.status = StageStatus.FAILED
        rec.error = f"review rejected: {summary}"[:500]
        task.last_error = rec.error
        task.state = TaskState.BLOCKED_ON_HUMAN
        verdict["disposition"] = "held"
        return "review_rejected_held"

    def _apply_fallthrough(self, task: Task, result: StageResult, reason: str) -> str:
        """Re-route a codex stage to claude because the codex provider was out (#7).

        Called only when ``record`` has decided a fallthrough applies (run opted in, the failed
        dispatch actually ran on codex, and this stage hasn't fallen through before). Mirrors
        the rate-limit re-queue's shape — the provider was unavailable, NOT the task, so the
        stage attempt is KEPT (RUNNING marker, no breaker, no retry burned) rather than counted
        against the task's budget. What it changes:

        - Marks the stage in ``fallthrough_stages`` so the Router picks claude on the next
          dispatch (one-way; a stage already here never routes back to codex).
        - Clears the codex ``session_ref``/``session_provider`` (#9): a codex thread id means
          nothing to claude, and next_work's provider-gate would drop it anyway — we clear it
          explicitly (and regression-test it) so the codex context never leaks to the claude
          attempt.
        - Resets the rate-limit / fallback-model state: a DIFFERENT provider gets a fresh
          cooldown budget and starts at claude's role-default model (no queued codex fallback).
        - Appends a learning so the claude attempt knows the prior codex context is gone and to
          work from the task description + learnings. The context/learnings plane itself is
          provider-neutral and carries over untouched; the pre-dispatch checkpoint reset (a
          RUNNING re-queue with no salvage) starts claude from the last good checkpoint.
        """
        stage = result.stage
        rec = task.stages[stage]
        rec.status = StageStatus.RUNNING  # provider was out, not the task — keep the attempt
        rec.completed_at = None
        rec.error = None
        task.state = TaskState.RETRYING
        task.updated_at = _now()
        task.fallthrough_stages = (*task.fallthrough_stages, stage)
        # The codex session must NOT ride onto the claude dispatch (#9).
        task.session_ref = None
        task.session_provider = None
        task.pending_fallback_model = None  # claude starts fresh at its role default
        task.not_before = None
        task.rate_limit_waits = 0  # a different provider — a fresh cooldown budget
        task.learnings.append(
            f"{stage.value} (attempt {result.attempt}): the codex provider was UNAVAILABLE "
            f"({reason}) — this stage is re-routed to claude. Any prior codex session/context "
            f"is NOT carried over; work from the task description and the learnings above."
        )
        return "provider_fallthrough"

    def _apply_salvage(
        self, task: Task, result: StageResult, *, infra_only: bool = False
    ) -> None:
        """Salvage decision (#59): keep or discard the work a failed/timed-out attempt
        COMMITTED past the checkpoint, by failure KIND. Pure state — the git report was
        produced by the runner-side wrapper (``result.salvage``); this only reads it, sets
        the task's flag/counter, appends a retry learning, and emits an event. Called from
        both the failure path and the transient rate-limit re-queue. Silent (no flag, no
        event) when the attempt committed nothing past the anchor — a no-commits failure
        resets plainly. Uncommitted/dirty scraps are never salvaged (the wrapper reports
        commits only)."""
        salvage = result.salvage
        commits = list((salvage or {}).get("commits") or [])
        if not commits:
            return  # nothing committed past the checkpoint — plain reset, no salvage noise
        salvageable = self._work_not_implicated(result, infra_only=infra_only)
        shas = [str(c.get("sha", ""))[:9] for c in commits]
        total = int((salvage or {}).get("count") or len(commits))
        if salvageable and task.salvage_count < self.max_salvage_keeps:
            task.salvage_count += 1
            task.salvage_in_place = True  # next_work suppresses the reset -> work is kept
            shown = [
                f"{s} {str(c.get('subject', '')).strip()}"
                for s, c in zip(shas, commits, strict=True)
            ][:10]
            more = f"\n  (+{total - len(shown)} more)" if total > len(shown) else ""
            task.learnings.append(
                f"{result.stage.value} (attempt {result.attempt}): the previous attempt "
                f"COMMITTED work before it failed ({result.status.value}) — that work was "
                f"KEPT in your worktree, NOT discarded. Review these commits, keep what is "
                f"good, and fix what is not:\n  " + "\n  ".join(shown) + more
            )
            self.store.append_event(
                task.run_id,
                {"ts": _now(), "type": "salvage_kept", "run_id": task.run_id,
                 "task_id": task.task_id, "stage": result.stage.value,
                 "kind": result.status.value, "attempt": result.attempt, "shas": shas,
                 "count": total, "keeps_used": task.salvage_count,
                 "keeps_budget": self.max_salvage_keeps},
            )
        else:
            # Not a salvageable kind (the committed code may BE the defect — e.g. a real
            # test failure) OR the keep budget is spent (kept once already and it didn't
            # unstick the retry): discard by leaving the flag clear so next_work resets to
            # the checkpoint as usual — no infinite pile of half-work.
            task.salvage_in_place = False
            reason = "budget_exhausted" if salvageable else "kind_not_salvageable"
            self.store.append_event(
                task.run_id,
                {"ts": _now(), "type": "salvage_discarded", "run_id": task.run_id,
                 "task_id": task.task_id, "stage": result.stage.value,
                 "kind": result.status.value, "attempt": result.attempt, "shas": shas,
                 "reason": reason, "keeps_used": task.salvage_count,
                 "keeps_budget": self.max_salvage_keeps},
            )

    @staticmethod
    def _work_not_implicated(result: StageResult, *, infra_only: bool) -> bool:
        """Shared #59/#8 predicate: does the failure KIND leave the prior attempt's
        artifacts trustworthy? True for a mechanical/environmental failure — a TIMEOUT or
        RATE_LIMITED (``SALVAGEABLE_FAILURE_STATUSES``) or an infra-classified one — where
        the WORK wasn't the problem; False for a content failure (SCHEMA_VIOLATION, a real
        test FAILURE, a review rejection) where the produced code/context may BE the defect.
        Salvage (keep the commits, #59) and warm retry (keep the session, #8) both hang off
        this one question, so the kind-set lives in exactly one place."""
        return result.status in SALVAGEABLE_FAILURE_STATUSES or infra_only

    def _settle_failed_session(
        self, task: Task, result: StageResult, *, run: Run, infra_only: bool, retrying: bool
    ) -> None:
        """Decide the fate of a failed attempt's provider session (warm-retry policy #8).

        DEFAULT is to clear it — the 2026-07-01 design pass (§2) decided fresh-after-failure
        because a failed attempt's context is as likely poisoned as useful, and the learnings
        already carry the distilled failure forward. Warm retry is the explicit, conservative
        opt-in: KEEP the session only when ALL of

          (a) the run opted in (``run.warm_retry``);
          (b) a retry actually follows (``retrying`` — a terminal fail-out reuses nothing);
          (c) the failure is mechanical/not-content (``_work_not_implicated`` — TIMEOUT,
              RATE_LIMITED, infra — never a SCHEMA_VIOLATION or a genuine test/review FAILURE);
          (d) the session's provider matches the provider the retry will route to (no
              cross-provider warmth — a #7 fallthrough re-routes the stage, so its session
              won't match and the retry is cold, composing correctly);
          (e) the WORKTREE still matches what the session remembers — see below.

        The crux (e), worktree/session coupling: a git (checkpoint) stage hard-resets the
        worktree to the last good checkpoint on retry UNLESS salvage kept the committed work.
        A warm session WITHOUT that salvage is a trap — the model's conversation remembers
        edits the reset just threw away. So on a checkpoint stage we keep the session only
        when salvage kept the work (``task.salvage_in_place``); a non-checkpoint stage (SCOPE
        / REVIEW — never resets the tree) is always safe. Net: warm session composes WITH
        salvage on git stages, and stands alone only where no reset can diverge them.

        The transports' session-lost fallback (a stale/gc'd id cold-starts a fresh call inside
        the same dispatch) remains the safety net if the kept id has since expired."""
        keep = (
            retrying
            and run.warm_retry
            # There must actually BE a session to carry — else "keep" is a no-op that would
            # emit a misleading warm_retry_used event (e.g. a deterministic/first stage).
            and bool(result.session_ref or task.session_ref)
            and self._work_not_implicated(result, infra_only=infra_only)
            and self._warm_session_provider_ok(task, result)
            and self._warm_worktree_ok(task, result)
        )
        if not keep:
            task.session_ref = None
            task.session_provider = None
            return
        # Keep warm. Prefer the failed dispatch's OWN reported ref (the same-stage session it
        # was mid-work in); if the runner reported none, leave the threaded-in ref in place
        # (same "no ref reported keeps the prior one" rule as the success path).
        if result.session_ref:
            task.session_ref = result.session_ref
            task.session_provider = result.lane_used.provider
        kind = result.status.value
        task.learnings.append(
            f"{result.stage.value} (attempt {result.attempt}): WARM RETRY — you are resuming "
            f"the failed attempt's own session, so its conversation context is intact. The "
            f"failure was {kind} (mechanical, not a content problem), NOT a rejection of the "
            f"work. Re-verify the prior work already in your worktree before continuing; if the "
            f"session has expired the tools will simply start fresh."
        )
        self.store.append_event(
            task.run_id,
            {"ts": _now(), "type": "warm_retry_used", "run_id": task.run_id,
             "task_id": task.task_id, "stage": result.stage.value, "kind": kind,
             "attempt": result.attempt,
             "session_provider": (
                 task.session_provider.value if task.session_provider else None
             ),
             "session_ref": task.session_ref},
        )

    def _warm_session_provider_ok(self, task: Task, result: StageResult) -> bool:
        """(d): the session to reuse must be resumable by the retry's lane. The session was
        produced on ``result.lane_used`` (when the dispatch reported a ref) or carried over
        from a prior success (``task.session_provider``); the retry routes via the Router
        (which reflects any #7 fallthrough already recorded). Mirrors next_work's provider
        gate so the ``warm_retry_used`` event never claims warmth next_work would then drop."""
        retry_provider = self.router.lane_for(result.stage, task).provider
        sess_provider = result.lane_used.provider if result.session_ref else task.session_provider
        return sess_provider in (None, Provider.NONE, retry_provider)

    @staticmethod
    def _warm_worktree_ok(task: Task, result: StageResult) -> bool:
        """(e): keep the session only when the retry's worktree will still match it. A
        checkpoint (git-affecting) stage resets the tree to the last good checkpoint on
        retry unless salvage kept the work — so require ``salvage_in_place`` there. A
        non-checkpoint stage never resets the tree, so the session can't have diverged."""
        if not STAGE_SPECS[result.stage].checkpoint:
            return True
        return task.salvage_in_place

    def _handle_failure(self, task: Task, result: StageResult, *, run: Run) -> str:
        failures = None
        if result.structured_output:
            failures = result.structured_output.get("failures")
        sig = error_signature(result.stage, failures=failures, error=result.error)
        # Best-effort taxonomy over a TEST failure via the project's classifier. An
        # infra-classed failure is an environment problem, not a code problem: it must
        # not stack the breaker's identical-code-failure streak, and (the #14 loop,
        # below) it earns an environment reset + a free re-run of the SAME attempt
        # instead of burning the retry budget on a broken runner.
        classified = self._classify_failure(result)
        infra_only = bool(classified) and all(f.kind is FailureKind.INFRA for f in classified)
        if not infra_only:
            task.error_signatures.append(sig)
        baseline = task.context.get("baseline_failures")
        task.learnings.append(
            self._failure_learning(result, failures, classified, infra_only, baseline)
        )
        if classified:
            self.store.append_event(
                task.run_id,
                {"ts": _now(), "type": "failure_classified", "run_id": task.run_id,
                 "task_id": task.task_id, "stage": result.stage.value,
                 "kinds": sorted({f.kind.value for f in classified}),
                 "infra_only": infra_only},
            )

        # Salvage (#59): before any reset decision, if the failure's KIND doesn't implicate
        # the committed work (timeout / infra), KEEP the commits the attempt made past the
        # checkpoint so the retry builds on them. Sets the flag next_work reads to suppress
        # the reset. Runs for both the infra-reset re-run and the ordinary retry below.
        self._apply_salvage(task, result, infra_only=infra_only)

        # Infra-failure reset loop (#14, ports OC:3835-3860): reset the environment via
        # the project's infra_reset command, then re-run the SAME attempt (RUNNING marker
        # keeps the attempt number — a broken runner must not consume the code-fix
        # budget). Bounded by max_infra_resets; past the budget it falls through to the
        # normal failure path (the old persistent_infra_failure halt).
        if infra_only and task.infra_resets < self.max_infra_resets:
            reset_note = self._run_infra_reset(task)
            task.infra_resets += 1
            rec = task.stages[result.stage]
            rec.status = StageStatus.RUNNING
            rec.completed_at = None
            rec.error = None
            task.state = TaskState.RETRYING
            task.learnings.append(
                f"{result.stage.value} (attempt {result.attempt}): infrastructure "
                f"failure — environment reset ({reset_note}), re-running the same "
                f"attempt ({task.infra_resets}/{self.max_infra_resets} resets used)"
            )
            self.store.append_event(
                task.run_id,
                {"ts": _now(), "type": "infra_reset", "run_id": task.run_id,
                 "task_id": task.task_id, "stage": result.stage.value,
                 "reset_result": reset_note, "resets_used": task.infra_resets,
                 "resets_budget": self.max_infra_resets},
            )
            self._settle_failed_session(task, result, run=run, infra_only=infra_only, retrying=True)
            return "stage_infra_reset_retry"

        # Reuse the tested CircuitBreaker over the persisted signature streak.
        breaker = CircuitBreaker(self.breaker_threshold)
        for s in task.error_signatures:
            breaker.observe(s)
        # result.attempt is trustworthy: content_hash (which includes attempt) was
        # validated against the dispatched WorkItem, so it equals the engine's count.
        attempts_done = result.attempt + 1
        if attempts_done >= task.max_attempts or breaker.tripped:
            task.state = TaskState.FAILED
            task.error_signatures = []  # don't carry a poisoned streak into a re-queue
            # Terminal: no retry follows, so the session is always cleared (retrying=False).
            self._settle_failed_session(task, result, run=run, infra_only=infra_only, retrying=False)
            return "task_failed_breaker" if breaker.tripped else "task_failed_max_attempts"
        task.state = TaskState.RETRYING
        self._settle_failed_session(task, result, run=run, infra_only=infra_only, retrying=True)
        return "stage_failed_will_retry"

    def _run_infra_reset(self, task: Task) -> str:
        """Shell the project's ``infra_reset`` command in the task's worktree
        (best-effort, bounded). A deterministic project command, not a model call —
        the same class of work the ENGINE lane's setup runner does."""
        getter = getattr(self.project, "infra_reset", None)
        try:
            argv = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 - a project command surface must never break failure handling
            argv = None
        if not argv or argv == ["true"]:  # the no-op sentinel
            return "skipped (no infra_reset command)"
        cwd = task.context.get("worktree")
        if not (cwd and Path(cwd).is_dir()):  # a recorded-but-gone worktree must not
            cwd = None  # turn the reset itself into a FileNotFoundError
        try:
            proc = subprocess.run(  # noqa: S603
                argv, cwd=cwd, capture_output=True, text=True, timeout=300
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"error ({type(exc).__name__})"
        return "ok" if proc.returncode == 0 else f"rc={proc.returncode}"

    def _classify_failure(self, result: StageResult) -> list:
        """Run the project's failure classifier over a failed TEST result's output
        (raw output + error + the structured failing-test ids). Best-effort and
        engine-agnostic: no classifier / no text / a raising classifier all yield []."""
        if result.stage is not Stage.TEST:
            return []
        classifier = getattr(self.project, "classifier", None)
        if classifier is None:
            return []
        parts = [result.raw_output or "", result.error or ""]
        out = result.structured_output or {}
        if isinstance(out.get("failures"), list):
            parts.append("\n".join(str(f) for f in out["failures"]))
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            return []
        try:
            return list(classifier.classify(text))
        except Exception:  # noqa: BLE001 - classification must never break failure handling
            return []

    @staticmethod
    def _failure_learning(
        result: StageResult,
        failures: object,
        classified: list,
        infra_only: bool,
        baseline: object = None,
    ) -> str:
        """One failed attempt's learning entry. Richer than a bare error string (the old
        system carried the stage trail + a log tail): the failing-test ids (kind-tagged
        when classified) and a bounded tail of the runner's output, so the retry starts
        from the failure's substance instead of re-discovering it.

        #298 — the vacuity-rejection asymmetry, closed here: a REVIEW rejection already
        carries its blocking ``issues`` forward as learnings (``_apply_review_rejection``),
        which is what makes the fix cycle converge. A TEST vacuity veto
        (``_stage_gate``, ``tests_meaningful=false``) used to carry NOTHING: the gate
        downgrades a SUCCESS to a FAILURE, so ``apply_result`` never absorbs the result's
        outputs and ``validation_notes`` — the only channel naming *why* the attempt
        rejected — was dropped. The retry then re-derived the diagnosis from scratch, or
        (worse) passed on a second look and converted a caught vacuity into a shipped one.
        So a TEST failure whose result explicitly self-reports ``tests_meaningful=false``
        appends the prior ``validation_notes`` verbatim (bounded) plus the convergence
        directive: close the named gap, treat already-confirmed coverage as settled."""
        lines = [f"{result.stage.value} (attempt {result.attempt}): {result.error or 'failed'}"]
        kind_by_test = {f.test: f.kind.value for f in classified}
        if isinstance(failures, list) and failures:
            shown = [str(f)[:200] for f in failures[:10]]
            tagged = [f"{t} [{kind_by_test[t]}]" if t in kind_by_test else t for t in shown]
            more = f" (+{len(failures) - 10} more)" if len(failures) > 10 else ""
            lines.append(f"  failing: {'; '.join(tagged)}{more}")
            # Deterministic regression-vs-inherited split against the intake baseline
            # (ADR-035 parity): the retry must chase regressions, never inherited red.
            if result.stage is Stage.TEST and isinstance(baseline, list) and baseline:
                base_set = {str(b) for b in baseline}
                inherited = [str(f) for f in failures if str(f) in base_set]
                regressions = [str(f) for f in failures if str(f) not in base_set]
                if inherited:
                    lines.append(
                        f"  inherited from base (already failing before this change — "
                        f"do NOT fix): {'; '.join(inherited[:10])}"
                    )
                    lines.append(
                        f"  true regressions to fix: "
                        f"{'; '.join(regressions[:10]) if regressions else '(none — all failures are inherited)'}"
                    )
        elif classified:
            lines.append(
                "  classified: "
                + "; ".join(f"{f.test} [{f.kind.value}]" for f in classified[:10])
            )
        if infra_only:
            lines.append(
                "  NOTE: classified as an INFRASTRUCTURE failure (env/ports/browser), "
                "not a code failure — consider resetting the test environment before "
                "re-diagnosing the change itself."
            )
        # #298: a TEST vacuity veto carries its reasoning forward, the way a REVIEW
        # rejection carries its blocking issues. Bounded to the explicit self-report
        # (never a missing/null tests_meaningful — #261 made the field tri-state) and to
        # a non-empty string note, so no empty header is ever emitted.
        notes = Engine._vacuity_notes(result)
        if notes is not None:
            clipped = notes[:1000]
            suffix = "…" if len(notes) > 1000 else ""
            lines.append(
                f"  why the previous attempt rejected — prior validation_notes: "
                f"{clipped}{suffix}"
            )
            lines.append(
                "  CLOSE the specific gap those notes name — do not merely re-derive it. "
                "Coverage the notes already confirm ADEQUATE is settled: do not redundantly "
                "re-verify or re-mutation-test it."
            )
        tail = (result.raw_output or "").strip()
        if tail:
            clipped = tail[-500:]
            prefix = "…" if len(tail) > 500 else ""
            lines.append(f"  output tail: {prefix}{clipped}")
        return "\n".join(lines)

    @staticmethod
    def _vacuity_notes(result: StageResult) -> str | None:
        """The prior attempt's ``validation_notes`` when — and only when — this failed
        result is a TEST stage that explicitly self-reported ``tests_meaningful=false``
        (#298). None for every other stage, for an absent/null tri-state field (#261), and
        for absent/blank/non-string notes, so the caller emits no empty header."""
        if result.stage is not Stage.TEST:
            return None
        out = result.structured_output or {}
        if out.get("tests_meaningful") is not False:  # explicit self-report only
            return None
        notes = out.get("validation_notes")
        if not isinstance(notes, str) or not notes.strip():
            return None
        return notes.strip()

    # --- human approval gate (design pass §4) ----------------------------------
    def hold_for_approval(self, run_id: str, task_id: str, what: str) -> Task:
        """Park a task at the human gate. Refuses while a dispatch is outstanding
        (record the in-flight result first — a held task must be quiescent). If the
        result can never arrive because the run was killed mid-dispatch, use
        ``abandon()`` to release the lease and drive the task terminal instead. ``what``
        is persisted on the task so approval releases the exact pending checkpoint."""

        def _hold(t: Task) -> None:
            if t.state in TERMINAL_TASK_STATES:
                raise ContractError(f"task {task_id} is terminal ({t.state.value}); cannot hold")
            if t.pending_work_item_id is not None:
                raise ContractError(
                    f"task {task_id} has an outstanding dispatch {t.pending_work_item_id}; "
                    f"record its result before holding"
                )
            t.state = TaskState.BLOCKED_ON_HUMAN
            t.pending_approval_what = what

        # #199: commit the task mutation + its transition event atomically (event
        # appended first, task doc last) so a crash can never leave a held task with no
        # held_for_approval event — the same orphan gap dispatch closed via this primitive.
        task = self.store.commit_task_events(
            run_id, task_id, _hold,
            [{"ts": _now(), "type": "held_for_approval", "run_id": run_id,
              "task_id": task_id, "what": what}],
        )
        self._set_ref_state(run_id, task_id, TaskState.BLOCKED_ON_HUMAN)
        return task

    def approve(self, run_id: str, task_id: str, *, approved_by: str, what: str = "") -> Task:
        """Release a held task. The durable ``approval-<run>-<task>.json`` artifact IS
        the gate record (who/when/what) — prose norms stay documentation. A pending hold's
        identity takes precedence over the optional caller note and is recorded in both
        the task's approved-hold set and the artifact, preventing approval reuse across
        different checkpoints."""

        approved_what = what

        def _release(t: Task) -> None:
            nonlocal approved_what
            if t.state is not TaskState.BLOCKED_ON_HUMAN:
                raise ContractError(
                    f"task {task_id} is not held for approval (state {t.state.value})"
                )
            approved_what = t.pending_approval_what or what
            if t.pending_approval_what and t.pending_approval_what not in t.approved_holds:
                t.approved_holds.append(t.pending_approval_what)
            t.pending_approval_what = None
            t.state = TaskState.PENDING

        def _approval_events(_t: Task) -> list[dict]:
            return [
                {"ts": _now(), "type": "approved", "run_id": run_id,
                 "task_id": task_id, "approved_by": approved_by, "what": approved_what}
            ]

        # #199: commit the release + its `approved` event atomically (event first, task
        # doc last), so a durable PENDING transition always has its event on disk. The
        # mutator's state guard runs inside the commit, so a rejected release still raises
        # BEFORE any approval artifact is written (no spurious gate record on the error path).
        task = self.store.commit_task_events(
            run_id, task_id, _release, _approval_events,
        )
        self.store.write_approval(
            run_id, task_id,
            {"approved_by": approved_by, "at": _now(), "what": approved_what, "run_id": run_id,
             "task_id": task_id},
        )
        self._set_ref_state(run_id, task_id, TaskState.PENDING)
        return task

    # --- alerting seam (#55) ----------------------------------------------------
    def emit_notification(self, run_id: str, kind: str, payload: dict) -> None:
        """Best-effort alerting: ALWAYS append a ``notification`` audit row (so the
        trail shows what was signalled even when no hook is installed), then call the
        project's optional duck-typed ``notify(kind, payload)`` hook.

        The hook is the seam the old bash monitor's email + desktop-notify plugs into.
        Like ``review_findings``/``publish_note`` it is getattr-called and NOT part of
        the versioned contract (no CONTRACT_VERSION bump). A raising hook is swallowed
        and evented (``notify_failed``) — an alert must NEVER break a run."""
        row = {"ts": _now(), "type": "notification", "run_id": run_id, "kind": kind}
        for key, value in payload.items():  # fold the payload for a self-describing row
            row.setdefault(key, value)
        self.store.append_event(run_id, row)
        hook = getattr(self.project, "notify", None)
        if not callable(hook):
            return
        try:
            hook(kind, payload)
        except Exception as exc:  # noqa: BLE001 - an alert hook must never break the run
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "notify_failed", "run_id": run_id,
                 "kind": kind, "error": str(exc)},
            )

    def _task_cost_facts(self, run_id: str, task_id: str) -> dict:
        """This task's metered spend, rolled up from the run's ledger rows (#359).

        #319-aware on purpose: an unmetered row contributes its ``cost_usd: 0.0`` to the
        total like any other, so the count of unmetered calls travels WITH the figure —
        without it an alert would render a confident ``$0.0000`` for a task whose usage was
        never recoverable, which reads as "free" when the truth is "unknown"."""
        rows = [r for r in self.run_rows(run_id) if r.get("task_id") == task_id]
        return {
            "usd": round(sum(r.get("cost_usd") or 0.0 for r in rows), 6),
            "invocations": len(rows),
            "unmetered_calls": sum(1 for r in rows if r.get("metered") is False),
        }

    def _notification_facts(self, run_id: str, task: Task) -> dict:
        """The shared, deterministic enrichment every per-task notification carries (#359):
        enough for a sink to render an ACTIONABLE alert — what landed, where the PR is, what
        it cost, and where the full trail lives — without the recipient re-opening `status`.

        Derived purely from durable state (the task doc + the cost ledger), so it is
        model-free and reproducible on replay. Load-bearing beyond email: the same richer
        payload improves the desktop sink and the ``notification`` audit row.

        TOTAL by construction — this runs INSIDE a terminal transition that has already
        mutated durable state and cannot be replayed, so an enrichment failure must never
        escape. The flat fields are plain attribute reads that cannot raise; the two derived
        blocks (stage roll-up, ledger scan — the latter does file I/O) are wrapped
        individually so one failing still yields the other, and a degraded payload is EVENTED
        rather than silently thinned (the "never silent" convention)."""
        facts: dict = {
            "task_id": task.task_id,
            "title": task.title or None,
            "issue_number": task.issue_number,
            "pr_url": task.pr_url,
            "pr_number": task.pr_number,
            "task_state": task.state.value,
            "attempt": task.attempt,
            "review_cycles": task.review_cycles,
            # Where the full trail lives — status/events.jsonl/stage-costs.jsonl/stages/.
            # Run logs are retained until the human prunes them, so this stays resolvable
            # long after the worktree and task branch are cleaned up.
            "run_dir": str(self.store.root),
        }
        try:
            facts["stages"] = [
                {
                    "stage": stage.value,
                    "status": rec.status.value,
                    "attempt": rec.attempt,
                    "model": rec.model,
                    "error": rec.error,
                }
                for stage, rec in task.stages.items()
                if rec.status is not StageStatus.PENDING
            ]
            review = task.stages.get(Stage.REVIEW)
            facts["review_approved"] = (review.output or {}).get("approved") if review else None
        except Exception as exc:  # noqa: BLE001 - enrichment must never break the transition
            self._event_facts_degraded(run_id, task, "stages", exc)
        try:
            facts["cost"] = self._task_cost_facts(run_id, task.task_id)
        except Exception as exc:  # noqa: BLE001 - a ledger read must never break the transition
            self._event_facts_degraded(run_id, task, "cost", exc)
        return facts

    def _event_facts_degraded(self, run_id: str, task: Task, part: str, exc: Exception) -> None:
        """Record that a notification payload went out MISSING one of its derived blocks —
        so a thin alert is explained by the trail instead of looking like there was nothing
        to report. Best-effort: even the event write is guarded, because this is the last
        line of defense inside an unreplayable terminal transition."""
        with contextlib.suppress(Exception):
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "notification_facts_degraded", "run_id": run_id,
                 "task_id": task.task_id, "part": part, "error": str(exc)},
            )

    # --- run pause (batch-wide circuit breaker, #58) ----------------------------
    def pause_run(self, run_id: str, reason: str) -> None:
        """Pause a run (no further scheduler dispatch until unpaused). Used by the
        batch-wide circuit breaker on systemic failure; also human-callable."""
        self.store.update_run(run_id, lambda r: setattr(r, "state", RunState.PAUSED))
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "run_paused", "run_id": run_id, "reason": reason},
        )

    def unpause_run(self, run_id: str, *, raise_budget_to: float | None = None) -> Run:
        """Release a paused run back to RUNNING (the human fixed the systemic cause).

        Budget-pause override (#34): a plain unpause of a run PAUSED because its budget was
        exhausted would immediately re-pause at the next dispatch (spend is still over the
        cap). So when the run is currently over budget, an unpause resolves it honestly:
          - ``raise_budget_to=X`` sets a NEW, higher ceiling and re-arms the soft warning
            (the human granted more budget — proceed until the new cap). Same contract as
            ``create_run``'s budget: a finite amount > 0, rejected here rather than stored
            as an unusable cap (#274);
          - otherwise the human is explicitly overriding the cap, so it is REMOVED
            (``budget_usd=None``) — no further hard stops this run.
        A breaker/other pause (not over budget) leaves the budget untouched, unless
        ``raise_budget_to`` is given explicitly."""
        raise_budget_to = _validated_budget(
            raise_budget_to, field="raise_budget_to", run_id=run_id
        )
        run = self.store.load_run(run_id)
        if run.state is not RunState.PAUSED:
            raise ContractError(f"run {run_id} is not paused (state {run.state.value})")
        over_budget = (
            run.budget_usd is not None
            and self.ledger.metered_spend(rows=self.run_rows(run_id)) >= run.budget_usd
        )

        def _mut(r: Run) -> None:
            r.state = RunState.RUNNING
            if raise_budget_to is not None:
                r.budget_usd = raise_budget_to
                r.budget_warning_sent = False  # re-arm the soft warning against the new cap
            elif over_budget:
                r.budget_usd = None  # explicit human override — drop the cap

        self.store.update_run(run_id, _mut)
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "run_unpaused", "run_id": run_id,
             "raised_budget_to": raise_budget_to,
             "budget_overridden": raise_budget_to is None and over_budget},
        )
        return self.store.load_run(run_id)

    # --- interactive supervisor context park (#259) ---------------------------
    def park_supervisor(
        self,
        run_id: str,
        *,
        reason: str,
        resume_command: str,
        context: dict | None = None,
    ) -> Run:
        """Park an interactive run only at a lease-free stage boundary.

        Unlike PAUSED (budget/breaker) and BLOCKED_ON_HUMAN (a decision gate), PARKED
        means the current Claude Code session lacks enough context to safely carry the
        next prompt. ``reason`` and ``resume_command`` are persisted in the run status
        and the single ``supervisor_parked`` audit event; ``context`` may retain the
        failed projection for diagnosis. Repeated calls are idempotent: one park episode
        has one event. The run state and event commit together with the event first, so a
        retry after an interrupted write repairs the document without duplicating the
        event. The park transition is serialized with fresh dispatch commits, and its
        state is revalidated under the run lock so it cannot overwrite a concurrent pause
        or finalization. Raises ``ContractError`` for missing handoff details, a paused or
        terminal run, or any outstanding dispatch lease.
        """
        with self.store.with_dispatch_lock(run_id):
            return self._park_supervisor_locked(
                run_id,
                reason=reason,
                resume_command=resume_command,
                context=context,
            )

    def _orphaned_supervisor_event(self, run_id: str, expected_type: str) -> dict | None:
        """The park/resume event this same transition already wrote, if its run-document
        commit was interrupted — otherwise ``None``.

        Both transitions commit events-first, so a crash between the event append and the
        document write leaves an event on disk that nothing reflects; the retry must reuse
        it rather than append a second one. Identity is the ORDER of the supervisor
        lifecycle events, NOT ``Run.updated_at``: an unrelated run mutation between the
        failed write and the retry bumps ``updated_at``, so a timestamp-derived key stops
        matching its own episode and the "idempotent lifecycle event" promise breaks.

        The trailing lifecycle event being the transition we are about to make is enough to
        identify it, because both callers have already established that the document is not
        in that state (``park`` returns early when already ``PARKED``; ``resume`` raises
        unless still ``PARKED``). A settled park/resume pair therefore never looks orphaned.
        """
        last = next(
            (
                event
                for event in reversed(self.store.read_events(run_id))
                if event.get("type") in _SUPERVISOR_LIFECYCLE_EVENTS
            ),
            None,
        )
        if last is not None and last.get("type") == expected_type:
            return last
        return None

    def _park_supervisor_locked(
        self,
        run_id: str,
        *,
        reason: str,
        resume_command: str,
        context: dict | None = None,
    ) -> Run:
        """Implement :meth:`park_supervisor` while the dispatch lock is held."""
        run = self.store.load_run(run_id)
        if run.state is RunState.PARKED:
            return run
        if not reason.strip() or not resume_command.strip():
            raise ContractError("supervisor park requires a reason and resume command")

        def _ensure_parkable(r: Run) -> None:
            if r.state is RunState.PAUSED:
                raise ContractError(
                    f"cannot supervisor-park paused run {run_id}; resolve its pause first"
                )
            if r.state in TERMINAL_RUN_STATES:
                raise ContractError(
                    f"cannot park terminal run {run_id} (state {r.state.value})"
                )

        _ensure_parkable(run)
        in_flight = self.in_flight(run_id)
        if in_flight:
            raise ContractError(
                "supervisor park requires a stage boundary with no dispatch leases; "
                f"still in flight: {in_flight}"
            )

        park_event: dict | None = None
        append_park_event = True

        def _park(r: Run) -> None:
            nonlocal append_park_event, park_event
            # A record/finalize, pause, or retire transition does not take the dispatch
            # lock. Revalidate the freshly locked run document so none of those can be
            # overwritten by the older snapshot validated above.
            _ensure_parkable(r)
            prior = self._orphaned_supervisor_event(run_id, "supervisor_parked")
            if prior is None:
                parked_at = _now()
                park_event = {
                    "ts": parked_at,
                    "type": "supervisor_parked",
                    "run_id": run_id,
                    "reason": reason,
                    "resume_command": resume_command,
                    "context": context,
                }
            else:
                # The event-first transaction was interrupted before its run-doc commit.
                # Reuse that episode's evidence and finish the document without appending
                # a duplicate event.
                append_park_event = False
                park_event = prior
            assert park_event is not None
            r.state = RunState.PARKED
            r.supervisor_parked_at = park_event["ts"]
            r.supervisor_park_reason = park_event["reason"]
            r.supervisor_resume_command = park_event["resume_command"]
            r.supervisor_context = park_event.get("context")

        return self.store.commit_run_events(
            run_id,
            _park,
            lambda _run: [park_event] if append_park_event and park_event is not None else [],
        )

    def resume_supervisor(
        self, run_id: str, *, supervisor_session_id: str | None = None
    ) -> Run:
        """Release a supervisor-context park after a fresh interactive handoff.

        The method clears the parked metadata, returns the run to ``RUNNING``, and emits
        ``supervisor_resumed``. The event is committed before the run document and a
        retry after an interrupted write reuses that event rather than emitting another.
        If the park recorded a session id, callers must provide a different
        ``supervisor_session_id``; this prevents the exhausted supervisor from immediately
        refilling the run. Raises ``ContractError`` unless the run is parked and that
        freshness check succeeds.
        """
        resume_event: dict | None = None
        append_resume_event = True

        def _resume(r: Run) -> None:
            nonlocal append_resume_event, resume_event
            if r.state is not RunState.PARKED:
                raise ContractError(f"run {run_id} is not parked (state {r.state.value})")
            previous_session = (
                r.supervisor_context.get("session_id")
                if isinstance(r.supervisor_context, dict)
                else None
            )
            if previous_session and not supervisor_session_id:
                raise ContractError(
                    "resuming this parked run requires a fresh supervisor context snapshot"
                )
            if previous_session and supervisor_session_id == previous_session:
                raise ContractError(
                    f"run {run_id} was parked by supervisor session {previous_session}; "
                    "resume it from a fresh Claude Code session"
                )
            prior = self._orphaned_supervisor_event(run_id, "supervisor_resumed")
            if prior is None:
                resume_event = {
                    "ts": _now(),
                    "type": "supervisor_resumed",
                    "run_id": run_id,
                    "previous_session_id": previous_session,
                    "session_id": supervisor_session_id,
                }
            else:
                append_resume_event = False
                resume_event = prior
            r.state = RunState.RUNNING
            r.supervisor_parked_at = None
            r.supervisor_park_reason = None
            r.supervisor_resume_command = None
            r.supervisor_context = None

        return self.store.commit_run_events(
            run_id,
            _resume,
            lambda _run: (
                [resume_event]
                if append_resume_event and resume_event is not None
                else []
            ),
        )

    # --- per-run ledger attribution (#281) -------------------------------------
    def run_rows(self, run_id: str) -> list[dict]:
        """Ledger rows attributed to ``run_id`` only. Every row carries ``run_id``
        (``cost_ledger._row``), so per-run reporting and budget maths filter BEFORE
        summing — defense in depth (#281): a ledger that (wrongly, or from a legacy
        shared-store layout) holds another run's rows can no longer inflate this run's
        cost summary or lane audit, nor corrupt its ``--budget-usd`` hard-PAUSE decision."""
        return [row for row in self.ledger.rows() if row.get("run_id") == run_id]

    # --- per-run cost budget (#34) ---------------------------------------------
    def _remaining_budget_fraction(self, run: Run) -> float:
        """Fraction of the run's budget still unspent (metered), clamped to [0, 1].
        1.0 when no budget is set (cost routing then always picks the top band)."""
        if not run.budget_usd:
            return 1.0
        spent = self.ledger.metered_spend(rows=self.run_rows(run.run_id))
        return max(0.0, min(1.0, (run.budget_usd - spent) / run.budget_usd))

    # --- capacity-aware model downgrade (#12) ----------------------------------
    def _observed_downshift_rate(
        self, stage: Stage, effort: Effort | None
    ) -> tuple[float | None, int | None]:
        """Empirical instability of a (stage, effort) group from run history (#155).

        Aggregates ``CostLedger.by_effort()`` across models for the resolved stage+effort
        and returns ``(observed_rate, sample_size)`` — ``observed_rate`` is the worse of the
        retry and failure rates (either signals a downshift that gets re-run, i.e. false
        economy), ``sample_size`` the pooled invocation count. Returns ``(None, None)`` when
        the group has no history so the caller falls back to the flat threshold. Effort is
        matched by the SAME label ``by_effort`` buckets under: an effort-less dispatch keys
        to ``(default)``, otherwise the Effort's string value (StrEnum)."""
        effort_label = "(default)" if effort is None else str(effort)
        invocations = retries = failures = 0
        for g in self.ledger.by_effort():
            if g.get("stage") == stage.value and g.get("effort") == effort_label:
                invocations += g.get("invocations", 0)
                retries += g.get("retries", 0)
                failures += g.get("failures", 0)
        if invocations <= 0:
            return None, None
        return max(retries, failures) / invocations, invocations

    def _capacity_downgrade(self, model: str) -> str:
        """The cheaper model ``capacity.downgrade_steps`` down the provider's fallback
        chain, floored at the chain's cheapest tier (never off the end). Reuses the SAME
        ``fallback_after`` chain the rate-limit fallback walks, so a downgrade and a
        subsequent rate-limit degrade compose along one ordering instead of two."""
        for _ in range(max(0, self.capacity.downgrade_steps)):
            nxt = self.models.fallback_after(model)
            if nxt is None:
                break  # already at the chain floor — nothing cheaper to drop to
            model = nxt
        return model

    def _estimate_from_labels(self, labels: list[str] | None) -> str | None:
        """Pull a size hint off a task's labels for cost routing: a ``size:``/``estimate:``
        prefixed label, or a bare size word (small/medium/large). None when absent."""
        for raw in labels or []:
            low = str(raw).strip().lower()
            for prefix in ("estimate:", "size:"):
                if low.startswith(prefix):
                    return low[len(prefix):].strip() or None
            if low in {"small", "medium", "large"}:
                return low
        return None

    def _budget_hard_stop(self, run_id: str) -> bool:
        """Budget gate consulted at each dispatch (#34). Emits the soft ``budget_warning``
        ONCE at ``budget_soft_fraction`` of metered spend, and on hard exhaustion PAUSES
        the run (distinct reason ``budget_exhausted``) and returns True so no new work is
        dispatched. No budget set -> always False. Idempotent: it won't re-pause an
        already-PAUSED run, so repeated calls across a tick emit at most one pause."""
        run = self.store.load_run(run_id)
        budget = run.budget_usd
        if budget is None:
            return False
        spent = self.ledger.metered_spend(rows=self.run_rows(run_id))
        if spent >= self.budget_soft_fraction * budget and not run.budget_warning_sent:
            self.store.update_run(
                run_id, lambda r: setattr(r, "budget_warning_sent", True)
            )
            self.emit_notification(
                run_id, "budget_warning",
                {"run_id": run_id, "kind": "budget_warning",
                 "spent_usd": round(spent, 4), "budget_usd": budget,
                 "fraction": round(spent / budget, 4),
                 "summary": f"run {run_id} budget at {spent / budget * 100:.0f}% — "
                            f"metered spend ${spent:.4f} of ${budget:.4f} "
                            f"(soft threshold {self.budget_soft_fraction * 100:.0f}%)"},
            )
        if spent >= budget:
            if run.state is not RunState.PAUSED:
                reason = (
                    f"budget_exhausted: metered spend ${spent:.4f} >= budget ${budget:.4f}"
                )
                self.pause_run(run_id, reason)
                self.emit_notification(
                    run_id, "run_paused",
                    {"run_id": run_id, "kind": "run_paused", "reason": reason,
                     "summary": f"run {run_id} PAUSED — budget exhausted "
                                f"(${spent:.4f} of ${budget:.4f} metered); "
                                f"`orchestrator unpause` (optionally --raise-budget) to resume"},
                )
            return True
        return False

    def _budget_status(self, run: Run, rows: list[dict] | None = None) -> dict | None:
        """The budget block for ``status`` output (None when no budget is set): metered
        spend vs. budget, the remaining fraction, and the routing/warning flags."""
        if run.budget_usd is None:
            return None
        budget = run.budget_usd
        spent = self.ledger.metered_spend(
            rows=self.run_rows(run.run_id) if rows is None else rows
        )
        return {
            "budget_usd": budget,
            "spent_usd": round(spent, 4),
            "remaining_usd": round(budget - spent, 4),
            "fraction": round(spent / budget, 4) if budget else None,
            "soft_threshold": self.budget_soft_fraction,
            "route_by_cost": run.route_by_cost,
            "warning_sent": run.budget_warning_sent,
            "exhausted": spent >= budget,
        }

    def reject(self, run_id: str, task_id: str, *, rejected_by: str, reason: str) -> Task:
        """Confirm-and-close a held task the human agrees is infeasible.

        The symmetric counterpart to ``approve``: instead of overriding the gate and
        proceeding, it transitions the task to the terminal ``CLOSED_INFEASIBLE`` state
        (#53 — distinct from FAILED: a deliberate human close, not an execution failure)
        so the run can actually close. The durable ``rejection-<run>-<task>.json`` artifact
        IS the gate record (who/when/why).

        As an out-of-band transition (like ``approve``/``hold_for_approval``) this method
        performs the full set of post-transition run-level effects via the shared
        ``_finalize_task_terminal`` helper (#110): surface the rejection reason in the
        durable artifacts (evidence-out, #52/#109 — see below), cascade-block any
        dependents of the now-closed task, release its port block (best-effort, #5),
        harvest any learnings it accrued into the cross-run KB (best-effort, #72), and
        call ``_maybe_finalize_run`` so the run reaches a terminal state instead of
        staying open. No ``task_failed`` notification is emitted — a deliberate human
        close is not an execution failure.

        Evidence-out (best-effort, like ``_on_task_completed``): the human-readable task
        index gains a rejection line, and — when the task has an issue — a rejection note
        is published to the task source. Both surface the reason READ BACK from the
        persisted artifact via ``load_rejection`` (proving the record round-trips, #52),
        not just the in-hand argument.

        Raises ``ContractError`` if the task is not currently ``BLOCKED_ON_HUMAN``."""

        def _reject(t: Task) -> None:
            if t.state is not TaskState.BLOCKED_ON_HUMAN:
                raise ContractError(
                    f"task {task_id} is not held for approval (state {t.state.value})"
                )
            t.state = TaskState.CLOSED_INFEASIBLE

        # #199: commit the terminal transition + its `rejected` event atomically (event
        # first, task doc last) so a durably CLOSED_INFEASIBLE task always has its event.
        # The mutator's state guard runs inside the commit, so a non-held task raises
        # BEFORE the rejection artifact is written. write_rejection stays AFTER the commit
        # and BEFORE _finalize_task_terminal, which reads the artifact back (#52).
        task = self.store.commit_task_events(
            run_id, task_id, _reject,
            [{"ts": _now(), "type": "rejected", "run_id": run_id, "task_id": task_id,
              "rejected_by": rejected_by, "reason": reason}],
        )
        self.store.write_rejection(
            run_id, task_id,
            {"rejected_by": rejected_by, "at": _now(), "reason": reason, "run_id": run_id,
             "task_id": task_id},
        )
        self._set_ref_state(run_id, task_id, TaskState.CLOSED_INFEASIBLE)
        # Out-of-band transition (like approve/hold, not via record()), so it must perform
        # record()'s post-transition run-level effects itself. All of them — the rejection
        # evidence-out (#52), cascade, port release (#5), learnings harvest (#72), and
        # finalize — live in the shared helper so every operator finalize path (reject /
        # abandon / future) stays consistent (#110). disposition='rejected' ⇒ the helper
        # surfaces the rejection and emits NO task_failed notification (a deliberate human
        # close is not an execution failure — reject's existing silence is preserved).
        self._finalize_task_terminal(run_id, task, disposition="rejected", reason=reason)
        return task

    def _surface_rejection(self, run_id: str, task: Task) -> None:
        """Surface a rejection's reason in the durable human-readable artifacts, reading
        it BACK from the persisted ``rejection-*.json`` (via ``load_rejection``) so the
        write→read round-trip is exercised in production, not only in tests (#52)."""
        record = self.store.load_rejection(run_id, task.task_id) or {}
        reason = record.get("reason") or ""
        rejected_by = record.get("rejected_by")
        # Per-task stage index gets a rejection line under the task's (now closed) state.
        self.store.write_task_index(
            task.task_id, render_task_index(task, rejection_reason=reason)
        )
        # Publish a rejection note to the task source when there is an issue to comment on
        # (mirrors _publish_completion_note; a no-op when the adapter lacks publish_note).
        publish_note = getattr(self.project.task_source, "publish_note", None)
        if not callable(publish_note) or task.issue_number is None:
            return
        body = render_rejection_note(task, reason, rejected_by=rejected_by)
        try:
            publish_note(task.task_id, body, pr_url=task.pr_url)
        except Exception as exc:  # noqa: BLE001 - finalize must survive a flaky task source
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "rejection_note_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
        else:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "rejection_note_published", "run_id": run_id,
                 "task_id": task.task_id},
            )

    def _finalize_task_terminal(
        self, run_id: str, task: Task, *, disposition: Literal["rejected", "failed"], reason: str
    ) -> None:
        """Run the shared post-transition run-level effects every finalize path must
        perform after transitioning a task to a terminal state: the operator-invoked
        paths (``reject``, ``abandon``, and future ones like ``force_complete``) AND
        ``record``'s terminal-failure path, routed through here in #133/#182 so every
        terminal transition shares one choke point.

        These effects were previously re-implemented inline in each caller, so each new
        operator path was an opportunity to miss one (#110 — ``abandon`` originally missed
        the ``emit_notification`` and ``_surface_rejection`` that ``reject``/``record``
        carry). Centralising them makes the paths consistent and future ones correct by
        construction. In the SAME order the peer methods use:

          1. ``disposition == 'rejected'`` — surface the rejection reason (read back from
             the durable artifact) in the task index + task-source note, wrapped in the
             best-effort try/except that events ``rejection_evidence_failed`` so a flaky
             task source never escapes and skips the cascade/finalize below (#109);
          2. cascade-block dependents of the now-terminal task;
          3. release its port block (#5, best-effort);
          4. harvest any learnings it accrued into the cross-run KB (#72, best-effort);
          5. ``disposition == 'failed'`` — emit the ``task_failed`` alerting notification
             that ``record``'s terminal-failure path emits (#107). A ``rejected``
             disposition emits NO ``task_failed`` (a deliberate human close is not an
             execution failure — this preserves ``reject``'s existing silence);
          6. finalize the run so it reaches a terminal state instead of hanging open.
        """
        if disposition == "rejected":
            # Evidence-out — surface the rejection reason READ BACK from the durable
            # artifact (#52). Best-effort and wrapped: a flaky task source or render must
            # never escape and skip the cascade/finalize below.
            try:
                self._surface_rejection(run_id, task)
            except Exception as exc:  # noqa: BLE001 - evidence-out must never crash the close
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "rejection_evidence_failed", "run_id": run_id,
                     "task_id": task.task_id, "error": str(exc)},
                )
        self._cascade_from(run_id, task.task_id)
        self._release_ports(run_id, task)
        self._harvest_task_learnings(run_id, task)
        self._harvest_process_retrospective(run_id, task)
        if disposition == "failed":
            # Same alerting record()'s terminal-failure path fires (#55/#107): a task that
            # died is exactly the unattended-run event the old monitor alerted on. Always
            # appends the notification audit row even when no notify hook is installed.
            # #184: the alert reason is the caller's authoritative ``reason`` — NOT
            # ``task.last_error``, which is not authoritative for THIS terminal transition
            # (it holds the most recent FAILED/review-rejection error; #213 now clears it at
            # each attempt start, but the caller's reason is still the correct source here).
            # Each caller passes the reason for THIS transition (record: effective.error or
            # outcome; abandon: the abandon reason; reject: the human's reason).
            # #359: enriched with the SAME shared facts the success half carries, so a sink
            # can render an actionable failure alert (which stages ran, what they cost, where
            # the trail is) instead of a bare one-liner. The four original keys are spelled
            # AFTER the spread so they win: summary/stage/reason stay byte-compatible for
            # every existing consumer.
            stage = task.current_stage
            self.emit_notification(
                run_id, NOTIFY_TASK_FAILED,
                {**self._notification_facts(run_id, task),
                 "run_id": run_id, "task_id": task.task_id, "kind": NOTIFY_TASK_FAILED,
                 "summary": f"task {task.task_id} FAILED"
                            + (f" at {stage.value}" if stage else "")
                            + f": {reason}",
                 "stage": stage.value if stage else None,
                 "reason": reason},
            )
        self._maybe_finalize_run(run_id)

    def abandon(
        self,
        run_id: str,
        task_id: str,
        *,
        reason: str,
        disposition: Literal["failed", "rejected"] = "failed",
        min_idle_s: int = DEFAULT_ABANDON_MIN_IDLE_S,
        force: bool = False,
    ) -> Task:
        """Sanctioned finalize for a task whose run was killed mid-dispatch (#82).

        When a run dies mid-dispatch (supervisor killed, machine rebooted, human walked
        away) the task is left holding an outstanding lease (``pending_work_item_id``) that
        every state-changing path correctly refuses to step over: ``record`` demands the
        dispatch's matching result, ``hold`` refuses while a dispatch is outstanding, and
        ``reject`` demands an already-held task. The only prior escape was hand-crafting a
        synthetic lease-matching ``StageResult`` and feeding it to ``record`` — this is that
        escape, sanctioned and guarded.

        It synthesizes the abandonment INTERNALLY (a $0 cost-ledger row explicitly flagged
        unmetered rather than a confident measurement — #319, since the provider process may
        have burned real spend before being orphaned — raw output naming the reason, outcome
        ``dispatch_abandoned``) WITHOUT routing through
        ``record``'s retry/fallback machinery, clears the lease, bumps the stage counter, and
        transitions the task DIRECTLY to a terminal state — FAILED (``disposition='failed'``)
        or CLOSED_INFEASIBLE (``disposition='rejected'``, writing the same durable rejection
        artifact ``reject`` does). The task-doc mutation is applied under the per-task lock
        via ``commit_task_events`` (#108/#199 — the ``dispatch_abandoned`` event commits
        atomically with the task doc). It then runs the SAME post-transition run-level effects
        the other operator finalize paths do, via the shared ``_finalize_task_terminal``
        helper (#110): on ``rejected`` it surfaces the rejection (publishes a note + renders
        the read-back reason, #109); on ``failed`` it emits the ``task_failed`` alert
        ``record`` fires (#107); both cascade-block dependents, release ports, harvest
        learnings, and finalize the run so it reaches terminal instead of hanging open.

        Guards (all ``ContractError``):
          - nothing to abandon: the task has no outstanding dispatch;
          - the task is already terminal;
          - the dispatch appears ALIVE — its provider stream grew within ``min_idle_s`` of
            now (the #66 probe is the liveness sensor). ``force=True`` overrides this last
            guard: the operator asserting the dispatch is dead despite a recent stream write.
        """
        if disposition not in ("failed", "rejected"):
            raise ContractError(
                f"unknown abandon disposition {disposition!r} (expected 'failed' or 'rejected')"
            )
        task = self.store.load_task(run_id, task_id)
        if task.state in TERMINAL_TASK_STATES:
            raise ContractError(
                f"task {task_id} is terminal ({task.state.value}); nothing to abandon"
            )
        if task.pending_work_item_id is None:
            raise ContractError(
                f"task {task_id} has no outstanding dispatch to abandon "
                f"(state {task.state.value})"
            )
        stage = task.current_stage
        if stage is None:  # a lease without a current stage is a corrupt doc — refuse
            raise ContractError(
                f"task {task_id} holds a lease {task.pending_work_item_id} but has no "
                f"current stage; cannot abandon"
            )
        work_item_id = task.pending_work_item_id
        content_hash = task.pending_content_hash

        # Liveness guard (#66 probe as the sensor): refuse if the dispatch's provider stream
        # grew recently — it may still be running. ``last_event_at`` is the stream file mtime
        # (epoch seconds). A missing stream (interactive/ENGINE lane, or nothing teed) leaves
        # ``stream_last_grew`` None, so the guard is vacuous there and the abandon proceeds.
        snap = probe_current_stream(self.store.root, task_id, stage.value, tail_lines=0)
        stream_last_grew = snap.get("last_event_at") if snap else None
        if not force and isinstance(stream_last_grew, (int, float)):
            idle_s = time.time() - stream_last_grew
            if idle_s < min_idle_s:
                raise ContractError(
                    f"dispatch for task {task_id} appears alive: its stream last grew "
                    f"{idle_s:.0f}s ago (< min_idle_s {min_idle_s}s). Wait it out, lower "
                    f"--min-idle-s, or pass --force if you know the process is dead."
                )

        # Synthesize the lease-matching abandonment honestly. No model call COMPLETED, so the
        # lane_used invocation says exactly that and the token usage is empty. #319: empty is
        # not the same as zero here — an abandoned dispatch is the canonical unrecoverable-usage
        # case (the provider process was orphaned or killed before printing a usage report, and
        # it may have burned minutes of Opus first), so the row is flagged usage_recovered=False
        # and lands unpriced/unmetered rather than as a confident, metered $0.00. The figure
        # itself is unchanged — the engine has nothing to bill — but it now reads as UNKNOWN.
        # We reuse the intended lane cell (router) for accurate attribution.
        dispatched = task.stages[stage]
        lane = self.router.lane_for(stage, task)
        completed_at = _now()
        synthetic = StageResult(
            work_item_id=work_item_id,
            content_hash=content_hash or "",
            run_id=run_id,
            task_id=task_id,
            stage=stage,
            attempt=dispatched.attempt,
            model=ModelId(dispatched.model or ENGINE_MODEL),
            # #138: echo the dispatched effort (persisted by begin_stage) so the abandoned
            # cost-ledger row attributes effort symmetrically with model. None on effort-less
            # (ENGINE-lane / pre-#96) dispatches.
            effort=dispatched.effort,
            status=ResultStatus.FAILURE,
            raw_output=f"Dispatch abandoned (no model call completed): {reason}",
            lane_used=LaneUsed(
                execution_mode=lane.execution_mode,
                provider=lane.provider,
                invocation=f"abandoned: dispatch orphaned, no model call ({reason})",
            ),
            token_usage=TokenUsage(),
            usage_recovered=False,
            error=f"dispatch_abandoned: {reason}",
            completed_at=completed_at,
        )
        abandon_row = self.ledger.record(
            synthetic, duration_s=_elapsed_s(dispatched.started_at)
        )
        cost = abandon_row["cost_usd"]
        cost_metered = bool(abandon_row.get("metered", True))

        # Clear the lease and fold the abandonment into the stage record + task state DIRECTLY
        # (not via apply_result / record — an abandoned dispatch never converges, so none of
        # the retry/salvage/fallback machinery applies). #108: apply the mutation under the
        # per-task lock (a read-modify-write on the FRESH doc) so a concurrent writer can't
        # clobber the transition — the side-effecting reads/computation above (probe, synthetic
        # result, ledger row, lane) already ran outside the lock. #199: commit via
        # commit_task_events so the `dispatch_abandoned` event is appended (first) atomically
        # with the task-doc write (last) — no terminal task with a missing transition event.
        error = f"dispatch_abandoned: {reason}"

        def _abandon(t: Task) -> None:
            t.pending_work_item_id = None
            t.pending_content_hash = None
            t.pending_plan = False  # #288: the plan marker never outlives its lease
            t.pending_fallback_model = None
            d = t.stages[stage]
            d.status = StageStatus.FAILED
            d.completed_at = completed_at
            d.error = error
            d.provider = lane.provider
            d.lane = lane.execution_mode
            d.cost_usd = cost
            d.metered = cost_metered
            t.last_error = error
            t.state = (
                TaskState.CLOSED_INFEASIBLE if disposition == "rejected"
                else TaskState.FAILED
            )
            t.stage_counter += 1

        task = self.store.commit_task_events(
            run_id, task_id, _abandon,
            [{"ts": _now(), "type": "dispatch_abandoned", "run_id": run_id, "task_id": task_id,
              "stage": stage.value, "attempt": dispatched.attempt, "work_item_id": work_item_id,
              "reason": reason, "disposition": disposition,
              "stream_last_grew": stream_last_grew, "forced": force}],
        )
        seq = task.stage_counter
        payload = {
            "work_item_id": work_item_id,
            "stage": stage.value,
            "task_id": task_id,
            "attempt": dispatched.attempt,
            "status": synthetic.status.value,
            "outcome": "dispatch_abandoned",
            "model": synthetic.model,
            "effort": synthetic.effort,  # #151: surface effort alongside model, as the cost ledger does
            "lane_used": synthetic.lane_used.model_dump(),
            "cost_usd": cost,
            "raw_output": synthetic.raw_output,
            "error": synthetic.error,
            "completed_at": completed_at,
        }
        self.store.write_stage_log(task_id, seq, stage.value, payload)
        self.store.write_stage_markdown(task_id, seq, stage.value, render_stage(payload))
        # A rejected abandon writes the SAME durable rejection artifact reject() does (the
        # gate record), so status()/retrospective read the reason back the identical way.
        rejection_reason = None
        if disposition == "rejected":
            rejection_reason = reason
            self.store.write_rejection(
                run_id, task_id,
                {"rejected_by": "abandon", "at": completed_at, "reason": reason,
                 "run_id": run_id, "task_id": task_id},
            )
        # The task doc is already persisted by commit_task_events above; the index/ref-state
        # below are derived artifacts. A FAILED abandon renders the task index HERE. A REJECTED
        # abandon SKIPS this write (#132): _finalize_task_terminal → _surface_rejection
        # immediately re-renders the index from the READ-BACK rejection artifact, so an
        # inline write would be redundant (overwritten with identical content).
        if disposition == "failed":
            self.store.write_task_index(
                task_id, render_task_index(task, rejection_reason=rejection_reason)
            )
        self._set_ref_state(run_id, task_id, task.state)
        # (#199: the `dispatch_abandoned` event is emitted with the task-doc commit above.)
        # Shared out-of-band post-transition run-level effects (#110): the rejection
        # evidence-out (#109 — publishes a note + re-renders the index from the read-back
        # artifact), cascade-block, port release, learnings harvest, the task_failed alert
        # on the FAILED path (#107), and run finalize. Delegated so this path can never drift
        # from reject()'s (each new operator finalize path stays correct by construction).
        self._finalize_task_terminal(run_id, task, disposition=disposition, reason=reason)
        return task

    # --- retire a superseded run (#257) ----------------------------------------
    def retire(
        self,
        run_id: str,
        *,
        reason: str,
        retired_by: str,
        superseded_by: str | None = None,
        force: bool = False,
    ) -> Run:
        """Sanctioned finalize for a whole run the human deliberately SUPERSEDED (#257).

        The gap this fills: every other finalize path is built for a different failure
        mode. ``abandon`` requires an outstanding lease (a cleanly-recorded run has none)
        and, on ``--disposition rejected``, publishes a "closed infeasible" note to the
        task source; ``reject`` requires an already-held task and publishes the same note.
        Both are wrong for a superseded run, whose issues are typically LIVE in the
        successor run. ``hold`` only silences the stale alarms — the run stays ``running``
        forever, and "blocked on human" is a lie when no human action is pending. So a
        superseded run could never reach terminal, permanently occupying the monitor's
        "needs you" list, which is exactly the signal that must stay unignorable.

        Run-level, not per-task: transitions EVERY non-terminal task (including
        ``BLOCKED_ON_HUMAN``, which no other path can drive terminal without a rejection
        artifact) to the terminal ``SUPERSEDED`` state, then finalizes the run to
        ``RunState.SUPERSEDED``. Already-terminal tasks are left exactly as they are — a
        task that genuinely COMPLETED or FAILED before the run was retired keeps its own
        honest outcome.

        No lease requirement (the common case is a cleanly-recorded run the human walked
        away from). When a lease IS outstanding, ``force=True`` is required: the operator
        asserting the dispatch is dead. The forced path then follows ``abandon``'s honesty
        (#319) — it clears the lease, marks the in-flight stage FAILED, and records an
        UNMETERED $0 cost-ledger row, because an orphaned provider process may have burned
        real spend before being retired, so the figure must read as UNKNOWN rather than as
        a confident zero. Unlike ``abandon`` there is no stream-liveness probe: retiring is
        a whole-run decision the human has already made out of band, and `force` is the
        explicit assertion the probe would otherwise be second-guessing.

        Per-task effects — enumerated rather than described as "the same as reject", since
        a peer's call chain is invisible in a plan and gets under-copied (the #110 lesson,
        where ``abandon`` shipped missing ``emit_notification`` and ``_surface_rejection``).
        This path deliberately runs a SUBSET of ``_finalize_task_terminal``'s chain:

          RUN — ``_release_ports`` (#5, best-effort: a retired task must not leak its port
                block) and ``_harvest_task_learnings`` (#72: a superseded task may still
                have learned something worth keeping), plus ``write_task_index`` and
                ``_set_ref_state`` so the derived artifacts match the doc.
          SKIP — ``_cascade_from``: a dependent of a superseded task is itself being
                superseded by this very call, and CASCADE_BLOCKED is an execution-failure
                state that would poison the run rollup and the batch circuit breaker.
          SKIP — ``write_rejection`` / ``_surface_rejection`` / the task source's
                ``publish_note``: NO task-source mutation is the defining property of this
                path. The issue is live in the successor run; "closed infeasible" on it
                would be actively wrong.
          SKIP — the ``task_failed`` notification: nothing failed, and re-alerting the
                operator is the opposite of what retiring is for.
          SKIP — ``_maybe_finalize_run``: its rollup is derived and failure-dominated,
                so a retired run containing one FAILED task would finalize FAILED. The
                run state here is DECLARED, and is set once at the end of this method.

        Raises ``ContractError`` if the run is already terminal, or if any task holds an
        outstanding lease and ``force`` is not set."""
        run = self.store.load_run(run_id)
        if run.state in TERMINAL_RUN_STATES:
            raise ContractError(
                f"run {run_id} is already terminal ({run.state.value}); nothing to retire"
            )
        # Load the authoritative task docs (TaskRef.state is an explicit derived cache, and
        # the lease lives on the task doc only).
        tasks = [self.store.load_task(run_id, ref.task_id) for ref in run.task_refs]
        non_terminal = [t for t in tasks if t.state not in TERMINAL_TASK_STATES]
        leased = [t.task_id for t in non_terminal if t.pending_work_item_id is not None]
        if leased and not force:
            raise ContractError(
                f"run {run_id} has {len(leased)} task(s) holding an outstanding dispatch "
                f"({', '.join(sorted(leased))}); a dispatch may still be running. Record "
                "the result, or pass --force if you know the process is dead."
            )
        for task in non_terminal:
            self._supersede_task(
                run_id, task, reason=reason, superseded_by=superseded_by, forced=force
            )
        retired_at = _now()

        def _retire(r: Run) -> None:
            r.state = RunState.SUPERSEDED
            r.superseded_at = retired_at
            r.superseded_by = superseded_by
            r.superseded_reason = reason
            r.retired_by = retired_by

        self.store.update_run(run_id, _retire)
        # The audit trail the operator reads later: why this run stops, who stopped it, and
        # where the work went — so a retired run is an explained stop, not a mystery
        # half-run. Emitted AFTER the run doc is durable, so the event never describes a
        # state that was not written.
        self.store.append_event(
            run_id,
            {"ts": retired_at, "type": "run_superseded", "run_id": run_id,
             "reason": reason, "retired_by": retired_by, "superseded_by": superseded_by,
             "retired_tasks": [t.task_id for t in non_terminal],
             "forced": force, "leased_tasks": sorted(leased)},
        )
        run = self.store.load_run(run_id)
        progress = run.progress()
        # The same "this run is done, stop watching it" ping every other finalize path
        # fires (#55), under its own kind so an operator can tell a retired run from a
        # completed one without parsing the summary.
        self.emit_notification(
            run_id, "run_superseded",
            {"run_id": run_id, "kind": "run_superseded", "state": run.state.value,
             "summary": f"run {run_id} retired as superseded — {len(non_terminal)} task(s) "
                        f"superseded of {progress.total}"
                        + (f", superseded by {superseded_by}" if superseded_by else "")
                        + f": {reason}",
             "reason": reason, "superseded_by": superseded_by, "retired_by": retired_by},
        )
        # A terminal run gets its final cost artifacts on every path (#281: this run's rows
        # only), then the lock sweep — no task can move again, so no writer remains. The
        # run LOG DIR itself is retained: retiring is about state, never cleanup.
        self._write_final_cost_artifacts(run_id, run)
        self.store.sweep_locks()
        return run

    def _supersede_task(
        self, run_id: str, task: Task, *, reason: str, superseded_by: str | None, forced: bool
    ) -> Task:
        """Drive ONE non-terminal task to the terminal ``SUPERSEDED`` state for ``retire``.

        Clearing the lease is unconditional (it is already ``None`` on the common,
        cleanly-recorded task) so a retired task can never be left holding one — including
        ``pending_plan``, whose marker must never outlive its lease (#288)."""
        task_id = task.task_id
        stage = task.current_stage
        work_item_id = task.pending_work_item_id
        error = f"superseded: {reason}"
        completed_at = _now()
        # The stage to charge and fold, or None when there is nothing in flight. A lease
        # WITHOUT a current stage is a corrupt doc: ``abandon`` refuses there because it
        # exists to finalize that one stage, but retire clears the lease and supersedes
        # anyway — refusing would leave the run unretireable, the exact trap this path
        # exists to open. There is simply no stage record to charge.
        in_flight = stage if (work_item_id is not None and stage is not None) else None
        cost: float | None = None
        cost_metered = True
        attempt: int | None = None
        lane = None
        if in_flight is not None:
            dispatched = task.stages[in_flight]
            attempt = dispatched.attempt
            lane = self.router.lane_for(in_flight, task)
            # Same honesty as abandon (#319): no model call COMPLETED, and the orphaned
            # process may have burned real spend, so the row lands UNMETERED
            # (usage_recovered=False) rather than as a confident, metered $0.00.
            synthetic = StageResult(
                work_item_id=work_item_id or "",
                content_hash=task.pending_content_hash or "",
                run_id=run_id,
                task_id=task_id,
                stage=in_flight,
                attempt=dispatched.attempt,
                model=ModelId(dispatched.model or ENGINE_MODEL),
                effort=dispatched.effort,
                status=ResultStatus.FAILURE,
                raw_output=f"Run retired as superseded (no model call completed): {reason}",
                lane_used=LaneUsed(
                    execution_mode=lane.execution_mode,
                    provider=lane.provider,
                    invocation=f"superseded: run retired mid-dispatch ({reason})",
                ),
                token_usage=TokenUsage(),
                usage_recovered=False,
                error=error,
                completed_at=completed_at,
            )
            row = self.ledger.record(synthetic, duration_s=_elapsed_s(dispatched.started_at))
            cost = row["cost_usd"]
            cost_metered = bool(row.get("metered", True))

        def _supersede(t: Task) -> None:
            t.pending_work_item_id = None
            t.pending_content_hash = None
            t.pending_plan = False  # #288: the plan marker never outlives its lease
            t.pending_fallback_model = None
            if in_flight is not None and lane is not None:
                d = t.stages[in_flight]
                d.status = StageStatus.FAILED
                d.completed_at = completed_at
                d.error = error
                d.provider = lane.provider
                d.lane = lane.execution_mode
                d.cost_usd = cost
                d.metered = cost_metered
                t.last_error = error
                t.stage_counter += 1
            t.state = TaskState.SUPERSEDED

        task = self.store.commit_task_events(
            run_id, task_id, _supersede,
            [{"ts": completed_at, "type": "task_superseded", "run_id": run_id,
              "task_id": task_id, "reason": reason, "superseded_by": superseded_by,
              "stage": stage.value if stage else None, "attempt": attempt,
              "work_item_id": work_item_id, "forced": forced}],
        )
        if in_flight is not None:
            seq = task.stage_counter
            payload = {
                "work_item_id": work_item_id,
                "stage": in_flight.value,
                "task_id": task_id,
                "attempt": attempt,
                "status": ResultStatus.FAILURE.value,
                "outcome": "superseded",
                "model": task.stages[in_flight].model,
                "effort": task.stages[in_flight].effort,
                "cost_usd": cost,
                "error": error,
                "completed_at": completed_at,
            }
            self.store.write_stage_log(task_id, seq, in_flight.value, payload)
            self.store.write_stage_markdown(task_id, seq, in_flight.value, render_stage(payload))
        # Derived artifacts, matching the now-durable doc. No rejection_reason is passed:
        # a superseded task has no rejection artifact and must not render as one.
        self.store.write_task_index(task_id, render_task_index(task))
        self._set_ref_state(run_id, task_id, TaskState.SUPERSEDED)
        self._release_ports(run_id, task)
        self._harvest_task_learnings(run_id, task)
        self._harvest_process_retrospective(run_id, task)
        return task
    # --- task spec snapshot refresh (#271) -------------------------------------
    def refresh_spec(
        self, run_id: str, task_id: str, *, force: bool = False, check_only: bool = False
    ) -> dict:
        """Re-read ``task_id``'s spec from the task source onto its Task doc (#271).

        A task's ``title``/``body`` are snapshotted ONCE, at ``add_task``, and every stage
        prompt for the rest of the run renders from that copy. The snapshot is deliberate —
        it is what makes a run reproducible and stage prompts byte-stable — but until now
        there was no sanctioned way to MOVE it: editing the upstream issue mid-run was
        silently a no-op, so the workarounds were rebuilding the run (discarding its
        history) or hand-patching the status JSON behind the engine's back (no event, no
        lock, no audit). This is that operation, guarded and evented.

        Deliberately NOT automatic: the engine never re-resolves the spec per stage. A
        refresh is an operator decision, recorded as one.

        Returns a JSON-safe report: ``{"task_id", "changed", "applied", "check_only",
        "leased_dispatch", "spec_captured_at", "spec_source_updated_at", "diff"}`` where
        ``diff`` is ``spec_refresh.diff_summary``'s block.

        ``check_only=True`` is a read-only dry run — resolve, diff, report, write nothing
        (no doc mutation, no event), so an operator can see what a refresh WOULD change
        before taking it.

        Guards (all ``ContractError``, modeled on :meth:`abandon`):

        - the task is terminal — no further stage will render a prompt, so a refresh would
          only rewrite the audit trail of what actually ran;
        - the task holds an outstanding dispatch lease (``pending_work_item_id``): that
          stage's prompt was ALREADY rendered from the old copy, so a refresh cannot reach
          it and would leave the in-flight stage's spec and the doc's spec disagreeing —
          exactly the #256 failure (a SCOPE plan contradicting its own task's spec).
          ``force=True`` overrides this one, for the operator who knows the leased stage's
          output will be discarded anyway; the override is stamped ``leased_dispatch`` on
          the event rather than being silent.

        Whether or not anything changed, a SUCCESSFUL (non-dry-run) refresh emits
        ``task_spec_refreshed`` — a no-op refresh is a receipt that the snapshot was
        verified against the source, and "verified identical" must not read like "never
        looked" (the #322 convention).
        """
        task = self.store.load_task(run_id, task_id)
        if task.state in TERMINAL_TASK_STATES:
            raise ContractError(
                f"task {task_id} is terminal ({task.state.value}); refusing to refresh its "
                "spec — no further stage will render a prompt from it"
            )
        leased = task.pending_work_item_id is not None
        if leased and not force and not check_only:
            raise ContractError(
                f"task {task_id} holds an outstanding dispatch ({task.pending_work_item_id} "
                f"on stage {task.current_stage.value if task.current_stage else '?'}) whose "
                "prompt was already rendered from the current snapshot — a refresh cannot "
                "reach it. Wait for the stage to land, or pass --force to refresh anyway "
                "(the in-flight stage keeps the old spec)."
            )
        # Resolve OUTSIDE the task lock: this is the network/subprocess call, and the
        # comparison below is pure. A source failure (unreachable, closed-issue refusal)
        # propagates to the caller unchanged — a refresh that could not read the source
        # must fail loudly, never quietly leave the snapshot in place.
        spec = self.project.task_source.resolve(task_id)
        diff = spec_diff_summary(task.title, task.body, spec.title, spec.body)
        source_updated_at = getattr(spec, "updated_at", None)
        report = {
            "task_id": task_id,
            "changed": diff["changed"],
            "applied": False,
            "check_only": check_only,
            "leased_dispatch": leased,
            "spec_captured_at": task.spec_captured_at,
            "spec_source_updated_at": source_updated_at,
            "diff": diff,
        }
        if check_only:
            return report

        refreshed_at = _now()
        new_title, new_body = spec.title, spec.body
        new_fingerprint = diff["new_fingerprint"]
        # The diff/guards above ran on a PRE-LOCK read, and the source round-trip between
        # them and the write is long enough for a scheduler tick to dispatch this very task.
        # Both are therefore re-decided inside the mutator against the doc actually being
        # overwritten, and ``applied_diff`` (not the pre-lock one) is what the event reports.
        applied_diff = diff

        def _refresh(t: Task) -> None:
            nonlocal applied_diff
            # Re-validate under the lock (#311's shape): raising here aborts the transaction
            # before any event append or doc write, so a refused refresh leaves no trace but
            # the caller's error — same outcome as the pre-check, just race-free.
            if t.state in TERMINAL_TASK_STATES:
                raise ContractError(
                    f"task {task_id} became terminal ({t.state.value}) while its spec was "
                    "being re-read; refusing to refresh"
                )
            if t.pending_work_item_id is not None and not force:
                raise ContractError(
                    f"task {task_id} was dispatched ({t.pending_work_item_id}) while its "
                    "spec was being re-read — that prompt is already rendered from the "
                    "current snapshot. Re-run once it lands, or pass --force."
                )
            applied_diff = spec_diff_summary(t.title, t.body, new_title, new_body)
            t.title = new_title
            t.body = new_body
            t.spec_fingerprint = new_fingerprint
            t.spec_source_updated_at = source_updated_at
            # ``spec_captured_at`` moves too: it dates the copy the prompts render from,
            # and after this write that copy is THIS read. ``spec_refreshed_at`` is what
            # distinguishes a refreshed snapshot from an original add_task capture.
            t.spec_captured_at = refreshed_at
            t.spec_refreshed_at = refreshed_at

        # #199 pattern: commit the doc mutation and its audit event as one unit under the
        # per-task lock, so a refreshed snapshot can never exist without its event. The
        # event is built from the MUTATED task (callable form) so its lease/stage fields
        # describe the state the refresh actually landed on.
        self.store.commit_task_events(
            run_id, task_id, _refresh,
            lambda t: [{
                "ts": refreshed_at, "type": "task_spec_refreshed", "run_id": run_id,
                "task_id": task_id, "changed": bool(applied_diff["changed"]),
                "leased_dispatch": t.pending_work_item_id is not None,
                "forced": bool(force and t.pending_work_item_id is not None),
                "stage": t.current_stage.value if t.current_stage else None,
                "source_updated_at": source_updated_at,
                "diff": applied_diff,
            }],
        )
        report["applied"] = True
        report["changed"] = applied_diff["changed"]
        report["diff"] = applied_diff
        report["spec_captured_at"] = refreshed_at
        return report

    def spec_staleness(self, run_id: str, task_id: str, *, task: Task | None = None) -> dict:
        """Has ``task_id``'s upstream spec diverged from the snapshot the run renders from?

        The read-only sensor behind ``status --check-spec`` (#271). Resolves the task source
        and compares CONTENT fingerprints — not the source's ``updated_at``, which moves for
        edits that never touch title or body (a label, a comment) and which some sources
        cannot report at all.

        Never raises: a source that is unreachable, rate-limited, or refuses the issue (the
        GitHub source refuses a CLOSED one) returns ``{"spec_check_error": ...}`` so one
        unreachable task cannot take down a whole run's status dump. A task whose doc predates
        #271 has no stored fingerprint; its verdict is computed from the stored title/body
        directly, so it still gets a real answer rather than "unknown".
        """
        t = task if task is not None else self.store.load_task(run_id, task_id)
        out: dict = {
            "spec_captured_at": t.spec_captured_at,
            "spec_refreshed_at": t.spec_refreshed_at,
        }
        try:
            spec = self.project.task_source.resolve(task_id)
        except Exception as exc:  # noqa: BLE001 — a probe must degrade, never break status
            out["spec_stale"] = None
            out["spec_check_error"] = f"{type(exc).__name__}: {exc}"
            out["spec_source_updated_at"] = t.spec_source_updated_at
            return out
        # Prefer the stored fingerprint (it is what the snapshot was taken as), but fall back
        # to hashing the stored title/body for a pre-#271 doc — same answer, one extra hash.
        stored = t.spec_fingerprint or spec_fingerprint(t.title, t.body)
        current = spec_fingerprint(spec.title, spec.body)
        out["spec_stale"] = stored != current
        out["spec_source_updated_at"] = getattr(spec, "updated_at", None)
        if stored != current:
            out["spec_diff"] = spec_diff_summary(t.title, t.body, spec.title, spec.body)
        return out

    # --- driver claim / orphaned-lease reclaim (#313) --------------------------
    def driver_claim(
        self, run_id: str, *, alive: Callable[[int], bool] | None = None, run: Run | None = None
    ) -> dict:
        """Classify the run's recorded driver claim (#313). Read-only.

        Returns ``{"state", "host", "pid", "claimed_at", "reclaimable"}`` where ``state``
        is one of:

        - ``unclaimed`` — no driver ever stamped this run (e.g. it is being driven
          task-by-task through the CLI supervisor, whose background invocations hold real
          leases). NOT reclaimable: a lease with no known owner may still be live.
        - ``mine`` — the claim is this very process. Reclaimable: ``Scheduler.run``
          dispatches SYNCHRONOUSLY, so a lease this process left behind belongs to a tick
          that already returned/raised — nothing is still running against it.
        - ``dead`` — same host, and the pid is gone. Reclaimable: this is the killed-driver
          case the whole mechanism exists for.
        - ``live`` — same host, pid still running. NOT reclaimable — stealing a live
          driver's lease would double-dispatch the same stage.
        - ``foreign_host`` — claimed from another machine, where a local pid check means
          nothing. NOT reclaimable (fails safe).

        ``alive`` injects the pid-liveness sensor (tests pass a stub); it defaults to a
        signal-0 probe that fails safe by reporting "alive" for anything it cannot answer.
        ``run`` lets a caller that already holds the run doc (``status``) skip re-loading it.
        """
        probe = alive or _pid_alive
        driver = (run if run is not None else self.store.load_run(run_id)).driver
        if driver is None:
            return {"state": "unclaimed", "host": None, "pid": None, "claimed_at": None,
                    "reclaimable": False}
        out = {"host": driver.host, "pid": driver.pid, "claimed_at": driver.claimed_at}
        if driver.host != socket.gethostname():
            return {**out, "state": "foreign_host", "reclaimable": False}
        if driver.pid == os.getpid():
            return {**out, "state": "mine", "reclaimable": True}
        if probe(driver.pid):
            return {**out, "state": "live", "reclaimable": False}
        return {**out, "state": "dead", "reclaimable": True}

    def driver_liveness(
        self,
        run_id: str,
        *,
        alive: Callable[[int], bool] | None = None,
        run: Run | None = None,
    ) -> dict:
        """The driver claim (#313) PLUS what its own log says it was doing (#323).

        ``driver_claim`` answers "does that pid still exist"; a pid that exists says
        nothing about a loop that stopped looping, and a claim alone could not distinguish
        a driver sleeping out a capacity stall from a dead one. Merging in
        ``runs/<run>/driver.jsonl`` adds the last heartbeat, its age, the state it named,
        and whether the driver recorded an exit — so ``status`` shows a dead driver as
        visibly different from a working one.

        Reported by ``status`` as its ``driver`` block. The CLAIM's own fields
        (``state``/``reclaimable``) pass through unchanged: lease reclaim safety is decided
        by the pid probe, never by a heartbeat.
        """
        claim = self.driver_claim(run_id, alive=alive, run=run)
        return liveness_from_log(self.store.root, claim, run_id=run_id)

    def claim_run_driver(
        self, run_id: str, *, alive: Callable[[int], bool] | None = None
    ) -> dict:
        """Stamp THIS process as the run's driver (#313), unless a live foreign driver
        already holds it — in which case the existing claim is left untouched and returned.

        Persisted on the Run doc (not engine memory) because its whole purpose is to
        survive the process that wrote it: the NEXT driver reads it back to tell its own
        crashed leases from a live driver's (#206's persistence rule, for the same reason).
        The claim is never cleared on exit — a stale claim is the evidence, not litter.
        Returns the resulting claim classification.
        """
        current = self.driver_claim(run_id, alive=alive)
        if current["state"] == "live":
            return current  # someone else is driving; do not overwrite their ownership
        claimed_at = _now()
        pid, host = os.getpid(), socket.gethostname()

        def _claim(run: Run) -> None:
            run.driver = RunDriver(host=host, pid=pid, claimed_at=claimed_at)

        self.store.update_run(run_id, _claim)
        self.store.append_event(
            run_id,
            {"ts": claimed_at, "type": "driver_claimed", "run_id": run_id, "host": host,
             "pid": pid, "previous": current["state"], "previous_pid": current["pid"]},
        )
        return {"state": "mine", "host": host, "pid": pid, "claimed_at": claimed_at,
                "reclaimable": True}

    def reclaim_orphaned_dispatches(
        self, run_id: str, *, alive: Callable[[int], bool] | None = None
    ) -> dict:
        """Free the dispatch leases a KILLED driver left behind, so the run can continue
        at the SAME attempt (#313) — called at ``Scheduler.run`` startup.

        A driver killed mid-dispatch (Ctrl-C on the foreground ``run-headless``, a reboot)
        leaves each in-flight task RUNNING with ``pending_work_item_id`` set. Every normal
        path correctly refuses to step over that lease, so ``dispatchable()`` excluded the
        task and the next ``Scheduler.run`` had nothing to do and exited looking like a
        clean no-op. This is the third sanctioned lease-clearing path, alongside ``record``
        (the result arrived) and ``abandon`` (give up, terminally) — the one that RESUMES.

        Clearing the lease while leaving the stage record ``RUNNING`` is what makes the
        re-dispatch free: ``next_work`` derives the attempt from the persisted stage status
        (RUNNING -> re-dispatch the SAME attempt, and reset the worktree to the last
        successful checkpoint), so recovery costs no retry budget — unlike the
        hand-crafted ``timeout`` StageResult that was the only prior recovery.

        SAFETY — same-owner only. Nothing is reclaimed unless ``driver_claim`` says the
        recorded claim is ours or provably dead on this host; an unclaimed run, a live
        driver, or a foreign-host claim reclaims NOTHING (the caller reports that loudly
        instead). Stealing a live dispatch's lease would double-dispatch the stage.

        Never silent: each reclaim appends a ``dispatch_reclaimed`` event naming the
        retired ``work_item_id`` — the audit counterpart of ``lease_superseded``, so the
        #175 dispatch/record balance still closes out every ``stage_dispatched``. The
        task-doc mutation and its event commit together under the per-task lock (#199).

        Returns ``{"driver", "reclaimed", "skipped"}``: the claim classification, the
        per-task reclaim records, and any leased task that could not be reclaimed (a lease
        with no current stage is a corrupt doc — left for ``abandon``).
        """
        claim = self.driver_claim(run_id, alive=alive)
        reclaimed: list[dict] = []
        skipped: list[dict] = []
        if not claim["reclaimable"]:
            return {"driver": claim, "reclaimed": reclaimed, "skipped": skipped}
        run = self.store.load_run(run_id)
        for ref in run.task_refs:
            if ref.state in TERMINAL_TASK_STATES:
                continue
            task = self.store.load_task(run_id, ref.task_id)
            if task.pending_work_item_id is None:
                continue
            stage = task.current_stage
            if stage is None or stage not in task.stages:
                skipped.append({"task_id": ref.task_id, "reason": "no_current_stage",
                                "work_item_id": task.pending_work_item_id})
                continue
            work_item_id = task.pending_work_item_id
            attempt = task.stages[stage].attempt

            def _release(t: Task) -> None:
                t.pending_work_item_id = None
                t.pending_content_hash = None
                # #288: the panel-plan marker describes the dispatch that just died, so it
                # must not outlive its lease. `pending_fallback_model` is deliberately NOT
                # touched: it is consumed INTO a dispatch, so it is already None here, and
                # clearing it blindly could discard a fallback queued for the next one.
                t.pending_plan = False
                # The stage record stays RUNNING on purpose — that is what makes
                # next_work re-dispatch the SAME attempt from the last checkpoint.

            self.store.commit_task_events(
                run_id, ref.task_id, _release,
                [{"ts": _now(), "type": "dispatch_reclaimed", "severity": "warning",
                  "run_id": run_id, "task_id": ref.task_id, "stage": stage.value,
                  "attempt": attempt, "work_item_id": work_item_id,
                  "driver_state": claim["state"], "driver_pid": claim["pid"],
                  "reason": "driver died mid-dispatch; lease released for re-dispatch at "
                            "the same attempt"}],
            )
            reclaimed.append({"task_id": ref.task_id, "stage": stage.value,
                              "attempt": attempt, "work_item_id": work_item_id})
        return {"driver": claim, "reclaimed": reclaimed, "skipped": skipped}

    # --- resume / status ------------------------------------------------------
    def resume(self, run_id: str) -> dict:
        run = self.store.load_run(run_id)
        out = {}
        for ref in run.task_refs:
            task = self.store.load_task(run_id, ref.task_id)
            rp = resume_point(task)
            out[ref.task_id] = rp.value if rp else None
        return out

    def retrospective(self, run_id: str) -> dict:
        """Structured retrospective over the run's durable artifacts. Emitted for every
        finalized run (see ``_emit_retrospective``), so this is reached by clean and
        rejection-only runs too — not just failures."""
        run = self.store.load_run(run_id)
        tasks = [self.store.load_task(run_id, ref.task_id) for ref in run.task_refs]
        events = self.store.read_events(run_id)
        stage_logs = {t.task_id: self.store.read_stage_logs(t.task_id) for t in tasks}
        # #67: annotate any deliberately-closed (CLOSED_INFEASIBLE) tasks with the reason
        # read BACK from the durable rejection artifact, so a mixed failure+rejection run's
        # retrospective separates human closes from execution failures instead of ignoring
        # them. A rejection-only run (COMPLETED_WITH_REJECTIONS) now reaches here too, and
        # this is the section that keeps its retrospective from reading "no failures".
        rejections = {}
        for t in tasks:
            if t.state is TaskState.CLOSED_INFEASIBLE:
                record = self.store.load_rejection(run_id, t.task_id) or {}
                rejections[t.task_id] = {"title": t.title, "reason": record.get("reason")}
        return build_retrospective(run, tasks, events, stage_logs, rejections=rejections)

    def status(
        self,
        run_id: str,
        *,
        stale_after_s: int = 1800,
        include_activity: bool = False,
        check_spec: bool = False,
    ) -> dict:
        """Poll a run. ``check_spec`` (#271, opt-in like ``include_activity``) additionally
        resolves each non-terminal task's spec from the task source and flags one whose
        upstream has diverged from the snapshot its prompts render from. Default-off because
        it costs one source round-trip per task — the cheap poll path stays offline and its
        output byte-for-byte unchanged."""
        run = self.store.load_run(run_id)
        progress = run.progress()
        now = datetime.now(UTC)
        tasks = {}
        for ref in run.task_refs:
            task = self.store.load_task(run_id, ref.task_id)
            # Liveness: how long since this task last moved. A non-terminal task that
            # hasn't updated past the threshold is flagged STALE — the caller's cheap
            # stall signal (the old monitor's dead-process/no-progress checks had no
            # counterpart; nothing said a run was dead until a human went digging).
            age_s: float | None = None
            try:
                age_s = max(0.0, (now - datetime.fromisoformat(task.updated_at)).total_seconds())
            except (ValueError, TypeError):
                age_s = None
            task_status: dict = {
                "state": task.state.value,
                "current_stage": task.current_stage.value if task.current_stage else None,
                "stages": {s.value: r.status.value for s, r in task.stages.items()},
                "pr_url": task.pr_url,
                "seconds_since_update": round(age_s, 1) if age_s is not None else None,
                "stale": bool(
                    age_s is not None
                    and age_s > stale_after_s
                    and task.state not in TERMINAL_TASK_STATES
                    and task.state is not TaskState.BLOCKED_ON_HUMAN
                    and run.state is not RunState.PARKED
                ),
            }
            if task.quality_tier is not None:
                task_status.update({
                    "agent_role": task.agent_role,
                    "quality_tier": task.quality_tier.value,
                    "implementation_budget": (
                        task.implementation_budget.value
                        if task.implementation_budget is not None else None
                    ),
                })
            if task.decomposition_children:
                task_status["decomposition"] = {
                    "mapping": task.decomposition_mapping,
                    "children": task.decomposition_children,
                }
            # A human-closed-infeasible task surfaces WHY it was closed — read back from the
            # durable rejection artifact (#52), so status output is self-explanatory.
            if task.state is TaskState.CLOSED_INFEASIBLE:
                record = self.store.load_rejection(run_id, ref.task_id)
                if record:
                    task_status["rejection_reason"] = record.get("reason")
            # In-flight activity (#66, opt-in): for a non-terminal task whose current stage has
            # a live provider stream (headless/codex lanes tee it), attach a LEAN snapshot —
            # what the model is doing + when its stream last grew. Default-off so existing
            # callers and the cheap cost-poll path are byte-for-byte unchanged; the tail (raw
            # lines) is deliberately NOT attached here to keep the status JSON small.
            if (
                include_activity
                and task.current_stage is not None
                and task.state not in TERMINAL_TASK_STATES
            ):
                snap = probe_current_stream(
                    self.store.root, ref.task_id, task.current_stage.value, tail_lines=0
                )
                if snap is not None:  # a provider stream exists (headless/codex lane)
                    last_at = snap.get("last_event_at")
                    task_status["activity"] = {
                        "current_activity": snap.get("current_activity"),
                        "events_seen": snap.get("events_seen"),
                        "last_event_at": last_at,
                        "seconds_since_event": (
                            round(max(0.0, time.time() - last_at), 1)
                            if isinstance(last_at, (int, float))
                            else None
                        ),
                    }
            # Spec staleness (#271, opt-in): does the upstream issue still say what this
            # task's snapshotted prompt says? Divergence used to be entirely invisible —
            # an amended issue reached nothing and nothing said so. Only for a task that
            # still has stages to render (a terminal task's snapshot is history, not
            # input), and never fatal: an unreachable source attaches spec_check_error.
            if check_spec and task.state not in TERMINAL_TASK_STATES:
                task_status["spec"] = self.spec_staleness(run_id, ref.task_id, task=task)
            tasks[ref.task_id] = task_status
        # One ledger read shared by the summary, the audit, and the cost-summary.md
        # refresh (status() used to read the ledger twice) — filtered to THIS run's rows
        # (#281 defense in depth), so a shared/legacy ledger can't inflate the report.
        rows = self.run_rows(run_id)
        summary = self.ledger.summary(rows=rows)
        budget = self._budget_status(run, rows)  # #34: None unless a budget is set
        self.store.write_run_artifact(
            "cost-summary.md", render_cost_summary(run_id, summary, budget=budget)
        )
        # cost-report.md (the richer per-stage/-task + session-reuse breakdown) is NOT
        # written here: status() is the cheap poll path and analysis() re-scans the whole
        # ledger. It is produced at run finalize and on demand via the `cost-report` CLI.
        # A poll of an already-terminal run just recreated a cost-artifact lock; sweep it
        # (safe only because the run is terminal — a mid-run poll leaves live locks alone).
        if run.state in TERMINAL_RUN_STATES:
            self.store.sweep_locks()
        # ONE events.jsonl read shared by the lease audit and the panel block (#285) — the
        # same discipline as the single ledger read above; status() is the cheap poll path.
        events = self.store.read_events(run_id)
        return {
            "run_id": run_id,
            "run_state": run.state.value,
            "progress": progress.model_dump(),
            "tasks": tasks,
            "cost": summary,
            "budget": budget,  # #34: metered spend vs. budget (None when no budget set)
            # #259: a clean interactive handoff is run-level, not a stale task or a
            # human decision. The block is present only for the active park episode.
            "supervisor_parked": (
                {
                    "at": run.supervisor_parked_at,
                    "reason": run.supervisor_park_reason,
                    "resume_command": run.supervisor_resume_command,
                    "context": run.supervisor_context,
                }
                if run.state is RunState.PARKED
                else None
            ),
            # #313/#323: who is driving this run's scheduler loop, whether that process is
            # still alive, and what its own log says it was last doing (last heartbeat +
            # age + state) — so "the model went quiet", "the driver is sleeping out a
            # capacity stall", and "nobody is driving this run any more" are all
            # distinguishable from a poll (watch renders the difference).
            # #257: why a SUPERSEDED run stops, read off the Run doc (None on every other
            # run). Without it a retired run is a terminal mystery from a poll — the
            # explanation would live only in events.jsonl.
            "superseded": (
                {"reason": run.superseded_reason, "superseded_by": run.superseded_by,
                 "retired_by": run.retired_by, "at": run.superseded_at}
                if run.state is RunState.SUPERSEDED else None
            ),
            "driver": self.driver_liveness(run_id, run=run),
            "lane_audit": self.lane_audit(run_id, rows=rows),
            # #175: dispatch/record balance — flags orphaned leases (the #142 failure mode)
            # automatically at every poll / batch completion instead of by hand-count.
            "events_audit": self.events_audit(run_id, events=events),
            # #268/#285/#288: what the review panels had to cap or give up on — and whether a
            # requested panel ran at all — so a degraded review is visible from a poll
            # instead of only inside a stage log.
            "review_panel": self.review_panel_audit(run_id, events=events),
            # #357: a completion note that never reached a human — and the unfiled review
            # findings it was carrying, which have no other channel — so such a run does not
            # read as clean from a poll.
            "completion_notes": self.completion_notes_audit(run_id, events=events),
        }

    def completion_notes_audit(self, run_id: str, *, events: list[dict] | None = None) -> dict:
        """Which completion notes never reached a human, and what they were carrying (#357).

        The completion note is the ONLY channel for the review findings the engine
        deliberately does not file (``fix_now``/``drop``/over the #188 cap), and publishing
        it is a best-effort external call. On ``batch-codex-3`` every note failed and three
        valid ``fix_now`` findings were recoverable only by hand-reading per-stage JSON.

        Derived from the SAME single events read as the other audit blocks (no second source
        of truth). A task is undelivered when its last ``completion_note_*`` outcome is a
        failure — a later successful publish (a re-run of finalize) clears it, so the audit
        reports the current state rather than run-history. ``unfiled_findings`` counts only
        the findings carried by notes that are STILL undelivered; ``notes`` is every
        undelivered note's ``{task_id, error, note_file, unfiled}`` so a human polling the
        run can recover the payload without opening the log tree, and ``persist_failed``
        flags the worse case where even the durable artifact could not be written."""
        events = self.store.read_events(run_id) if events is None else events
        latest: dict[str, dict] = {}
        persist_failed_by_task: dict[str, int] = {}
        for ev in events:
            kind = ev.get("type")
            task_id = str(ev.get("task_id") or "unknown")
            if kind in ("completion_note_failed", "completion_note_published"):
                latest[task_id] = ev
            elif kind == "completion_note_persist_failed":
                persist_failed_by_task[task_id] = persist_failed_by_task.get(task_id, 0) + 1
        notes: list[dict] = [
            {
                "task_id": task_id,
                "error": str(ev.get("error") or ""),
                "note_file": ev.get("note_file"),
                # Tolerate a pre-#357 event (no payload) — it reports 0 unfiled, never a
                # crash, and the note is still flagged as undelivered.
                "unfiled": ev.get("unfiled") or [],
            }
            for task_id, ev in sorted(latest.items())
            if ev.get("type") == "completion_note_failed"
        ]
        return {
            "undelivered": len(notes),
            "undelivered_by_task": {n["task_id"]: 1 for n in notes},
            "unfiled_findings": sum(len(n["unfiled"]) for n in notes),
            "notes": notes,
            "persist_failed": sum(persist_failed_by_task.values()),
            "persist_failed_by_task": dict(sorted(persist_failed_by_task.items())),
            "clean": not (notes or persist_failed_by_task),
        }

    def review_panel_audit(self, run_id: str, *, events: list[dict] | None = None) -> dict:
        """Compact per-run view of how the multi-agent REVIEW panels actually went (#268/#288).

        Three degradations, all derived from the SAME single events read (no second source of
        truth), so a run that asked for panels and got something thinner says so from a poll
        instead of only inside a stage log:

        - ``notices``/``by_notice``/``by_task`` — the panel ran but fell short of its plan (a
          verifier cap it hit, an inconclusive verifier, an unrecognized dedupe rule), one
          ``review_panel_notice`` event per normalized runner notice;
        - ``plan_not_executed``/``plan_not_executed_by_task`` (#288) — a plan-bearing REVIEW
          dispatch came back with no panel output at all, i.e. the lane declared
          ``supports_plan`` and its runner ignored the plan. Without this the review is
          byte-indistinguishable from a single-reviewer one;
        - ``workflow_skipped``/``workflow_skipped_by_reason`` — the engine itself declined to
          attach a plan (``review_workflow_skipped``: the lane cannot execute one, a cheap
          lane preset, capacity, a thinning budget).

        ``clean`` is False as soon as ANY of the three appears: each means the operator's
        ``review_workflow`` opt-in did not buy what it asked for somewhere in this run, which
        is precisely the signal a human polling a batch needs (findings stay blocking either
        way, and no verdict is changed by any of this)."""
        events = self.store.read_events(run_id) if events is None else events
        by_notice: dict[str, int] = {}
        by_task: dict[str, dict[str, int]] = {}
        not_executed_by_task: dict[str, int] = {}
        skipped_by_reason: dict[str, int] = {}
        for ev in events:
            kind_of = ev.get("type")
            task_id = str(ev.get("task_id") or "unknown")
            if kind_of == "review_panel_notice":
                kind = str(ev.get("notice") or "unknown")
                by_notice[kind] = by_notice.get(kind, 0) + 1
                by_task.setdefault(task_id, {})
                by_task[task_id][kind] = by_task[task_id].get(kind, 0) + 1
            elif kind_of == "review_plan_not_executed":
                not_executed_by_task[task_id] = not_executed_by_task.get(task_id, 0) + 1
            elif kind_of == "review_workflow_skipped":
                reason = str(ev.get("reason") or "unknown")
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        return {
            "notices": sum(by_notice.values()),
            "by_notice": dict(sorted(by_notice.items())),
            "by_task": {t: dict(sorted(k.items())) for t, k in sorted(by_task.items())},
            # #288: a panel was requested and the runner did not deliver one.
            "plan_not_executed": sum(not_executed_by_task.values()),
            "plan_not_executed_by_task": dict(sorted(not_executed_by_task.items())),
            # The engine's own honest declines to attach a plan, by reason.
            "workflow_skipped": sum(skipped_by_reason.values()),
            "workflow_skipped_by_reason": dict(sorted(skipped_by_reason.items())),
            "clean": not (by_notice or not_executed_by_task or skipped_by_reason),
        }

    def lane_audit(self, run_id: str, *, rows: list[dict] | None = None) -> dict:
        """Every recorded model call ran on a sanctioned, attributed lane.

        Generalized beyond 3a: 'sanctioned' = the registry's served (mode, provider)
        cells, so the audit holds for headless/codex once those runners are
        registered. The failure mode it catches is a hidden/unattributed call —
        not a deliberately-selected lane (target.md §4: attribution, not abstinence)."""
        rows = self.run_rows(run_id) if rows is None else rows
        sanctioned = {f"{m.value}:{p.value}" for (m, p) in self.registry.sanctioned()}
        by_lane: dict[str, int] = {}
        unattributed = 0
        for row in rows:
            lane = row.get("lane") or "UNKNOWN"
            prov = row.get("provider") or "UNKNOWN"
            key = f"{lane}:{prov}"
            by_lane[key] = by_lane.get(key, 0) + 1
            if lane == "UNKNOWN" or prov == "UNKNOWN":
                unattributed += 1
        off_lane = sum(n for k, n in by_lane.items() if k not in sanctioned)
        return {
            "total_calls": len(rows),
            "by_lane": by_lane,
            "sanctioned_lanes": sorted(sanctioned),
            "unattributed": unattributed,
            "off_lane": off_lane,
            "clean": unattributed == 0 and off_lane == 0,
        }

    def events_audit(self, run_id: str, *, events: list[dict] | None = None) -> dict:
        """Balance every dispatch lease against its terminal event (#175).

        The #142 orphan bug — superseded leases inflating ``stage_dispatched`` over
        ``stage_recorded`` (29 vs 23) — was caught only because a human hand-counted the
        timeline. This makes that check automatic and self-describing: each
        ``stage_dispatched`` opens a ``work_item_id`` that must be closed by exactly one of
        ``stage_recorded`` / ``lease_superseded`` / ``dispatch_abandoned`` /
        ``dispatch_reclaimed`` (#313 — a killed driver's lease released for re-dispatch), OR still be a
        live outstanding lease (a dispatch currently in flight on a non-terminal task). A
        dispatched lease with no closing event that is not outstanding is an ORPHAN and gets
        flagged — the regression this audit exists to surface at batch completion / CI
        instead of during a manual post-run inspection of production logs.

        Robust to pre-#175 logs: a ``stage_recorded`` written before this change carries no
        ``work_item_id`` and cannot be joined by id, so those are counted and the orphan
        list is conservatively discounted by that many — old, known-good history never
        false-flags.

        Also returns a ``continuity`` block (#314): how many ``stage_dispatched`` events
        resumed a provider session (``session_ref`` set) vs. started fresh, over just the
        dispatches that carry the field at all (pre-#314 events are ``unknown`` and excluded
        from ``rate``, so old logs read as "no data" rather than a false 0%).
        """
        events = self.store.read_events(run_id) if events is None else events
        dispatched: dict[str, dict] = {}  # work_item_id -> opening dispatch info
        closed: dict[str, str] = {}  # work_item_id -> closing event type
        recorded_no_wid = 0  # pre-#175 stage_recorded rows without a joinable lease id
        counts = {"stage_dispatched": 0, "stage_recorded": 0, "lease_superseded": 0,
                  "dispatch_abandoned": 0, "dispatch_reclaimed": 0}
        # #314: session continuity, measured off the dispatch events. `session_ref` is only
        # stamped since #314, and an ABSENT key is not the same fact as an explicit null —
        # reading absence as "no session" is exactly the bug this reports (a pre-#314 run
        # would score a confident 0% when continuity was working the whole time). So a
        # dispatch that predates the field counts as UNKNOWN and is excluded from the rate.
        resumed = 0
        continuity_known = 0
        continuity_unknown = 0
        for ev in events:
            etype = ev.get("type")
            if etype in counts:
                counts[etype] += 1
            wid = ev.get("work_item_id")
            if etype == "stage_dispatched":
                if "session_ref" in ev:
                    continuity_known += 1
                    if ev.get("session_ref"):
                        resumed += 1
                else:
                    continuity_unknown += 1
                if wid:
                    dispatched[wid] = {"task_id": ev.get("task_id"),
                                       "stage": ev.get("stage"),
                                       "attempt": ev.get("attempt")}
            elif etype in ("stage_recorded", "lease_superseded", "dispatch_abandoned",
                           "dispatch_reclaimed"):
                if wid:
                    # First close wins: a later duplicate can't unflag an already-closed lease.
                    closed.setdefault(wid, etype)
                elif etype == "stage_recorded":
                    recorded_no_wid += 1

        # Live in-flight leases (held by a non-terminal task) are legitimately unclosed
        # mid-run — discount them so a status() poll of a running batch never false-flags.
        # A terminal task must NOT hold a lease, so those are deliberately NOT discounted.
        outstanding: set[str] = set()
        try:
            run = self.store.load_run(run_id)
            for ref in run.task_refs:
                if ref.state in TERMINAL_TASK_STATES:
                    continue
                doc = self.store.load_task(run_id, ref.task_id)
                if doc.pending_work_item_id is not None:
                    outstanding.add(doc.pending_work_item_id)
        except Exception:  # noqa: BLE001 - the audit is best-effort; a bad load must not raise
            pass

        orphans = [
            {"work_item_id": wid, **info}
            for wid, info in dispatched.items()
            if wid not in closed and wid not in outstanding
        ]
        # Pre-#175 logs: a work_item_id-less stage_recorded closed SOME dispatch we can't
        # attribute by id. Conservatively drop that many orphans (avoid false positives on
        # old history); new runs stamp every stage_recorded, so recorded_no_wid == 0 and the
        # list is exact.
        if recorded_no_wid and orphans:
            orphans = orphans[recorded_no_wid:]
        return {
            "dispatched": counts["stage_dispatched"],
            "recorded": counts["stage_recorded"],
            "superseded": counts["lease_superseded"],
            "abandoned": counts["dispatch_abandoned"],
            # #313: leases released by the startup reclaim. Counted separately from
            # `superseded` so a resumed-after-kill run is legible as such, and so the
            # dispatched == recorded + superseded + abandoned + reclaimed + outstanding
            # balance still closes.
            "reclaimed": counts["dispatch_reclaimed"],
            "outstanding": len(outstanding),
            "orphans": orphans,
            "clean": not orphans,
            # #314: how many dispatches resumed a provider session, over the dispatches that
            # can answer the question at all. `rate` is None (not 0.0) when nothing is
            # measurable, so "no data" never renders as "continuity never engaged".
            "continuity": {
                "resumed": resumed,
                "fresh": continuity_known - resumed,
                "known": continuity_known,
                "unknown": continuity_unknown,
                "rate": (resumed / continuity_known) if continuity_known else None,
            },
        }

    # --- helpers --------------------------------------------------------------
    def _deterministic_context(
        self, task: Task, *, stage: Stage | None = None, run: Run | None = None
    ) -> dict:
        """The structured task context a deterministic ENGINE-lane runner reads — the
        SAME facts the model lanes receive through the rendered prompt: the engine-owned
        folded context plane (branch/worktree/baseline_failures/pr_url/…) plus the task
        fields the TEST/DELIVER runners need (issue_number/title/body/task_id). Includes
        ``review_cycles`` when set (#68): the deterministic DELIVER runner uses it to
        annotate a reused PR's advisory comment with which fix cycle re-pushed the branch.
        For the INTAKE stage of a batch task with DAG dependencies (#216) it also carries
        ``dep_branches`` — the branch names of this task's COMPLETED deps — so the setup
        runner can compose their code into the dependent's worktree before its gate runs.
        Purely engine-derived, so no project-specific logic reaches a model call and it
        stays reconstructible on replay. ``setdefault`` lets a folded value win over the
        task field (e.g. a deliver-folded pr_url) without clobbering it."""
        ctx = dict(task.context)
        ctx.setdefault("task_id", task.task_id)
        if task.title:
            ctx.setdefault("title", task.title)
        if task.body:
            ctx.setdefault("body", task.body)
        if task.issue_number is not None:
            ctx.setdefault("issue_number", task.issue_number)
        if task.pr_url:
            ctx.setdefault("pr_url", task.pr_url)
        if task.pr_number is not None:
            ctx.setdefault("pr_number", task.pr_number)
        # #68: the fix-cycle count lets the deterministic DELIVER runner annotate a REUSED
        # PR with which review cycle re-pushed it (best-effort comment; never load-bearing).
        if task.review_cycles:
            ctx.setdefault("review_cycles", task.review_cycles)
        # #216: at INTAKE, compose the tree of every COMPLETED DAG dependency into the
        # dependent's worktree. The edge only ORDERS execution; without this the dependent
        # is branched off the run base and its per-PR gate never sees a sibling's shared
        # type/signature change. Empty (and so a byte-identical no-op) for a single-task
        # run, a task with no deps, or a later stage.
        if stage is Stage.INTAKE and run is not None:
            dep_branches = self._dep_branches(run, task)
            if dep_branches:
                ctx["dep_branches"] = dep_branches
        return ctx

    def _dep_branches(self, run: Run, task: Task) -> list[str]:
        """Branch names of this task's COMPLETED DAG dependencies (#216), in graph order,
        for the deterministic INTAKE runner to git-merge into the dependent's worktree.
        Sourced from each completed dep's intake-folded ``branch`` context key. Skips a
        dep that is unloadable, not yet COMPLETED, or has no resolvable branch — so a
        task whose deps aren't all composable yet degrades to today's base-only behavior
        rather than failing. Returns ``[]`` (a clean no-op) when the task has no deps."""
        deps = run.dependency_graph.get(task.task_id) or []
        branches: list[str] = []
        for dep_id in deps:
            try:
                dep = self.store.load_task(run.run_id, dep_id)
            except Exception:  # noqa: BLE001 - an unloadable dep is skipped, never fatal
                continue
            if dep.state is not TaskState.COMPLETED:
                continue
            branch = (dep.context or {}).get("branch")
            if isinstance(branch, str) and branch:
                branches.append(branch)
        return branches

    def _port_env_for_task(self, task: Task) -> dict[str, str] | None:
        """The per-task port env (#5) to export into this task's stage subprocess, or None.
        Sourced from the intake-folded ``port_base``/``port_count`` context keys; a clean
        None before intake allocates OR when the project doesn't opt into ports."""
        base = (task.context or {}).get("port_base")
        if base is None or not project_needs_ports(self.project):
            return None
        try:
            count = int((task.context or {}).get("port_count") or 0)
            return port_env_for(self.project, int(base), count)
        except (TypeError, ValueError):
            return None

    def _release_ports(self, run_id: str, task: Task) -> None:
        """Best-effort release of a terminal task's port block (#5). Guarded on the task
        actually holding one (``port_base`` in context) so a no-op project never touches the
        registry, and fully wrapped: releasing ports must NEVER break task finalize."""
        if (task.context or {}).get("port_base") is None:
            return
        try:
            registry_for_project(self.project).release(run_id, task.task_id)
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "ports_released", "run_id": run_id,
                 "task_id": task.task_id, "port_base": task.context.get("port_base")},
            )
        except Exception as exc:  # noqa: BLE001 - release is best-effort; never break finalize
            with contextlib.suppress(Exception):
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "ports_release_failed", "run_id": run_id,
                     "task_id": task.task_id, "error": str(exc)},
                )

    def reclaim_stale_ports(self, run_id: str | None = None) -> int:
        """Free port blocks left by crashed/terminal runs (#5) — called at scheduler
        startup. Host-wide (it prunes EVERY run's stale blocks, not just ``run_id``): a
        block is stale when its task is terminal in its status store, its owning pid is
        gone, or it has aged past the registry TTL. Opt-in + best-effort: a no-op for a
        project without port needs, and a swallowed error for anything that raises."""
        if not project_needs_ports(self.project):
            return 0

        def _is_terminal(rid: str, tid: str) -> bool:
            try:
                return self.store.load_task(rid, tid).state in TERMINAL_TASK_STATES
            except Exception:  # noqa: BLE001 - an unloadable run is left to pid/TTL to reclaim
                return False

        try:
            freed = registry_for_project(self.project).reclaim_stale(_is_terminal)
        except Exception:  # noqa: BLE001 - reclaim is best-effort startup hygiene
            return 0
        if freed and run_id is not None:
            with contextlib.suppress(Exception):
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "ports_reclaimed", "run_id": run_id,
                     "count": len(freed),
                     "blocks": [{"run_id": a.run_id, "task_id": a.task_id, "base": a.base}
                                for a in freed[:20]]},
                )
        return len(freed)

    def _project_commands(self) -> dict[str, str]:
        """The project's stable shell commands, folded into the prompt's cache-stable
        prefix (was decorative — declared on ProjectConfig, consumed by nothing). Only
        the generic Protocol methods are called, so the engine stays project-agnostic.
        Each maps a label to a joined command string; a command that raises/returns
        empty is simply omitted (tolerant)."""
        out: dict[str, str] = {}
        for label, getter in (
            ("install", self.project.install_cmd),
            ("test (unit)", self.project.test_unit_cmd),
            # e2e/shell were declared on the adapter but invisible to prompts — the
            # model could never learn the project HAS an e2e suite. Optional (getattr):
            # a minimal adapter without them still works.
            ("test (e2e)", getattr(self.project, "test_e2e_cmd", None)),
            ("test (shell)", getattr(self.project, "test_shell_cmd", None)),
            ("typecheck", self.project.typecheck_cmd),
        ):
            if getter is None:
                continue
            try:
                argv = getter()
            except Exception:  # noqa: BLE001 - a project command surface must never block dispatch
                continue
            if argv and argv != ["true"]:  # skip the no-op sentinel, not just empty
                out[label] = " ".join(argv)
        return out

    def _set_ref_state(self, run_id: str, task_id: str, state: TaskState) -> None:
        def mut(run: Run) -> None:
            for ref in run.task_refs:
                if ref.task_id == task_id:
                    ref.state = state

        self.store.update_run(run_id, mut)

    def _on_task_completed(self, run_id: str, task: Task) -> None:
        # Evidence-out (matches the work-in seam): mark_complete + file follow-ups from the
        # review's non-blocking findings + the improvement idea, then publish a completion
        # note. ALL of it is best-effort and wrapped: a flaky `gh`, a malformed review
        # payload (e.g. a non-string field on the un-validated interactive lane), or a
        # render error must NEVER escape record() and skip the task_completed event /
        # finalize — the task is already COMPLETED and the result can't be replayed.
        #
        # Two separately-guarded steps (#357), not one: the completion note is the ONLY
        # channel for the findings the engine deliberately does not file, so a failure while
        # marking complete / filing follow-ups must not also cost us the note.
        followups: list[dict] = []
        improvement_ref: str | None = None
        note_md: str | None = None
        ts = self.project.task_source
        try:
            if task.pr_url or task.decomposition_children:
                ts.mark_complete(task.task_id, task.pr_url)
            followups = self._file_review_followups(run_id, task, ts)
            # #188 dedup: don't file the improvement as a separate enhancement when it is
            # the same observation the reviewer also emitted as a (now-filed) non-blocking
            # finding (the #186/#187 class — one idea, filed twice). Fingerprint the filed
            # titles and suppress a matching improvement.
            # Dedup the improvement only against ACTUALLY-filed follow-ups (#190): a finding
            # whose file_followup raised is appended with ref=None, and must NOT suppress an
            # identically-titled improvement — otherwise a filing failure leaves neither in
            # the tracker (the note records it, but the backlog entry is silently lost).
            filed_fps = {
                self._issue_fingerprint(f["title"]) for f in followups if f.get("ref") is not None
            }
            improvement_ref = self._file_review_improvement(run_id, task, ts, skip_fingerprints=filed_fps)
        except Exception as exc:  # noqa: BLE001 - evidence-out must never crash finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "evidence_out_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
        try:
            # Rendered ONCE here and handed to both consumers (#359): the task source's
            # completion note and the alerting payload's "what was done" prose. The engine
            # never calls a model, so reusing this already-published artifact is what keeps
            # the alert descriptive without authoring new prose.
            #
            # Rendered in THIS block, not the one above (#357): the note is the only channel
            # for the findings the engine deliberately does not file, so a failure while
            # marking complete / filing follow-ups must not also cost us the note. Both
            # inputs default to empty/None, so a block-1 failure still yields a valid — if
            # thinner — note rather than none at all.
            note_md = render_completion_note(task, followups, improvement_ref)
            self._publish_completion_note(run_id, task, ts, note_md, followups)
        except Exception as exc:  # noqa: BLE001 - evidence-out must never crash finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "evidence_out_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "task_completed", "run_id": run_id,
             "task_id": task.task_id, "pr_url": task.pr_url,
             "followups_filed": len(followups), "improvement_filed": improvement_ref is not None,
             "review_fixups_applied": sum(fixup.applied for fixup in task.review_fixups)},
        )
        # Alerting (#359): the success half of the per-task pair, symmetric with the
        # ``task_failed`` that ``_terminal_effects`` fires. Emitted from THIS choke point
        # (not ``_terminal_effects``, which only runs the failed/rejected dispositions)
        # because it is the one place every completed task passes through exactly once —
        # ``record``'s success path AND ``_complete_ready_umbrellas``' decomposition parents.
        # Deliberately per-task and immediate rather than coalesced into a ``run_finalized``
        # digest: the whole point is proactive notice BEFORE the batch ends.
        summary = f"task {task.task_id} COMPLETED"
        if task.title:
            summary += f" — {task.title}"
        if task.pr_url:
            summary += f" ({task.pr_url})"
        self.emit_notification(
            run_id, NOTIFY_TASK_COMPLETED,
            {**self._notification_facts(run_id, task),
             "run_id": run_id, "task_id": task.task_id, "kind": NOTIFY_TASK_COMPLETED,
             "summary": summary,
             "followups_filed": len(followups),
             "improvement_ref": improvement_ref,
             "note_md": _bounded(note_md, NOTIFY_NOTE_MAX_CHARS)},
        )

    def _file_review_followups(self, run_id: str, task: Task, task_source: object) -> list[dict]:
        """File non-blocking review findings as UNLABELED follow-up issues — but only
        the ones that clear the #188 filing threshold, so task completion doesn't become a
        hydra. A finding is filed only when its ``disposition`` is explicitly ``file`` AND
        the filing cap (``Task.max_filed_followups``, falling
        back to the run-wide ``Run.max_filed_followups`` then the engine-wide default —
        #191/#196) is not yet reached; ``fix_now``/``drop`` findings and cap overflow are skipped here and
        surfaced in the completion note's "Noted, not filed" section instead (nothing is
        silently dropped). Returns ``[{"title", "ref"}]`` for the FILED findings; a no-op
        when the adapter lacks ``file_followup`` or the review reported none."""
        file_followup = getattr(task_source, "file_followup", None)
        if not callable(file_followup):
            return []
        review = task.stages.get(Stage.REVIEW)
        findings = (review.output or {}).get("non_blocking") if review else None
        if not findings:
            return []
        # Cap precedence (#191/#196): per-task override > run-wide default (set at
        # create_run) > engine constructor default. A cheap single run read at filing time
        # (mirrors the route_by_capacity pattern) so a run-wide baseline survives the
        # per-command CLI process boundary that rebuilds the engine.
        if task.max_filed_followups is not None:
            cap = task.max_filed_followups
        else:
            run = self.store.load_run(run_id)
            cap = (
                run.max_filed_followups
                if run.max_filed_followups is not None
                else self.max_filed_followups
            )
        filed: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            title = str(finding.get("title") or "").strip()  # coerce: a model may emit non-strings
            if not title:
                continue
            # #188/#228 gate: filing is opt-in. Absent, empty, unrecognized, fix-in-place,
            # and drop dispositions are surfaced in the completion note, not filed.
            disposition = str(finding.get("disposition") or "").strip().casefold()
            if disposition != "file":
                continue
            # #188 cap: past the per-task limit, additional `file` findings are also noted,
            # not filed — the completion note lists them as "over per-task cap".
            if len(filed) >= cap:
                continue
            body = (
                f"{str(finding.get('detail') or '').strip()}\n\n"
                f"_Filed automatically from the {task.task_id} review "
                f"({task.pr_url or 'PR'}) as a non-blocking follow-up._"
            )
            try:
                # No label: a review nit may be a bug, a docs gap, or an enhancement, and
                # the engine cannot tell which. triage-followups matches on the body footer,
                # not a label, and the human assigns a real one at triage (#367 follow-up).
                ref = file_followup(title=title, body=body)
            except Exception as exc:  # noqa: BLE001 - finalize must survive a flaky task source
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "followup_failed", "run_id": run_id,
                     "task_id": task.task_id, "title": title, "error": str(exc)},
                )
                filed.append({"title": title, "ref": None})
                continue
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "followup_filed", "run_id": run_id,
                 "task_id": task.task_id, "title": title, "ref": ref},
            )
            filed.append({"title": title, "ref": ref})
        return filed

    def _file_review_improvement(
        self, run_id: str, task: Task, task_source: object,
        skip_fingerprints: set[str] | None = None,
    ) -> str | None:
        """File the review's single forward-looking improvement idea as an ``enhancement``
        issue (the self-improvement loop). Returns the
        issue ref, or None when the adapter lacks ``file_followup``, the review had none,
        or the improvement was suppressed.

        Suppression cases (all return None):

        * (#223/#228) The improvement does not carry an explicit ``file`` disposition —
          the reviewer did not opt it into a standing issue.  An
          ``improvement_not_filed`` event is emitted so the decision is auditable.
          ``fixup`` is normally consumed by #227's pre-finalize rework loop; this remains
          a defensive no-file gate for legacy/hand-built completed task documents.  The
          other non-file dispositions are noted in the completion note.
        * (#188) The idea fingerprint-matches an already-filed follow-up
          (``skip_fingerprints``) — one observation must not be filed twice as both a
          non-blocking finding and an enhancement."""
        file_followup = getattr(task_source, "file_followup", None)
        if not callable(file_followup):
            return None
        review = task.stages.get(Stage.REVIEW)
        improvement = (review.output or {}).get("improvement") if review else None
        if not isinstance(improvement, dict):
            return None
        title = str(improvement.get("title") or "").strip()  # coerce: a model may emit non-strings
        if not title:
            return None
        # #223/#228 disposition gate: filing is opt-in.  A live `fixup` is handled before
        # finalize by #227; every non-file disposition is suppressed here.
        disposition = str(improvement.get("disposition") or "").strip().casefold()
        if disposition != "file":
            event_disposition = disposition if "disposition" in improvement else None
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "improvement_not_filed", "run_id": run_id,
                 "task_id": task.task_id, "title": title,
                 "disposition": event_disposition},
            )
            return None
        if skip_fingerprints and self._issue_fingerprint(title) in skip_fingerprints:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "improvement_deduped", "run_id": run_id,
                 "task_id": task.task_id, "title": title},
            )
            return None
        body = (
            f"{str(improvement.get('detail') or '').strip()}\n\n"
            f"_Filed automatically from the {task.task_id} review "
            f"({task.pr_url or 'PR'}) — the run's own improvement idea (self-improvement loop)._"
        )
        try:
            ref = file_followup(title=title, body=body, labels=["enhancement"])
        except Exception as exc:  # noqa: BLE001 - finalize must survive a flaky task source
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "improvement_failed", "run_id": run_id,
                 "task_id": task.task_id, "title": title, "error": str(exc)},
            )
            return None
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "improvement_filed", "run_id": run_id,
             "task_id": task.task_id, "title": title, "ref": ref},
        )
        return ref

    def trunk_gate(
        self, run_id: str, *, cwd: str | Path, file_fix: bool = True,
        timeout_s: int = 1800,
    ) -> dict:
        """Post-merge integrity gate (#229): shell the PROJECT ADAPTER's declared
        verification commands over an already-merged trunk checkout (``cwd``) and,
        when trunk is red and ``file_fix`` is set, auto-file a single remediation task
        so a cross-task integration break is caught before a human reads CI.

        Runs the always-present ``test_unit_cmd``/``typecheck_cmd`` pair plus the
        optional ``test_shell_cmd``/``test_e2e_cmd`` and the duck-typed ``types_cmd``
        (the static-typing leg distinct from the linter, #243) when the adapter declares
        them non-noop — each via ``subprocess.run`` mirroring ``_run_infra_reset``. The
        engine stays project-agnostic (only adapter argv, never a hardcoded
        pytest/ruff/mypy) and NEVER calls a model. A gate with no runnable command (every
        command the ``['true']`` sentinel) has nothing to fail and is reported green.

        Every leg the gate does NOT run — an adapter without the method (a pre-#243 adapter
        has no ``types_cmd``), a getter that raised, or the ``['true']``/empty no-op
        sentinel — is recorded in ``skipped:[{name, reason}]`` (never-silent), so a reader
        can distinguish an absent/skipped leg from one that ran green.

        Returns a rollup ``{run_id, green, cwd, commands:[{name, argv, rc, output_tail,
        truncated}], failing, skipped, file_fix, filed, deduped}``. ``green`` is every command
        exiting 0; a command whose invocation raises (missing binary, timeout) is recorded
        red with the error in its tail. Best-effort filing mirrors ``_file_review_followups``
        (``ref=None`` + a ``followup_failed`` event on a raising task source, never a crash)
        and dedups on a prior ``trunk_gate_fix_filed`` event so a re-invocation never files
        the fix twice.

        A ``cwd`` that is not an existing directory is a misconfigured invocation, not a
        red trunk: the gate reports red (synthetic ``cwd_check`` command, ``rc=-1``) and
        emits ``trunk_gate_error``/``reason=cwd_not_found`` rather than silently running the
        commands against the process's own cwd — and files nothing (there is nothing to
        remediate)."""
        cwd_path = Path(cwd)
        # Caller contract (never-silent): the invoker must ensure the merged-trunk checkout
        # exists. If it does not, `subprocess.run(cwd=None, …)` would silently verify the
        # PROCESS's own cwd — a different tree than requested — and could report green while
        # having tested the wrong checkout. The gate must never verify a tree other than the
        # one it was asked to. So a missing cwd is reported red (a misconfigured invocation),
        # with nothing to remediate — the invocation itself is wrong, not the trunk — hence no
        # filing. The CLI still exits non-zero on `green=False` so it can't be mistaken for a
        # pass.
        if not cwd_path.is_dir():
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "trunk_gate_error", "run_id": run_id,
                 "cwd": str(cwd_path), "reason": "cwd_not_found"},
            )
            return {
                "run_id": run_id, "green": False, "cwd": str(cwd_path),
                "commands": [{"name": "cwd_check", "argv": [], "rc": -1,
                              "output_tail": f"cwd not found: {cwd_path}",
                              "truncated": False}],
                "failing": ["cwd_check"], "file_fix": file_fix, "filed": None,
                "deduped": False,
            }
        run_cwd = str(cwd_path)
        commands: list[dict] = []
        # Never-silent: a leg the gate does NOT run — the adapter omits the method
        # (``types`` on a pre-#243 adapter), its getter raised, or it returned the
        # ``['true']``/empty no-op sentinel — is recorded here with WHY, not dropped, so a
        # reader can tell an absent/skipped leg from one that ran green.
        skipped: list[dict] = []
        for name, getter in (
            ("test_unit", self.project.test_unit_cmd),
            ("test_e2e", getattr(self.project, "test_e2e_cmd", None)),
            ("test_shell", getattr(self.project, "test_shell_cmd", None)),
            ("typecheck", self.project.typecheck_cmd),
            # Distinct static-typing leg (#243): the adapter's type checker where that is a
            # command separate from the linter (selfhost: mypy vs ruff). Duck-typed —
            # ``getattr(..., None)`` so a legacy/external adapter without ``types_cmd``
            # degrades to a skip (recorded below), never a crash.
            ("types", getattr(self.project, "types_cmd", None)),
        ):
            if getter is None:
                skipped.append({"name": name, "reason": "absent"})
                continue
            try:
                argv = getter()
            except Exception:  # noqa: BLE001 - a project command surface must never break the gate
                skipped.append({"name": name, "reason": "getter_raised"})
                continue
            if not argv or argv == ["true"]:  # skip the no-op sentinel, not just empty
                skipped.append({"name": name, "reason": "noop"})
                continue
            try:
                proc = subprocess.run(  # noqa: S603
                    argv, cwd=run_cwd, capture_output=True, text=True, timeout=timeout_s
                )
                rc = proc.returncode
                tail, truncated = _tail((proc.stdout or "") + (proc.stderr or ""))
            except (OSError, subprocess.SubprocessError) as exc:
                rc = -1  # a command that could not even run (missing binary, timeout) is red
                tail, truncated = f"error ({type(exc).__name__}): {exc}", False
            commands.append({"name": name, "argv": list(argv), "rc": rc,
                             "output_tail": tail, "truncated": truncated})

        # `all([])` is True: a gate with nothing to run is not "red" — auto-filing on an
        # empty command set would be a false alarm, so an empty gate reports green.
        green = all(c["rc"] == 0 for c in commands)
        failing = [c["name"] for c in commands if c["rc"] != 0]
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "trunk_gate_ran", "run_id": run_id, "cwd": str(cwd_path),
             "green": green, "failing": failing,
             "commands": [{"name": c["name"], "rc": c["rc"]} for c in commands],
             "skipped": skipped},
        )
        result: dict = {
            "run_id": run_id, "green": green, "cwd": str(cwd_path), "commands": commands,
            "failing": failing, "skipped": skipped, "file_fix": file_fix, "filed": None,
            "deduped": False,
        }
        if green:
            return result

        # Red: state the red rollup regardless of whether we go on to file.
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "trunk_gate_red", "run_id": run_id, "cwd": str(cwd_path),
             "failing": failing},
        )
        if not file_fix:
            return result

        # Light dedup (never-silent): a prior successful filing for this run means the fix
        # task already exists — skip a second one and say so, rather than spawn duplicates.
        prior = next(
            (e for e in self.store.read_events(run_id)
             if e.get("type") == "trunk_gate_fix_filed"),
            None,
        )
        if prior is not None:
            result["deduped"] = True
            result["filed"] = prior.get("ref")
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "trunk_gate_fix_skipped_duplicate", "run_id": run_id,
                 "existing_ref": prior.get("ref")},
            )
            return result

        result["filed"] = self._file_trunk_gate_fix(run_id, commands, failing)
        return result

    def _file_trunk_gate_fix(
        self, run_id: str, commands: list[dict], failing: list[str]
    ) -> str | None:
        """File the single post-merge remediation issue for a red trunk (#229). Best-effort
        exactly like ``_file_review_followups``: a missing/raising ``file_followup`` yields
        ``ref=None`` (with a ``followup_failed`` event on a raise) and never crashes; a
        successful filing emits ``trunk_gate_fix_filed`` — also the dedup marker."""
        task_source = getattr(self.project, "task_source", None)
        file_followup = getattr(task_source, "file_followup", None)
        if not callable(file_followup):
            return None
        title = f"Post-merge trunk gate red: {', '.join(failing) or 'unknown'} (run {run_id})"
        body = self._render_trunk_gate_fix_body(run_id, commands, failing)
        try:
            ref = file_followup(title=title, body=body, labels=["bug"])
        except Exception as exc:  # noqa: BLE001 - the gate must survive a flaky task source
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "followup_failed", "run_id": run_id,
                 "title": title, "error": str(exc)},
            )
            return None
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "trunk_gate_fix_filed", "run_id": run_id,
             "title": title, "ref": ref, "failing": failing},
        )
        return ref

    def _render_trunk_gate_fix_body(
        self, run_id: str, commands: list[dict], failing: list[str]
    ) -> str:
        """The remediation issue body: the failing command names + output tails, the run id,
        and the batch's task/PR-URL list so the fixer has the integration context (#216/#229)."""
        lines = [
            f"The post-merge trunk gate for run `{run_id}` is **red**: the merged trunk fails "
            f"verification even though each task's PR was individually green — a cross-task "
            f"integration break (#216/#229).",
            "",
            f"**Failing commands:** {', '.join(failing) or 'unknown'}",
            "",
        ]
        for c in commands:
            if c["rc"] == 0:
                continue
            trunc = " (last lines only)" if c.get("truncated") else ""
            lines += [
                f"### `{c['name']}` — rc={c['rc']}",
                f"`{' '.join(c['argv'])}`",
                "",
                f"```\n{c['output_tail']}\n```{trunc}",
                "",
            ]
        batch = self._batch_task_pr_list(run_id)
        if batch:
            lines.append("**Batch tasks merged into this trunk:**")
            lines += [f"- `{e['task_id']}` — {e['pr_url'] or '(no PR)'}" for e in batch]
            lines.append("")
        lines.append("_Filed automatically by the post-merge trunk gate (#229)._")
        return "\n".join(lines)

    def _batch_task_pr_list(self, run_id: str) -> list[dict]:
        """The run's ``task_id``/PR-URL pairs, for trunk-gate remediation context.
        Best-effort: a missing run or an unreadable task doc degrades to fewer/absent
        entries rather than crashing the gate's filing path."""
        out: list[dict] = []
        try:
            run = self.store.load_run(run_id)
        except Exception:  # noqa: BLE001 - context enrichment must never break filing
            return out
        for ref in run.task_refs:
            try:
                doc = self.store.load_task(run_id, ref.task_id)
            except Exception:  # noqa: BLE001 - one unreadable task doc must not drop the rest
                out.append({"task_id": ref.task_id, "pr_url": None})
                continue
            out.append({"task_id": ref.task_id, "pr_url": doc.pr_url})
        return out

    def _publish_completion_note(
        self, run_id: str, task: Task, task_source: object, body: str,
        followups: list[dict],
    ) -> None:
        """Persist the run's completion evidence, then publish it via the adapter's
        ``publish_note`` hook. Failure is logged, never fatal to finalize.

        Persist-then-publish (#357): the note carries the review findings the engine
        deliberately does NOT file (``fix_now``/``drop``/over the #188 cap), so publishing
        was a single point of failure for that whole class of output — on ``batch-codex-3``
        every note failed and three valid findings reached nobody. The rendered note is now
        written to ``stages/<task>/completion-note.md`` FIRST, and always — including when
        the adapter exposes no ``publish_note`` hook at all — so the payload is durable
        independent of delivery. Both outcomes are evented with the unfiled findings inline
        (``completion_note_published`` / ``completion_note_failed``), so ``events.jsonl``
        alone answers "what did this run tell me that I never saw?" and a delivered note
        never reads like one that was never attempted.

        Takes the ALREADY-RENDERED note (#359) rather than rendering it here: the caller
        also feeds the same markdown to the ``task_completed`` alert, and rendering it twice
        would risk the note published to the PR and the note mailed to the operator drifting
        apart. ``followups`` is still needed to compute the unfiled set for the events."""
        unfiled = unfiled_findings(
            (task.stages[Stage.REVIEW].output or {}) if Stage.REVIEW in task.stages else {},
            followups,
        )
        note_file: str | None = None
        try:
            note_file = str(self.store.write_completion_note(task.task_id, body))
        except Exception as exc:  # noqa: BLE001 - an unwritable log dir must not break finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "completion_note_persist_failed", "level": "warning",
                 "run_id": run_id, "task_id": task.task_id, "error": str(exc)},
            )
        publish_note = getattr(task_source, "publish_note", None)
        if not callable(publish_note):
            return
        try:
            publish_note(task.task_id, body, pr_url=task.pr_url)
        except Exception as exc:  # noqa: BLE001 - finalize must survive a flaky task source
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "completion_note_failed", "level": "warning",
                 "run_id": run_id, "task_id": task.task_id, "error": str(exc),
                 # The payload, not just the fact of the failure: an undelivered note's
                 # unfiled findings have no other channel to a human.
                 "note_file": note_file, "unfiled": unfiled, "unfiled_count": len(unfiled)},
            )
        else:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "completion_note_published", "run_id": run_id,
                 "task_id": task.task_id, "note_file": note_file,
                 "unfiled_count": len(unfiled)},
            )

    def _maybe_publish_progress(self, run_id: str, task: Task) -> None:
        """Mid-run progress commentary (#64): if the run opted in (``progress_comments``)
        and the task source exposes ``publish_progress``, upsert a compact progress body onto
        the driving issue/PR. Throttled per (run, task) and best-effort — a missing hook, a
        disabled run, or a RAISING hook NEVER breaks record() (mirrors ``publish_note``);
        each attempt is evented (``progress_published`` / ``progress_publish_failed``)."""
        run = self.store.load_run(run_id)
        if not getattr(run, "progress_comments", False):
            return
        publish = getattr(self.project.task_source, "publish_progress", None)
        if not callable(publish) or task.issue_number is None:
            return
        # Throttle: skip a rapid successive publish unless the task is now terminal — the
        # final state must always land, whatever the timing.
        key = (run_id, task.task_id)
        now = time.monotonic()
        last = self._last_progress_at.get(key)
        if (
            not task.is_terminal
            and last is not None
            and (now - last) < self.progress_throttle_s
        ):
            return
        marker = f"orchestrator:progress:{task.task_id}"
        body = render_progress(task)
        try:
            publish(task.task_id, body, marker=marker, pr_url=task.pr_url)
        except Exception as exc:  # noqa: BLE001 - progress commentary must never break a run
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "progress_publish_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
            return
        self._last_progress_at[key] = now
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "progress_published", "run_id": run_id,
             "task_id": task.task_id, "target": "pr" if task.pr_url else "issue",
             "task_state": task.state.value},
        )

    # --- cross-run learnings KB (#72) ----------------------------------------
    def _learnings_kb_enabled(self) -> bool:
        """Feature gate: the engine param AND the live ``ORCHESTRATOR_NO_LEARNINGS_KB``
        env escape hatch (read here, not cached, so a run can be toggled by environment)."""
        if not self.use_learnings_kb:
            return False
        return os.environ.get("ORCHESTRATOR_NO_LEARNINGS_KB", "").strip().lower() not in (
            "1", "true", "yes", "on",
        )

    def _learnings_kb_path(self) -> Path:
        """The per-project KB file. Default ``<runs-root>/learnings-kb.jsonl`` (the run-log
        root is this run's store-root parent); a project may override via ``learnings_kb_path``
        and ops via the ``ORCHESTRATOR_LEARNINGS_KB_PATH`` env var (both in resolve_kb_path)."""
        return resolve_kb_path(self.store.root.parent, self.project)

    def _task_labels(self, task: Task) -> list[str]:
        """Best-effort issue labels for KB recall (enriches the title tokens). Wrapped: a
        flaky/absent task source must never break a dispatch, so any failure yields []."""
        try:
            spec = self.project.task_source.resolve(task.task_id)
            return [str(x) for x in (getattr(spec, "labels", None) or [])]
        except Exception:  # noqa: BLE001 - recall enrichment must never break next_work
            return []

    def _recall_prior_learnings(self, task: Task, stage: Stage) -> list[str] | None:
        """Deterministic KB recall for a fresh task at intake: query by the task's title
        tokens (+ any reachable issue labels) and the dispatched stage. Best-effort — a
        missing/corrupt KB or a raising query yields None (no fold), never an exception."""
        try:
            path = self._learnings_kb_path()
            if not path.exists():
                return None
            title_tokens = _kb_tokenize(task.title)
            for label in self._task_labels(task):
                title_tokens.extend(_kb_tokenize(label))
            query = {
                "files": list(task.context.get("files_changed") or []),
                "stage": stage.value,
                "failure_kind": None,
                "title_tokens": title_tokens,
            }
            hits = relevant_learnings(path, query, limit=5)
            return hits or None
        except Exception as exc:  # noqa: BLE001 - recall must never break a dispatch
            self.store.append_event(
                task.run_id,
                {"ts": _now(), "type": "learnings_recall_failed", "run_id": task.run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
            return None

    def _harvest_task_learnings(self, run_id: str, task: Task) -> None:
        """Harvest a finished task's learnings into the durable KB (best-effort). Only tasks
        that actually LEARNED something have non-empty learnings — a clean first-pass task
        harvests nothing (harvest_from_task returns []), so no noise and no event. NEVER
        breaks finalize: a raising/corrupt KB is swallowed + evented."""
        if not self._learnings_kb_enabled():
            return
        try:
            written = harvest_from_task(self._learnings_kb_path(), task, run_id)
        except Exception as exc:  # noqa: BLE001 - KB harvest must never break finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "learnings_harvest_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
            return
        if written:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "learnings_harvested", "run_id": run_id,
                 "task_id": task.task_id, "count": len(written)},
            )

    def _harvest_process_retrospective(self, run_id: str, task: Task) -> None:
        """Persist REVIEW's process lesson for deterministic cross-run recurrence checks."""
        if not self._learnings_kb_enabled():
            return
        try:
            written = harvest_process_retrospective(self._learnings_kb_path(), task, run_id)
        except Exception as exc:  # noqa: BLE001 - evidence harvest must never break finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "process_harvest_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
            )
            return
        if written:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "process_harvested", "run_id": run_id,
                 "task_id": task.task_id, "count": len(written)},
            )

    def _meta_proposals_path(self) -> Path:
        """Return the filing ledger beside the resolved cross-run learnings KB."""
        return self._learnings_kb_path().with_name("meta-proposals.jsonl")

    def _file_meta_proposals(self, run_id: str) -> None:
        """File newly recurring process complaints through the current task source.

        Detection and filing are best-effort run-finalize effects. A successful tracker
        reference is ledgered; a missing/raising hook is non-fatal and leaves the cluster
        eligible for a later run to retry. Each cluster's ledger recheck, external filing,
        and ledger append share one guard so concurrent finalizers cannot file duplicates.
        """
        if not self._learnings_kb_enabled():
            return
        file_followup = getattr(self.project.task_source, "file_followup", None)
        if not callable(file_followup):
            return
        try:
            proposals = recurring_proposals(read_kb_entries(self._learnings_kb_path()))
            ledger_path = self._meta_proposals_path()
        except Exception as exc:  # noqa: BLE001 - detection is best-effort evidence-out
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "meta_proposal_failed", "run_id": run_id,
                 "error": str(exc)},
            )
            return
        for proposal in proposals:
            title = proposal_title(proposal)
            try:
                with proposal_filing_guard(ledger_path, proposal["key"]) as should_file:
                    if not should_file:
                        continue
                    ref = file_followup(
                        title=title,
                        body=proposal_body(proposal),
                        labels=["meta-authoring", "enhancement"],
                    )
                    if not ref:
                        raise RuntimeError("file_followup returned no reference")
                    appended = append_filing(
                        ledger_path,
                        {"key": proposal["key"], "ref": str(ref), "filed_at": _now(),
                         "run_id": run_id},
                    )
                    if not appended:
                        raise RuntimeError("proposal filing claim was not recorded")
            except Exception as exc:  # noqa: BLE001 - filing must never break finalize
                self.store.append_event(
                    run_id,
                    {"ts": _now(), "type": "meta_proposal_failed", "run_id": run_id,
                     "key": proposal["key"], "title": title, "error": str(exc)},
                )
                continue
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "meta_proposal_filed", "run_id": run_id,
                 "key": proposal["key"], "title": title, "ref": str(ref)},
            )

    def _emit_retrospective(self, run_id: str) -> None:
        """Write ``retrospective.md`` and harvest its distilled patterns — for EVERY
        finalized run, not only a FAILED one.

        This used to be gated on ``RunState.FAILED`` ("nothing to retrospect on a clean
        run"). That premise was wrong, and measurably so: across 26 finalized runs only 2
        emitted a retrospective, while the learnings KB accumulated 18 review-rejection
        entries — every one of them from a run that finished GREEN. A run that burned three
        review cycles, retried a stage, and shipped is exactly the run worth retrospecting;
        "did it end green" is not the same question as "did it go smoothly", and only the
        first one was being asked. The KB's cross-run recall is fed from here, so the gate
        was starving the loop that makes each run teach the next.

        Deterministic (a fold over the run's own durable artifacts — no model call), so a
        clean run pays nothing but the write. On a genuinely uneventful run the document
        honestly says ``_No failures recorded._`` and ``_harvest_retrospective`` finds no
        distilled pattern to persist — a thin artifact, not a misleading one.

        Best-effort in the same sense as ``_harvest_task_learnings``: a raising retrospective
        must never break finalize, since the run's real work is already done and its state
        already written. The emission event is deduped on a prior one so a repeat
        ``_maybe_finalize_run`` (an out-of-band ``reject()`` after every task went terminal)
        rewrites the artifact idempotently without appending a second receipt."""
        try:
            retro = self.retrospective(run_id)
            self.store.write_run_artifact("retrospective.md", render_retrospective(retro))
        except Exception as exc:  # noqa: BLE001 - a retrospective must never break finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "retrospective_failed", "run_id": run_id,
                 "error": str(exc)},
            )
            return
        already = any(
            ev.get("type") == "retrospective_emitted"
            for ev in self.store.read_events(run_id)
        )
        if not already:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "retrospective_emitted", "run_id": run_id,
                 "run_state": retro.get("run_state")},
            )
        # #72: harvest the retrospective's distilled cross-task patterns into the KB too
        # (the per-task learnings were harvested at each task's finalize). Idempotent on its
        # own — ``append_kb_learnings`` dedupes — so it is safe on a repeat finalize.
        self._harvest_retrospective(run_id, retro)

    def _harvest_retrospective(self, run_id: str, retro: dict) -> None:
        """Harvest the failure retrospective's DISTILLED cross-task patterns into the KB
        (#72 tie-in). The per-task learnings were already harvested at task finalize; this
        adds the recurring/cross-task signatures the retrospective aggregates. Best-effort."""
        if not self._learnings_kb_enabled():
            return
        entries: list[dict] = []
        for pat in retro.get("patterns", []):
            # Only genuinely distilled patterns are worth persisting cross-run: a signature
            # that recurred (breaker plateau) or spanned tasks (systemic) — not a one-off.
            if not (pat.get("cross_task") or (pat.get("occurrences") or 0) >= 2):
                continue
            stage = pat.get("stage")
            span = ", across tasks" if pat.get("cross_task") else ""
            sample = (pat.get("sample_error") or "no sample error").strip()
            entries.append(
                {"run_id": run_id, "task_id": None, "kind": "failure", "stage": stage,
                 "failure_kind": None, "files": [],
                 "text": f"recurring failure at {stage} "
                         f"({pat.get('occurrences')}x{span}): {sample}"}
            )
        if not entries:
            return
        try:
            written = append_kb_learnings(self._learnings_kb_path(), entries)
        except Exception as exc:  # noqa: BLE001 - KB harvest must never break finalize
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "learnings_harvest_failed", "run_id": run_id,
                 "error": str(exc)},
            )
            return
        if written:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "learnings_harvested", "run_id": run_id,
                 "source": "retrospective", "count": len(written)},
            )

    def _cascade_from(self, run_id: str, failed_task_id: str) -> None:
        """Transitively cascade-block every dependent of a failed task (fix D14)."""
        run = self.store.load_run(run_id)
        if not run.dependency_graph:
            return
        states = {ref.task_id: ref.state for ref in run.task_refs}
        states[failed_task_id] = TaskState.FAILED
        blocked = Dag(run.dependency_graph).transitive_cascade(failed_task_id, states)
        for tid in blocked:
            self.store.update_task(
                run_id, tid, lambda t: setattr(t, "state", TaskState.CASCADE_BLOCKED)
            )
            self._set_ref_state(run_id, tid, TaskState.CASCADE_BLOCKED)
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "cascade_blocked", "run_id": run_id,
                 "task_id": tid, "caused_by": failed_task_id},
            )

    def _write_final_cost_artifacts(self, run_id: str, run: Run) -> None:
        """Write a terminal run's cost artifacts (the per-record write was removed for
        O(N^2)) — this run's rows only (#281 defense in depth). Shared by the derived
        finalize rollup and the declared ``retire`` path (#257), so a retired run gets the
        same durable cost record as any other terminal run."""
        rows = self.run_rows(run_id)
        self.store.write_run_artifact(
            "cost-summary.md",
            render_cost_summary(
                run_id, self.ledger.summary(rows=rows),
                budget=self._budget_status(run, rows),  # #34
            ),
        )
        self.store.write_run_artifact(
            "cost-report.md", render_cost_report(run_id, self.ledger.analysis(rows=rows))
        )

    def _finalize_roster(self, run: Run) -> list[dict]:
        """Per-task roster for the ``run_finalized`` alert (#359): ``{task_id, state, title,
        pr_url}`` per task, in run order.

        ``TaskRef`` carries only the state cache, so the PR url and title come from the task
        DOCS — N reads, taken once at the single finalize transition (the same path already
        scans the whole ledger for the cost artifacts). Best-effort per task AND overall: a
        finalize must never be broken by an unreadable task doc, so an unloadable task
        degrades to its ref-level facts rather than losing the whole roster."""
        roster: list[dict] = []
        for ref in run.task_refs:
            entry = {"task_id": ref.task_id, "state": ref.state.value,
                     "title": None, "pr_url": None}
            with contextlib.suppress(Exception):
                task = self.store.load_task(run.run_id, ref.task_id)
                entry["title"] = task.title or None
                entry["pr_url"] = task.pr_url
            roster.append(entry)
        return roster

    def _maybe_finalize_run(self, run_id: str) -> None:
        """Finalize the run once every task is terminal (multi-task aware)."""
        run = self.store.load_run(run_id)
        if not run.task_refs or not all(r.state in TERMINAL_TASK_STATES for r in run.task_refs):
            return
        # #257: a SUPERSEDED run state is DECLARED by the human via retire(), not derived
        # from the task states — so this rollup must never recompute over it. Without the
        # guard, a retired run containing one FAILED task would be rewritten to FAILED by
        # any later call, contradicting the run_superseded event and the Run doc's own
        # reason. Retiring is terminal in both senses: no task moves, and no rollup rules.
        if run.state is RunState.SUPERSEDED:
            return
        # Honest three-way run-level rollup (#67). An execution failure (FAILED or
        # CASCADE_BLOCKED) on ANY task dominates → the run is FAILED, even if other tasks
        # were also rejected (a mixed run must not be softened to a non-failure). With NO
        # execution failure but at least one deliberate human close (CLOSED_INFEASIBLE), the
        # run is COMPLETED_WITH_REJECTIONS: done, nothing broke, but not everything shipped —
        # neither the false-alarm FAILED nor the "all delivered" COMPLETED. Otherwise every
        # task completed cleanly → COMPLETED.
        any_failed = any(
            r.state in (TaskState.FAILED, TaskState.CASCADE_BLOCKED) for r in run.task_refs
        )
        any_rejected = any(r.state is TaskState.CLOSED_INFEASIBLE for r in run.task_refs)
        if any_failed:
            new_state = RunState.FAILED
        elif any_rejected:
            new_state = RunState.COMPLETED_WITH_REJECTIONS
        else:
            new_state = RunState.COMPLETED
        if run.state is not new_state:
            self.store.update_run(run_id, lambda r: setattr(r, "state", new_state))
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "run_finalized", "run_id": run_id,
                 "state": new_state.value},
            )
            # Alerting (#55): the "batch is done" ping. Emitted at the finalize
            # transition so it fires exactly once, on every path that finalizes a run
            # (scheduler, engine-lane record, and the out-of-band reject()).
            progress = run.progress()
            self.emit_notification(
                run_id, NOTIFY_RUN_FINALIZED,
                {"run_id": run_id, "kind": NOTIFY_RUN_FINALIZED, "state": new_state.value,
                 "summary": f"run {run_id} finalized {new_state.value} — "
                            f"{progress.completed}/{progress.total} tasks completed",
                 # #359: the per-task roster, so a sink can render a batch DIGEST — which
                 # task landed where — instead of only a completed/total count that says
                 # nothing about which of N tasks shipped.
                 "run_dir": str(self.store.root),
                 "tasks": self._finalize_roster(run)},
            )
        self._write_final_cost_artifacts(run_id, run)
        self._emit_retrospective(run_id)
        # Process observations from every terminal task are now durable. Detect recurrence
        # only at the run boundary, and ledger successful filings so replay is idempotent.
        self._file_meta_proposals(run_id)
        # Every task is terminal → no more writers → sweep the now-idle lock sentinels
        # (done LAST, after the final artifact writes that recreate their own locks).
        self.store.sweep_locks()
