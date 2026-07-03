"""The engine: ties the deterministic modules into the supervisor's operations.

CRITICAL INVARIANT: the engine NEVER calls a model. ``next_work`` emits a WorkItem;
the supervisor dispatches it on the execution lane; ``record`` ingests the returned
StageResult (cost ledger + status). Every model call therefore produces a ledger
row keyed by its actual lane — an unattributed call is structurally impossible
(closes as-built D6).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from adapters.execution.base import Registry, default_registry
from adapters.project.base import ProjectConfig

from .capacity import DEFAULT_CAPACITY, CapacityPolicy
from .cost_ledger import CostLedger
from .dag import Dag
from .errors import CapacityExhausted, ContractError
from .model_table import DEFAULT_MODEL_TABLE, ENGINE_MODEL, ModelTable
from .render import (
    format_review_issue,
    render_completion_note,
    render_cost_report,
    render_cost_summary,
    render_retrospective,
    render_stage,
    render_task_index,
)
from .retrospective import build_retrospective
from .retry import CircuitBreaker, error_signature
from .routing import DEFAULT_ROUTER, Router
from .schemas.enums import (
    LANE_STAGES,
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
from .schemas.status import Run, Task, TaskRef
from .schemas.work import LanePolicy, StageResult, WorkItem
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
        registry: Registry | None = None,
        max_attempts: int = 3,
        breaker_threshold: int = 2,
        concurrency_ceiling: int = 1,
        max_review_cycles: int = 2,
        max_rate_limit_waits: int = 4,
        rate_limit_cooldown_s: int = 900,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.project = project
        self.models = model_table
        # Single pricing source: the ledger prices with the engine's model table.
        self.ledger.model_table = model_table
        self.capacity = capacity
        self.router = router
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

    # --- run/task setup -------------------------------------------------------
    def create_run(self, run_id: str, lane: ExecutionLane = ExecutionLane.FULL) -> Run:
        run = Run(run_id=run_id, created_at=_now(), updated_at=_now(), lane=lane, state=RunState.RUNNING)
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
    ) -> Task:
        """Register a task. ``pipeline`` is the ordered stage list this task runs;
        omitted, it resolves from the lane preset (design pass §1 — a lane is a named
        pipeline, not the sequencing mechanism). ``depends_on``/``provider_tag``
        override the task source's values — the supervisor's way to supply DAG edges
        and per-task provider routing when the source has no analysis step (the old
        ``82:codex`` tag / ralph dependency analysis, human-supplied). Validation
        (non-empty, duplicate-free) is the Task model's."""
        spec = self.project.task_source.resolve(task_id)
        deps = list(depends_on) if depends_on is not None else list(spec.depends_on)
        tag = provider_tag if provider_tag is not None else spec.provider_tag
        run = self.store.load_run(run_id)
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
            issue_number=spec.issue_number,
            depends_on=deps,
            execution_lane=lane or run.lane,
            pipeline=tuple(pipeline) if pipeline else LANE_STAGES[lane or run.lane],
            max_attempts=self.max_attempts,
        )
        self.store.save_task(task)
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

    # --- dispatch / record ----------------------------------------------------
    def next_work(
        self, run_id: str, task_id: str, *, util_pct: float = 0.0, resume: bool = False
    ) -> WorkItem | None:
        # Capacity backpressure first: at the per-call gate, no new dispatch (the
        # caller waits). This gates EVERY path — including a rate-limit re-queue — so
        # graceful fallback never over-subscribes an already-saturated API.
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
        if spec.deterministic:
            # No model: route to the in-process ENGINE lane (a shell runner does the work).
            # heysoo #227 — don't ask an LLM to run `git worktree add`.
            lane = LanePolicy(
                execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False
            )
            model = ENGINE_MODEL
        else:
            lane = self.router.lane_for(stage, task)  # execution_mode × provider (§4)
            # Graceful model fallback: a queued fallback model (set when a prior dispatch of
            # this stage was rate-limited) overrides the role default so the retry runs on a
            # cheaper model. Consumed in the commit below.
            model = task.pending_fallback_model or self.models.model_for_role(
                spec.model_role, lane.provider
            )
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
        checkpoint_tag = reset_to = None
        if spec.checkpoint:
            checkpoint_tag = (
                f"task/{_ref_safe(run_id)}/{_ref_safe(task_id)}/{stage.value}/{attempt}"
            )
            # Reset only a retry (FAILED) or crash-resume (RUNNING) — a first attempt
            # starts from a clean tree by construction. Anchor = last SUCCESSFUL
            # checkpoint, so the failed attempt's debris (tracked or not) is discarded.
            if rec.status in (StageStatus.FAILED, StageStatus.RUNNING) and task.last_checkpoint:
                reset_to = task.last_checkpoint.get("tag")
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
            model=model,
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
            # fallback-to-fresh, never a correctness problem.
            session_ref=task.session_ref,
            checkpoint_tag=checkpoint_tag,
            reset_to=reset_to,
        )
        # Commit the dispatch as a locked read-modify-write: re-check the lease and
        # that the stage hasn't advanced under us, so two concurrent next_work calls
        # can't both claim the task — the loser sees the moved lease/stage and raises.
        def _commit(t: Task) -> None:
            if t.pending_work_item_id is not None and not resume:
                raise ContractError(
                    f"task {task_id} dispatch raced: lease {t.pending_work_item_id} taken"
                )
            if next_stage(t) is not stage:
                raise ContractError(f"task {task_id} stage advanced under dispatch of {stage.value}")
            begin_stage(t, stage, now=_now(), model=model, attempt=attempt)
            t.state = TaskState.RUNNING
            t.pending_work_item_id = work.id
            t.pending_content_hash = work.content_hash
            t.pending_fallback_model = None  # consumed into this dispatch's model
            t.not_before = None  # cooldown (if any) has elapsed — clear the stamp

        self.store.update_task(run_id, task_id, _commit)
        self._set_ref_state(run_id, task_id, TaskState.RUNNING)
        self.store.append_event(
            run_id,
            {
                "ts": _now(),
                "type": "stage_dispatched",
                "run_id": run_id,
                "task_id": task_id,
                "stage": stage.value,
                "attempt": attempt,
                "model": model,
                "agent": agent,
                "work_item_id": work.id,
            },
        )
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
        # and the engine share one model table; compute once, in the ledger).
        cost = self.ledger.record(result)["cost_usd"]

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
        # retry the ORIGINAL model, bounded by max_rate_limit_waits; only past that
        # budget does it degrade to a normal failure.
        cooldown_until: str | None = None
        if effective.status is ResultStatus.RATE_LIMITED and fallback_model is None:
            if task.rate_limit_waits < self.max_rate_limit_waits:
                cooldown_until = (
                    datetime.now(UTC) + timedelta(seconds=self.rate_limit_cooldown_s)
                ).isoformat()
            else:
                effective = effective.model_copy(update={
                    "status": ResultStatus.FAILURE,
                    "error": "rate-limited with no cheaper fallback available and the "
                             f"cooldown budget exhausted ({task.rate_limit_waits} waits)",
                })

        task.pending_work_item_id = None
        task.pending_content_hash = None

        outcome: str
        scope_blocked_reason: str | None = None
        review_verdict: dict | None = None
        if effective.status is ResultStatus.RATE_LIMITED:
            # Transient: re-queue the stage (RUNNING marker keeps the attempt) — either
            # immediately on a cheaper model, or after a cooldown on the original one.
            # No apply_result/learnings/breaker, but cost is recorded.
            rec = task.stages[result.stage]
            rec.status = StageStatus.RUNNING
            rec.completed_at = None
            rec.error = None
            task.state = TaskState.RETRYING
            task.updated_at = _now()
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
                # Session chaining (design pass §2): reuse across SUCCESSFUL stage
                # transitions only. A runner that reports no ref leaves the prior one
                # in place (resuming a slightly-stale session is safe: prompts are
                # self-contained and a dead session cold-starts in the transport).
                if effective.session_ref:
                    task.session_ref = effective.session_ref
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
                review_verdict = self._review_verdict(effective)
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
                # Warm retry is deliberately OFF: a failed attempt's session is as
                # likely poisoned as useful; learnings carry the distilled failure.
                task.session_ref = None
                outcome = self._handle_failure(task, effective)

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
            "lane_used": result.lane_used.model_dump(),
            "cost_usd": cost,
            "session_ref": result.session_ref,
            "checkpoint": result.checkpoint,
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
                "status": effective.status.value,
                "outcome": outcome,
                "lane": result.lane_used.execution_mode.value,
                "provider": result.lane_used.provider.value,
                "cost_usd": cost,
                "task_state": task.state.value,
            },
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
        # Post-transition run-level effects: cascade-block dependents of a failed task,
        # mark_complete + finalize the run when everything is terminal.
        if task.state is TaskState.FAILED:
            self._cascade_from(run_id, result.task_id)
        if task.state is TaskState.COMPLETED:
            self._on_task_completed(run_id, task)
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

    def _review_verdict(self, result: StageResult) -> dict | None:
        """Interpret a completed REVIEW stage's verdict; None when there is nothing to act
        on (not the review stage, or approved / field omitted — fail-OPEN like the other
        soft gates: only an explicit ``approved=false`` triggers).

        Restores the old severity gate (as-built ``orchestrator-common.sh:965``): when every
        blocking issue is a structured object explicitly marked ``severity=suggestion``,
        the rejection auto-approves (kind="auto_approved") instead of cycling — suggestions
        must not hold up an otherwise-approved PR."""
        if result.stage is not Stage.REVIEW:
            return None
        out = result.structured_output or {}
        if out.get("approved") is not False:  # explicit self-report only
            return None
        raw = out.get("issues")
        issues = raw if isinstance(raw, list) else []
        issues_text = [format_review_issue(i)[:300] for i in issues[:10]]
        suggestions_only = bool(issues) and all(
            isinstance(i, dict) and str(i.get("severity", "")).lower() == "suggestion"
            for i in issues
        )
        kind = "auto_approved" if suggestions_only else "rejected"
        return {"kind": kind, "issues_text": issues_text}

    def _apply_review_rejection(self, task: Task, verdict: dict) -> str:
        """Dispose of a rejected review: a bounded fix cycle (re-open implement→…→review
        with the blocking issues as learnings) while cycles remain, else park the task at
        the human gate with the REVIEW record re-opened as FAILED — so an approve() leads
        to a re-review, never a zombie task with no next stage."""
        summary = "; ".join(verdict["issues_text"]) or "no issues listed"
        task.learnings.append(
            f"review rejected (cycle {task.review_cycles + 1}) — blocking issues: {summary}"
        )
        # Fix work must not inherit the reviewer's session (same rationale as warm-retry
        # OFF: a rejecting session's context is as likely poisoned as useful).
        task.session_ref = None
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

    def _handle_failure(self, task: Task, result: StageResult) -> str:
        failures = None
        if result.structured_output:
            failures = result.structured_output.get("failures")
        sig = error_signature(result.stage, failures=failures, error=result.error)
        # Best-effort taxonomy over a TEST failure via the project's classifier (its
        # first production caller — the reset LOOP stays #14). An infra-classed failure
        # is an environment problem, not a code problem: it must not stack the breaker's
        # identical-code-failure streak (the old system didn't burn fix iterations on
        # infra either), though max_attempts still bounds it.
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
            return "task_failed_breaker" if breaker.tripped else "task_failed_max_attempts"
        task.state = TaskState.RETRYING
        return "stage_failed_will_retry"

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
        (record the in-flight result first — a held task must be quiescent)."""

        def _hold(t: Task) -> None:
            if t.state in TERMINAL_TASK_STATES:
                raise ContractError(f"task {task_id} is terminal ({t.state.value}); cannot hold")
            if t.pending_work_item_id is not None:
                raise ContractError(
                    f"task {task_id} has an outstanding dispatch {t.pending_work_item_id}; "
                    f"record its result before holding"
                )
            t.state = TaskState.BLOCKED_ON_HUMAN

        task = self.store.update_task(run_id, task_id, _hold)
        self._set_ref_state(run_id, task_id, TaskState.BLOCKED_ON_HUMAN)
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "held_for_approval", "run_id": run_id,
             "task_id": task_id, "what": what},
        )
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

        task = self.store.update_task(run_id, task_id, _release)
        self.store.write_approval(
            run_id, task_id,
            {"approved_by": approved_by, "at": _now(), "what": what, "run_id": run_id,
             "task_id": task_id},
        )
        self._set_ref_state(run_id, task_id, TaskState.PENDING)
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "approved", "run_id": run_id, "task_id": task_id,
             "approved_by": approved_by, "what": what},
        )
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
        return build_retrospective(run, tasks, events, stage_logs)

    def status(self, run_id: str) -> dict:
        run = self.store.load_run(run_id)
        progress = run.progress()
        tasks = {}
        for ref in run.task_refs:
            task = self.store.load_task(run_id, ref.task_id)
            tasks[ref.task_id] = {
                "state": task.state.value,
                "current_stage": task.current_stage.value if task.current_stage else None,
                "stages": {s.value: r.status.value for s, r in task.stages.items()},
                "pr_url": task.pr_url,
            }
        # One ledger read shared by the summary, the audit, and the cost-summary.md
        # refresh (status() used to read the ledger twice).
        rows = self.ledger.rows()
        summary = self.ledger.summary(rows=rows)
        self.store.write_run_artifact("cost-summary.md", render_cost_summary(run_id, summary))
        # cost-report.md (the richer per-stage/-task + session-reuse breakdown) is NOT
        # written here: status() is the cheap poll path and analysis() re-scans the whole
        # ledger. It is produced at run finalize and on demand via the `cost-report` CLI.
        # A poll of an already-terminal run just recreated a cost-artifact lock; sweep it
        # (safe only because the run is terminal — a mid-run poll leaves live locks alone).
        if run.state in (RunState.COMPLETED, RunState.FAILED):
            self.store.sweep_locks()
        return {
            "run_id": run_id,
            "run_state": run.state.value,
            "progress": progress.model_dump(),
            "tasks": tasks,
            "cost": summary,
            "lane_audit": self.lane_audit(run_id, rows=rows),
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

    # --- helpers --------------------------------------------------------------
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
            improvement_ref = self._file_review_improvement(run_id, task, ts)
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
        """File each non-blocking review finding as a deferred-scope follow-up issue, so
        nothing the reviewer noticed is silently dropped (the project's scope-ledger norm).
        Returns ``[{"title", "ref"}]``; a no-op when the adapter lacks ``file_followup`` or
        the review reported none."""
        file_followup = getattr(task_source, "file_followup", None)
        if not callable(file_followup):
            return []
        review = task.stages.get(Stage.REVIEW)
        findings = (review.output or {}).get("non_blocking") if review else None
        if not findings:
            return []
        filed: list[dict] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            title = str(finding.get("title") or "").strip()  # coerce: a model may emit non-strings
            if not title:
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

    def _file_review_improvement(self, run_id: str, task: Task, task_source: object) -> str | None:
        """File the review's single forward-looking improvement idea as an ``enhancement``
        issue (the self-improvement loop — heysoo's Innovation Brainstorm). Returns the
        issue ref, or None when the adapter lacks ``file_followup`` or the review had none."""
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
        any_failed = any(
            r.state in (TaskState.FAILED, TaskState.CASCADE_BLOCKED) for r in run.task_refs
        )
        new_state = RunState.FAILED if any_failed else RunState.COMPLETED
        if run.state is not new_state:
            self.store.update_run(run_id, lambda r: setattr(r, "state", new_state))
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "run_finalized", "run_id": run_id,
                 "state": new_state.value},
            )
        # Final cost artifacts (the per-record write was removed for O(N^2)).
        rows = self.ledger.rows()
        self.store.write_run_artifact(
            "cost-summary.md", render_cost_summary(run_id, self.ledger.summary(rows=rows))
        )
        self.store.write_run_artifact(
            "cost-report.md", render_cost_report(run_id, self.ledger.analysis(rows=rows))
        )
        # Auto-generate the failure retrospective only when the run actually failed —
        # there is nothing to retrospect on a clean run.
        if new_state is RunState.FAILED:
            self.store.write_run_artifact(
                "retrospective.md", render_retrospective(self.retrospective(run_id))
            )
            self.store.append_event(
                run_id,
                {"ts": _now(), "type": "retrospective_emitted", "run_id": run_id},
            )
        # Every task is terminal → no more writers → sweep the now-idle lock sentinels
        # (done LAST, after the final artifact writes that recreate their own locks).
        self.store.sweep_locks()
