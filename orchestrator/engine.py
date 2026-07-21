"""The engine: ties the deterministic modules into the supervisor's operations.

CRITICAL INVARIANT: the engine NEVER calls a model. ``next_work`` emits a WorkItem;
the supervisor dispatches it on the execution lane; ``record`` ingests the returned
StageResult (cost ledger + status). Every model call therefore produces a ledger
row keyed by its actual lane — an unattributed call is structurally impossible
(closes as-built D6).
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from adapters.execution.base import Registry, default_registry
from adapters.project.base import ProjectConfig

from .capacity import DEFAULT_CAPACITY, CapacityPolicy, DispatchBand
from .cost_ledger import CostLedger
from .cost_policy import BUDGET_SOFT_FRACTION, DEFAULT_COST_ROUTER, CostRouter
from .dag import Dag
from .errors import CapacityExhausted, ContractError
from .learnings_kb import (
    append_learnings as append_kb_learnings,
)
from .learnings_kb import (
    harvest_from_task,
    relevant_learnings,
    resolve_kb_path,
)
from .learnings_kb import (
    tokenize as _kb_tokenize,
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
)
from .retrospective import build_retrospective
from .retry import CircuitBreaker, error_signature
from .routing import DEFAULT_ROUTER, Router
from .schemas.enums import (
    LANE_DETERMINISTIC_STAGES,
    LANE_STAGES,
    TERMINAL_RUN_STATES,
    TERMINAL_TASK_STATES,
    Effort,
    ExecutionLane,
    ExecutionMode,
    FailureKind,
    ModelId,
    Provider,
    ResultStatus,
    RunState,
    Stage,
    StageStatus,
    TaskState,
    effort_below,
    resolve_effort,
)
from .schemas.status import Run, Task, TaskRef
from .schemas.work import LanePolicy, LaneUsed, StageResult, TokenUsage, WorkItem
from .stages import STAGE_SPECS, render_prompt
from .state_machine import (
    apply_result,
    begin_stage,
    is_done,
    next_stage,
    reset_for_fix_cycle,
    resume_point,
)
from .status_store import StatusStore
from .stream_probe import probe_current_stream


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _in_future(iso: str | None) -> bool:
    """Is an ISO timestamp still ahead of now? Unparsable/absent => False (never
    let a corrupt cooldown stamp park a task forever)."""
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso) > datetime.now(UTC)
    except ValueError:
        return False


# Default liveness window for `abandon` (#82): a mid-dispatch abandon is refused when the
# task's provider stream grew within this many seconds of now (the dispatch may still be
# alive). Five minutes is comfortably longer than a normal inter-event gap on a live
# headless stream; `force=True` overrides it when the operator knows the process is dead.
DEFAULT_ABANDON_MIN_IDLE_S = 300

# Filing threshold for review evidence-out (#188): a review can surface any number of
# non-blocking findings, but task completion must not become a hydra (one task spawning
# a dozen follow-ups, as batch-queue-1 did). Only findings dispositioned `file` (or with
# an absent disposition, for backward compatibility) are filed, and only up to this cap
# per task; `fix_now`/`drop` findings — and any `file` finding over the cap — are surfaced
# in the completion note's "Noted, not filed" section instead, so nothing is silently
# dropped without ballooning the backlog. This is the engine-wide DEFAULT (#191): it is
# overridable at engine-construction / run-create time via ``max_filed_followups`` and,
# more granularly, per task via ``add_task(max_filed_followups=...)`` — a micro-pipeline
# and a full-pipeline have very different expected review surfaces.
MAX_FILED_FOLLOWUPS_PER_TASK = 2

# Non-blocking dispositions the engine must NOT file (they are noted in the completion
# note instead). Anything else — including an absent disposition — files.
_UNFILED_DISPOSITIONS = frozenset({"fix_now", "drop"})


# Failure kinds whose committed work is NOT implicated by the failure itself, so any
# commits the attempt made before dying are worth KEEPING for the retry (#59): the model
# ran out of wall-clock (TIMEOUT) or hit a transient provider wall (RATE_LIMITED) — the
# code it committed is unrelated to why the stage failed. An infra-classified failure
# (FailureKind.INFRA — a broken environment, not broken code) is salvageable too and is
# folded in separately (it is a classifier verdict, not a ResultStatus). A plain FAILURE
# — notably a genuine TEST failure — is DELIBERATELY excluded: the committed code may BE
# the defect, so the safe default (reset to the checkpoint) stands.
SALVAGEABLE_FAILURE_STATUSES = frozenset({ResultStatus.TIMEOUT, ResultStatus.RATE_LIMITED})

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
    ) -> Run:
        """Create a run. ``budget_usd`` caps metered spend (soft warning at
        ``budget_soft_fraction``, hard PAUSE at/after the budget — #34); ``route_by_cost``
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
        repeating it per add_task; None inherits the engine constructor default. All default
        off; the routers are DISTINCT levers (USD vs rate-limit headroom vs provider outage
        vs failed-session reuse)."""
        if max_filed_followups is not None and max_filed_followups < 0:
            raise ContractError(
                f"max_filed_followups must be >= 0, got {max_filed_followups} for run {run_id}"
            )
        run = Run(
            run_id=run_id, created_at=_now(), updated_at=_now(), lane=lane,
            state=RunState.RUNNING, budget_usd=budget_usd, route_by_cost=route_by_cost,
            route_by_capacity=route_by_capacity,
            cross_provider_fallback=cross_provider_fallback,
            warm_retry=warm_retry,
            progress_comments=progress_comments,
            max_filed_followups=max_filed_followups,
        )
        self.store.save_run(run)
        return run

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
        max_filed_followups: int | None = None,
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
        ``max_filed_followups`` (#191) caps how many non-blocking review findings THIS task
        files as follow-up issues, overriding the engine-wide default for a task type whose
        expected review surface differs (a micro fix vs a full feature); None inherits the
        engine default, a negative value is rejected. Validation (non-empty, duplicate-free)
        is the Task model's.

        Cost-aware lane routing (#34): when the run enables ``route_by_cost`` AND no
        ``pipeline`` is explicitly pinned, the deterministic ``cost_router`` picks the
        lane preset from the run's remaining budget fraction (refined by ``estimate`` /
        the task's ``size:``/``estimate:`` labels) and prefers $0 deterministic
        TEST/DELIVER — every such decision is emitted as a ``lane_routed`` event (never
        silent). An explicit ``pipeline`` is always honored."""
        spec = self.project.task_source.resolve(task_id)
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
        def _register(r: Run) -> None:
            if any(ref.task_id == task_id for ref in r.task_refs):
                raise ContractError(f"task {task_id} already added to run {run_id}")
            r.task_refs.append(
                TaskRef(task_id=task_id, status_file=f"status-{run_id}-{task_id}.json")
            )
            r.dependency_graph[task_id] = list(deps)

        self.store.update_run(run_id, _register)
        task = Task(
            task_id=task_id,
            run_id=run_id,
            created_at=_now(),
            updated_at=_now(),
            state=TaskState.PENDING,
            title=spec.title,
            body=spec.body,
            provider_tag=tag,
            model_pin=model_pin,
            effort_pin=effort_pin,
            issue_number=spec.issue_number,
            depends_on=deps,
            execution_lane=effective_lane,
            pipeline=tuple(pipeline) if pipeline else LANE_STAGES[effective_lane],
            deterministic_stages=tuple(deterministic_stages or ()),
            max_attempts=self.max_attempts,
            max_filed_followups=max_filed_followups,
        )
        self.store.save_task(task)
        if route_reason is not None:
            # Routing is never silent (#34): record WHY this un-pinned task got its preset
            # (remaining budget fraction, estimate) so a downgraded run explains itself.
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "lane_routed", "run_id": run_id,
                 "task_id": task_id, **route_reason},
            )
        return task

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
        run = self.store.load_run(run_id)
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
        self, run_id: str, task_id: str, *, util_pct: float = 0.0, resume: bool = False
    ) -> WorkItem | None:
        """Emit the task's next dispatchable WorkItem, or None when there is nothing to
        dispatch (terminal/parked task, budget pause, or pipeline exhausted).

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
        supervisor crash instead. Raises ``CapacityExhausted`` at the per-call gate or
        during a rate-limit cooldown, ``ContractError`` on a lease conflict.
        """
        # Budget backpressure (#34): consult the run's metered spend against its budget at
        # this dispatch point. Once spend >= budget, do NOT dispatch new work — PAUSE the
        # run (reusing the PAUSED/unpause machinery) and return None; in-flight recorded
        # state is untouched. Gated on a budget being set (default off) and skipped on a
        # resume (that recovers an ALREADY-dispatched, already-charged lease). Placed HERE,
        # at the single dispatch point, so it catches BOTH the scheduler loop AND the
        # single-task engine-lane drain (mirrors the alerting seam's one-layer rationale).
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

        spec = STAGE_SPECS[stage]
        rec = task.stages[stage]
        run = self.store.load_run(run_id)  # for route_by_capacity (#12); cheap single read
        # Deterministic routing: a stage is run by the in-process ENGINE-lane shell runner
        # (no model call, $0) when it is globally deterministic (intake) OR the task/pipeline
        # opted it in via `deterministic_stages` (#33: TEST/DELIVER). ONE decision, two
        # sources — never a second selection mechanism.
        deterministic = spec.deterministic or stage in task.deterministic_stages
        # Resolved to an Effort (from the task pin or stage spec) or downshifted via
        # effort_below below — always an Effort member or None (#161/#202 narrowed the
        # transitional ``str | Effort | None``). StrEnum, so it flows identically into the
        # WorkItem/hash/events (#172).
        effort: Effort | None = None
        if deterministic:
            # No model: route to the in-process ENGINE lane (a shell runner does the work).
            # heysoo #227 — don't ask an LLM to run `git worktree add` / `gh pr create`.
            # No model also means no effort — the ENGINE lane has nothing to throttle.
            lane = LanePolicy(
                execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False
            )
            model = ENGINE_MODEL
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
        prompt = render_prompt(
            stage,
            task_id=task_id,
            title=task.title,
            body=task.body,
            learnings=learnings,
            context=task.context,
            project_commands=self._project_commands(),
        )
        agent = self.project.agent_for(stage, spec.agent_role)
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
            timeout_s=spec.timeout_s,
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
            context=self._deterministic_context(task) if deterministic else None,
            # #5: the task's per-task port block, exported into the stage subprocess (BOTH
            # the model CLI and the deterministic test runner) so parallel worktrees don't
            # collide on dev/test-server ports. None until intake has allocated a block (and
            # for projects that don't opt in) — a clean no-op there. Hash-excluded: derived
            # from the same durable context (port_base) the prompt is.
            env=self._port_env_for_task(task),
        )
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
            }
            # Self-describing re-dispatch (#142): stamp the resume marker and the lease it
            # supersedes so a reader can tell a genuine crash-recovery re-lease from a
            # fresh dispatch without joining on `work_item_id` against `stage_recorded`.
            if superseded_lease is not None:
                event["resume"] = True
                event["supersedes"] = superseded_lease
            evs.append(event)
            return evs

        self.store.commit_task_events(run_id, task_id, _commit, _dispatch_events)
        self._set_ref_state(run_id, task_id, TaskState.RUNNING)
        return work

    def record(self, run_id: str, result: StageResult) -> dict:
        task = self.store.load_task(run_id, result.task_id)
        # A result is only valid against the WorkItem currently outstanding for this
        # task. No outstanding dispatch (pending is None) => a replay/duplicate; reject
        # so a stale result can never be re-folded into an already-advanced stage.
        if task.pending_work_item_id is None:
            raise ContractError(
                f"no dispatch outstanding for task {result.task_id} — refusing replayed result "
                f"{result.work_item_id}"
            )
        if result.work_item_id != task.pending_work_item_id:
            raise ContractError(
                f"result work_item_id {result.work_item_id} != pending {task.pending_work_item_id}"
            )
        if result.content_hash != task.pending_content_hash:
            raise ContractError("result content_hash does not match the dispatched WorkItem")
        # content_hash is an echoed string; the result still carries its OWN
        # stage/model/attempt/run_id, and those drive pricing (result.model) and which
        # stage record we fold into (result.stage). Bind them to what was actually
        # dispatched so a buggy runner or hand-edited result can't complete the wrong
        # stage or price the wrong model. task.current_stage is the dispatched stage
        # (begin_stage set it; a dispatch is outstanding).
        if result.run_id != run_id:
            raise ContractError(f"result run_id {result.run_id} != dispatched {run_id}")
        dispatched_stage = task.current_stage
        if result.stage is not dispatched_stage:
            raise ContractError(
                f"result stage {result.stage} != dispatched {dispatched_stage}"
            )
        dispatched = task.stages[dispatched_stage]
        if result.model != dispatched.model:
            raise ContractError(
                f"result model {result.model!r} != dispatched {dispatched.model!r}"
            )
        if result.attempt != dispatched.attempt:
            raise ContractError(
                f"result attempt {result.attempt} != dispatched {dispatched.attempt}"
            )

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
        cost = self.ledger.record(result, duration_s=duration_s)["cost_usd"]

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
        # Deterministic project policy gates (#65): merge the adapter's review_findings
        # into a completed REVIEW result BEFORE the verdict is read — the old
        # merge_e2e_policy_review_finding semantics (a blocking deterministic finding
        # overrides the model's approval; the model can't skip a policy gate).
        if effective.stage is Stage.REVIEW and effective.status is ResultStatus.SUCCESS:
            effective = self._merge_policy_findings(run_id, task, effective)
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
        cooldown_until: str | None = None
        provider_out_reason: str | None = None
        if effective.status is ResultStatus.PROVIDER_UNAVAILABLE:
            provider_out_reason = effective.error or "provider reported unavailable"
        elif effective.status is ResultStatus.RATE_LIMITED and fallback_model is None:
            if task.rate_limit_waits < self.max_rate_limit_waits:
                cooldown_until = (
                    datetime.now(UTC) + timedelta(seconds=self.rate_limit_cooldown_s)
                ).isoformat()
            else:
                provider_out_reason = (
                    "rate-limited with no cheaper fallback available and the "
                    f"cooldown budget exhausted ({task.rate_limit_waits} waits)"
                )

        # Cross-provider fallthrough (#7): opt-in, one-way (codex→claude), once per stage. When
        # a CODEX dispatch's same-provider options are exhausted (provider_out_reason set) and
        # the run consented (cross_provider_fallback), re-route this stage's NEXT dispatch to the
        # equivalent claude lane instead of failing/parking. Keyed off the lane ACTUALLY used
        # (ground truth), so a claude result never falls through — no ping-pong.
        run = self.store.load_run(run_id)
        do_fallthrough = (
            provider_out_reason is not None
            and run.cross_provider_fallback
            and result.lane_used.provider is Provider.CODEX
            and result.stage not in task.fallthrough_stages
        )
        if provider_out_reason is not None and not do_fallthrough:
            # No fallthrough (flag off / not codex / already fell through once): a provider-out
            # signal degrades to a normal FAILURE — retry within the provider, then fail out,
            # exactly as before #7 existed. Idempotent for a rate-limit already FAILURE-shaped;
            # the meaningful conversion is PROVIDER_UNAVAILABLE → FAILURE.
            effective = effective.model_copy(update={
                "status": ResultStatus.FAILURE, "error": provider_out_reason,
            })

        task.pending_work_item_id = None
        task.pending_content_hash = None

        outcome: str
        scope_blocked_reason: str | None = None
        review_verdict: dict | None = None
        if do_fallthrough:
            # do_fallthrough implies provider_out_reason is not None (see above); assert it so
            # the reason: str parameter type-checks without laundering the None through.
            assert provider_out_reason is not None
            outcome = self._apply_fallthrough(task, result, provider_out_reason)
        elif effective.status is ResultStatus.RATE_LIMITED:
            # Transient: re-queue the stage (RUNNING marker keeps the attempt) — either
            # immediately on a cheaper model, or after a cooldown on the original one.
            # No apply_result/learnings/breaker, but cost is recorded.
            rec = task.stages[result.stage]
            rec.status = StageStatus.RUNNING
            rec.completed_at = None
            rec.error = None
            task.state = TaskState.RETRYING
            task.updated_at = _now()
            # Rate-limit is a salvageable KIND (SALVAGEABLE_FAILURE_STATUSES): if the
            # attempt committed real work before the 429, keep it in place so the seamless
            # re-queue (cheaper model / post-cooldown) builds on it instead of resetting to
            # the checkpoint. Same cap as the failure path (#59).
            self._apply_salvage(task, effective)
            if cooldown_until is None:
                task.pending_fallback_model = fallback_model
                outcome = "stage_rate_limited_fallback"
            else:
                task.rate_limit_waits += 1
                task.not_before = cooldown_until
                outcome = "stage_rate_limited_cooldown"
        else:
            apply_result(task, effective, now=_now(), cost_usd=cost)
            if effective.status is ResultStatus.SUCCESS:
                task.error_signatures = []  # streak resets on a clean stage
                task.rate_limit_waits = 0  # a clean stage refreshes the cooldown budget
                task.infra_resets = 0  # ... and the infra-reset budget (#14)
                task.salvage_count = 0  # ... and the salvage-keep budget (#59)
                task.salvage_in_place = False  # a clean stage leaves nothing to keep
                # Session chaining (design pass §2): reuse across SUCCESSFUL stage
                # transitions only. A runner that reports no ref leaves the prior one
                # in place (resuming a slightly-stale session is safe: prompts are
                # self-contained and a dead session cold-starts in the transport).
                if effective.session_ref:
                    task.session_ref = effective.session_ref
                    # Tag the ref with the provider that produced it (#9) so a later stage
                    # on the other provider won't try to resume a foreign session.
                    task.session_provider = effective.lane_used.provider
                # Checkpoint anchor (design pass §3): SUCCESS only — a failed or
                # gate-vetoed attempt's commits must never become a reset target.
                if effective.checkpoint:
                    task.last_checkpoint = effective.checkpoint
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
                review_verdict = self._review_verdict(effective, task)
                if scope_blocked_reason is not None:
                    task.state = TaskState.BLOCKED_ON_HUMAN
                    outcome = "scope_not_feasible_held"
                elif review_verdict is not None and review_verdict["kind"] == "rejected":
                    outcome = self._apply_review_rejection(task, review_verdict)
                elif is_done(task):
                    task.state = TaskState.COMPLETED
                    outcome = "task_completed"
                else:
                    task.state = TaskState.RUNNING
                    outcome = "stage_completed"
            else:
                # Session fate on a failure is decided inside _handle_failure (it has the
                # infra classification + the salvage decision the warm-retry policy needs).
                # Default: clear (design pass §2 fresh-after-failure). Warm retry (#8) keeps
                # it only when the run opted in AND the failure was mechanical AND the
                # worktree still matches the session — see _settle_failed_session.
                outcome = self._handle_failure(task, effective, run=run)

        # Durable per-stage log (JSON contract) + human-readable Markdown alongside.
        task.stage_counter += 1
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
            "salvage_kept": task.salvage_in_place,
            "stream_files": result.stream_files,  # #56: raw provider stdout/stderr on disk
            "persona_injected": result.persona_injected,  # #74: codex worktree AGENTS.md persona
            "structured_output": result.structured_output,
            "raw_output": result.raw_output,
            "error": effective.error,
            "completed_at": result.completed_at,
        }
        seq = task.stage_counter
        self.store.write_stage_log(result.task_id, seq, result.stage.value, payload)
        self.store.write_stage_markdown(result.task_id, seq, result.stage.value, render_stage(payload))
        self.store.save_task(task)
        self.store.write_task_index(result.task_id, render_task_index(task))
        # cost-summary.md is written at run finalization and on status() — NOT on
        # every record (that re-read the whole ledger each time: O(N^2) on long runs).
        self._set_ref_state(run_id, result.task_id, task.state)
        self.store.append_event(
            run_id,
            {
                "ts": _now(),
                "type": "stage_recorded",
                "run_id": run_id,
                "task_id": result.task_id,
                "stage": result.stage.value,
                "attempt": result.attempt,
                # #175: stamp the closed lease so the events-balance audit can join a
                # `stage_recorded` back to its opening `stage_dispatched` by work_item_id
                # (lease_superseded/dispatch_abandoned already carry it) — the join key that
                # turns the #142 hand-count into an automated orphan check.
                "work_item_id": result.work_item_id,
                "effort": result.effort,  # #96: audit alongside model/lane
                "status": effective.status.value,
                "outcome": outcome,
                "lane": result.lane_used.execution_mode.value,
                "provider": result.lane_used.provider.value,
                "cost_usd": cost,
                "task_state": task.state.value,
            },
        )
        # Mid-run progress commentary (#64): upsert the living progress comment/PR-body
        # section on the driving issue/PR at this stage boundary (opt-in, throttled,
        # best-effort). A rate-limit re-queue and a cross-provider fallthrough are NOT
        # boundaries (the same stage/attempt goes back on the wire) — skip them so the
        # running/next picture doesn't flicker.
        if not outcome.startswith("stage_rate_limited") and outcome != "provider_fallthrough":
            self._maybe_publish_progress(run_id, task)
        # Audit the cross-provider fallthrough (#7): the run consented and codex was out, so
        # the stage's NEXT dispatch is re-routed to claude — record from→to + why.
        if outcome == "provider_fallthrough":
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "provider_fallthrough", "run_id": run_id,
                 "task_id": result.task_id, "stage": result.stage.value,
                 "from": Provider.CODEX.value, "to": Provider.CLAUDE.value,
                 "reason": provider_out_reason,
                 "attempt": result.attempt},
            )
        # Audit a cooldown park: when the task may dispatch again, and how much of the
        # wait budget is spent — so a stalled-looking run explains itself in the events.
        if outcome == "stage_rate_limited_cooldown":
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "rate_limit_cooldown", "run_id": run_id,
                 "task_id": result.task_id, "stage": result.stage.value,
                 "not_before": task.not_before,
                 "waits_used": task.rate_limit_waits,
                 "waits_budget": self.max_rate_limit_waits},
            )
        # Audit the review verdict alongside the generic stage record: what the reviewer
        # rejected (or auto-approved as suggestions-only) and how the engine disposed of it.
        if review_verdict is not None:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "review_verdict", "run_id": run_id,
                 "task_id": result.task_id, "kind": review_verdict["kind"],
                 "disposition": review_verdict.get("disposition"),
                 "issues": review_verdict["issues_text"],
                 "review_cycles": task.review_cycles},
            )
        # Audit the WHY of a feasibility park alongside the generic stage record, so the
        # event stream shows the blocked_reason that routed the task to the human gate.
        if scope_blocked_reason is not None:
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "scope_not_feasible", "run_id": run_id,
                 "task_id": result.task_id, "stage": result.stage.value,
                 "blocked_reason": scope_blocked_reason},
            )
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
            self._maybe_finalize_run(run_id)
        return {
            "recorded": True,
            "outcome": outcome,
            "task_state": task.state.value,
            "stage": result.stage.value,
            "cost_usd": cost,
            "lane_attributed": lane_clean,
            "next_stage": (s.value if (s := next_stage(task)) else None),
        }

    def _stage_gate(self, result: StageResult) -> str | None:
        """Deterministic per-stage gate over a SUCCESS result; returns a veto reason or None.

        The collapsed test stage folds in the as-built 'test-validate' step (§6.1): a
        green run is necessary but not sufficient — the tests must actually exercise the
        change. The runner is asked to self-report ``tests_meaningful``; if it explicitly
        reports ``false`` we veto (retry-with-learnings) rather than ship vacuous tests.

        Fail-OPEN on a MISSING field: nothing enforces this soft field on the interactive/
        headless lanes (no JSON schema), so a model that simply omits it must not dead-end
        otherwise-green work — only an explicit ``false`` is a veto. (A stronger, schema-
        or independent-reviewer-enforced version is tracked as issue #13.)"""
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
                 "detail": format_review_issue({k: v for k, v in f.items() if k != "blocking"})}
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
        of the same finding still matches."""
        if isinstance(issue, dict):
            base = f"{str(issue.get('file') or '').strip()}:{str(issue.get('description') or '').strip()}"
        else:
            base = str(issue)
        return re.sub(r"\s+", " ", base).casefold()[:160]

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
        tests criterion, never a substantive reviewer rejection."""
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
        no_model_test_surface = (
            task.context.get("change_class") == "docs-only"
            or Stage.TEST not in task.pipeline
            or Stage.TEST in task.deterministic_stages
        )
        tests_vacuous = out.get("tests_meaningful") is False and not no_model_test_surface
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
        from the failure's substance instead of re-discovering it."""
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
        tail = (result.raw_output or "").strip()
        if tail:
            clipped = tail[-500:]
            prefix = "…" if len(tail) > 500 else ""
            lines.append(f"  output tail: {prefix}{clipped}")
        return "\n".join(lines)

    # --- human approval gate (design pass §4) ----------------------------------
    def hold_for_approval(self, run_id: str, task_id: str, what: str) -> Task:
        """Park a task at the human gate. Refuses while a dispatch is outstanding
        (record the in-flight result first — a held task must be quiescent). If the
        result can never arrive because the run was killed mid-dispatch, use
        ``abandon()`` to release the lease and drive the task terminal instead."""

        def _hold(t: Task) -> None:
            if t.state in TERMINAL_TASK_STATES:
                raise ContractError(f"task {task_id} is terminal ({t.state.value}); cannot hold")
            if t.pending_work_item_id is not None:
                raise ContractError(
                    f"task {task_id} has an outstanding dispatch {t.pending_work_item_id}; "
                    f"record its result before holding"
                )
            t.state = TaskState.BLOCKED_ON_HUMAN

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
        the gate record (who/when/what) — prose norms stay documentation."""

        def _release(t: Task) -> None:
            if t.state is not TaskState.BLOCKED_ON_HUMAN:
                raise ContractError(
                    f"task {task_id} is not held for approval (state {t.state.value})"
                )
            t.state = TaskState.PENDING

        # #199: commit the release + its `approved` event atomically (event first, task
        # doc last), so a durable PENDING transition always has its event on disk. The
        # mutator's state guard runs inside the commit, so a rejected release still raises
        # BEFORE any approval artifact is written (no spurious gate record on the error path).
        task = self.store.commit_task_events(
            run_id, task_id, _release,
            [{"ts": _now(), "type": "approved", "run_id": run_id, "task_id": task_id,
              "approved_by": approved_by, "what": what}],
        )
        self.store.write_approval(
            run_id, task_id,
            {"approved_by": approved_by, "at": _now(), "what": what, "run_id": run_id,
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
            (the human granted more budget — proceed until the new cap);
          - otherwise the human is explicitly overriding the cap, so it is REMOVED
            (``budget_usd=None``) — no further hard stops this run.
        A breaker/other pause (not over budget) leaves the budget untouched, unless
        ``raise_budget_to`` is given explicitly."""
        run = self.store.load_run(run_id)
        if run.state is not RunState.PAUSED:
            raise ContractError(f"run {run_id} is not paused (state {run.state.value})")
        over_budget = (
            run.budget_usd is not None and self.ledger.metered_spend() >= run.budget_usd
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

    # --- per-run cost budget (#34) ---------------------------------------------
    def _remaining_budget_fraction(self, run: Run) -> float:
        """Fraction of the run's budget still unspent (metered), clamped to [0, 1].
        1.0 when no budget is set (cost routing then always picks the top band)."""
        if not run.budget_usd:
            return 1.0
        spent = self.ledger.metered_spend()
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
        spent = self.ledger.metered_spend()
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
        spent = self.ledger.metered_spend(rows=rows)
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
            stage = task.current_stage
            self.emit_notification(
                run_id, "task_failed",
                {"run_id": run_id, "task_id": task.task_id, "kind": "task_failed",
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

        It synthesizes the abandonment INTERNALLY (an honest $0 cost-ledger row, raw output
        naming the reason, outcome ``dispatch_abandoned``) WITHOUT routing through
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

        # Synthesize the lease-matching abandonment honestly. No model ran, so the lane_used
        # invocation says exactly that and the token usage is empty (the ledger reprices to
        # $0). We reuse the intended lane cell (router) for accurate attribution.
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
            error=f"dispatch_abandoned: {reason}",
            completed_at=completed_at,
        )
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
        cost = self.ledger.record(synthetic, duration_s=duration_s)["cost_usd"]

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
            t.pending_fallback_model = None
            d = t.stages[stage]
            d.status = StageStatus.FAILED
            d.completed_at = completed_at
            d.error = error
            d.provider = lane.provider
            d.lane = lane.execution_mode
            d.cost_usd = cost
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
        """Structured failure retrospective over the run's durable artifacts."""
        run = self.store.load_run(run_id)
        tasks = [self.store.load_task(run_id, ref.task_id) for ref in run.task_refs]
        events = self.store.read_events(run_id)
        stage_logs = {t.task_id: self.store.read_stage_logs(t.task_id) for t in tasks}
        # #67: annotate any deliberately-closed (CLOSED_INFEASIBLE) tasks with the reason
        # read BACK from the durable rejection artifact, so a mixed failure+rejection run's
        # retrospective separates human closes from execution failures instead of ignoring
        # them. Rejection-only runs never reach here (they finalize COMPLETED_WITH_REJECTIONS,
        # which does not emit a retrospective) — this only enriches genuinely-failed runs.
        rejections = {}
        for t in tasks:
            if t.state is TaskState.CLOSED_INFEASIBLE:
                record = self.store.load_rejection(run_id, t.task_id) or {}
                rejections[t.task_id] = {"title": t.title, "reason": record.get("reason")}
        return build_retrospective(run, tasks, events, stage_logs, rejections=rejections)

    def status(
        self, run_id: str, *, stale_after_s: int = 1800, include_activity: bool = False
    ) -> dict:
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
                ),
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
            tasks[ref.task_id] = task_status
        # One ledger read shared by the summary, the audit, and the cost-summary.md
        # refresh (status() used to read the ledger twice).
        rows = self.ledger.rows()
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
        return {
            "run_id": run_id,
            "run_state": run.state.value,
            "progress": progress.model_dump(),
            "tasks": tasks,
            "cost": summary,
            "budget": budget,  # #34: metered spend vs. budget (None when no budget set)
            "lane_audit": self.lane_audit(run_id, rows=rows),
            # #175: dispatch/record balance — flags orphaned leases (the #142 failure mode)
            # automatically at every poll / batch completion instead of by hand-count.
            "events_audit": self.events_audit(run_id),
        }

    def lane_audit(self, run_id: str, *, rows: list[dict] | None = None) -> dict:
        """Every recorded model call ran on a sanctioned, attributed lane.

        Generalized beyond 3a: 'sanctioned' = the registry's served (mode, provider)
        cells, so the audit holds for headless/codex once those runners are
        registered. The failure mode it catches is a hidden/unattributed call —
        not a deliberately-selected lane (target.md §4: attribution, not abstinence)."""
        rows = self.ledger.rows() if rows is None else rows
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
        ``stage_recorded`` / ``lease_superseded`` / ``dispatch_abandoned``, OR still be a
        live outstanding lease (a dispatch currently in flight on a non-terminal task). A
        dispatched lease with no closing event that is not outstanding is an ORPHAN and gets
        flagged — the regression this audit exists to surface at batch completion / CI
        instead of during a manual post-run inspection of production logs.

        Robust to pre-#175 logs: a ``stage_recorded`` written before this change carries no
        ``work_item_id`` and cannot be joined by id, so those are counted and the orphan
        list is conservatively discounted by that many — old, known-good history never
        false-flags.
        """
        events = self.store.read_events(run_id) if events is None else events
        dispatched: dict[str, dict] = {}  # work_item_id -> opening dispatch info
        closed: dict[str, str] = {}  # work_item_id -> closing event type
        recorded_no_wid = 0  # pre-#175 stage_recorded rows without a joinable lease id
        counts = {"stage_dispatched": 0, "stage_recorded": 0,
                  "lease_superseded": 0, "dispatch_abandoned": 0}
        for ev in events:
            etype = ev.get("type")
            if etype in counts:
                counts[etype] += 1
            wid = ev.get("work_item_id")
            if etype == "stage_dispatched":
                if wid:
                    dispatched[wid] = {"task_id": ev.get("task_id"),
                                       "stage": ev.get("stage"),
                                       "attempt": ev.get("attempt")}
            elif etype in ("stage_recorded", "lease_superseded", "dispatch_abandoned"):
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
            "outstanding": len(outstanding),
            "orphans": orphans,
            "clean": not orphans,
        }

    # --- helpers --------------------------------------------------------------
    def _deterministic_context(self, task: Task) -> dict:
        """The structured task context a deterministic ENGINE-lane runner reads — the
        SAME facts the model lanes receive through the rendered prompt: the engine-owned
        folded context plane (branch/worktree/baseline_failures/pr_url/…) plus the task
        fields the TEST/DELIVER runners need (issue_number/title/body/task_id). Includes
        ``review_cycles`` when set (#68): the deterministic DELIVER runner uses it to
        annotate a reused PR's advisory comment with which fix cycle re-pushed the branch.
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
        return ctx

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
        followups: list[dict] = []
        improvement_ref: str | None = None
        try:
            ts = self.project.task_source
            if task.pr_url:
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
            self._publish_completion_note(run_id, task, ts, followups, improvement_ref)
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
             "followups_filed": len(followups), "improvement_filed": improvement_ref is not None},
        )

    def _file_review_followups(self, run_id: str, task: Task, task_source: object) -> list[dict]:
        """File non-blocking review findings as deferred-scope follow-up issues — but only
        the ones that clear the #188 filing threshold, so task completion doesn't become a
        hydra. A finding is filed only when its ``disposition`` is ``file`` (or absent, for
        backward compatibility) AND the filing cap (``Task.max_filed_followups``, falling
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
            # #188 gate: skip findings the reviewer marked as fix-in-place or drop; they are
            # surfaced in the completion note, not filed. An absent/unknown disposition
            # defaults to filing (preserves the pre-#188 behavior for the un-validated
            # interactive lane and older reviews).
            disposition = str(finding.get("disposition") or "").strip().casefold()
            if disposition in _UNFILED_DISPOSITIONS:
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
                ref = file_followup(title=title, body=body, labels=["deferred-scope"])
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
        issue (the self-improvement loop — heysoo's Innovation Brainstorm). Returns the
        issue ref, or None when the adapter lacks ``file_followup``, the review had none,
        or (#188) the idea fingerprint-matches an already-filed follow-up (``skip_fingerprints``)
        — one observation must not be filed twice as both a non-blocking finding and an
        enhancement."""
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

    def _publish_completion_note(
        self, run_id: str, task: Task, task_source: object, followups: list[dict],
        improvement_ref: str | None = None,
    ) -> None:
        """Publish the run's completion evidence via the adapter's ``publish_note`` hook
        (a no-op when absent). Failure is logged, never fatal to finalize."""
        publish_note = getattr(task_source, "publish_note", None)
        if not callable(publish_note):
            return
        body = render_completion_note(task, followups, improvement_ref)
        try:
            publish_note(task.task_id, body, pr_url=task.pr_url)
        except Exception as exc:  # noqa: BLE001 - finalize must survive a flaky task source
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "completion_note_failed", "run_id": run_id,
                 "task_id": task.task_id, "error": str(exc)},
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

    def _maybe_finalize_run(self, run_id: str) -> None:
        """Finalize the run once every task is terminal (multi-task aware)."""
        run = self.store.load_run(run_id)
        if not run.task_refs or not all(r.state in TERMINAL_TASK_STATES for r in run.task_refs):
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
                run_id, "run_finalized",
                {"run_id": run_id, "kind": "run_finalized", "state": new_state.value,
                 "summary": f"run {run_id} finalized {new_state.value} — "
                            f"{progress.completed}/{progress.total} tasks completed"},
            )
        # Final cost artifacts (the per-record write was removed for O(N^2)).
        rows = self.ledger.rows()
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
        # Auto-generate the failure retrospective only when the run actually failed —
        # there is nothing to retrospect on a clean run.
        if new_state is RunState.FAILED:
            retro = self.retrospective(run_id)
            self.store.write_run_artifact(
                "retrospective.md", render_retrospective(retro)
            )
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "retrospective_emitted", "run_id": run_id},
            )
            # #72: harvest the retrospective's distilled cross-task patterns into the KB
            # too (the per-task learnings were harvested at each task's finalize).
            self._harvest_retrospective(run_id, retro)
        # Every task is terminal → no more writers → sweep the now-idle lock sentinels
        # (done LAST, after the final artifact writes that recreate their own locks).
        self.store.sweep_locks()
