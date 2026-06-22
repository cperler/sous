"""The engine: ties the deterministic modules into the supervisor's operations.

CRITICAL INVARIANT: the engine NEVER calls a model. ``next_work`` emits a WorkItem;
the supervisor dispatches it on the execution lane; ``record`` ingests the returned
StageResult (cost ledger + status). Every model call therefore produces a ledger
row keyed by its actual lane — an unattributed call is structurally impossible
(closes as-built D6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from adapters.execution.base import Registry, default_registry
from adapters.project.base import ProjectConfig

from .capacity import DEFAULT_CAPACITY, CapacityPolicy
from .cost_ledger import CostLedger
from .dag import Dag
from .errors import CapacityExhausted, ContractError
from .model_table import DEFAULT_MODEL_TABLE, ModelTable
from .render import render_cost_summary, render_stage, render_task_index
from .retry import CircuitBreaker, error_signature
from .routing import DEFAULT_ROUTER, Router
from .schemas.enums import (
    TERMINAL_TASK_STATES,
    ExecutionLane,
    ResultStatus,
    RunState,
    StageStatus,
    TaskState,
)
from .schemas.status import Run, Task, TaskRef
from .schemas.work import StageResult, WorkItem
from .stages import STAGE_SPECS, render_prompt
from .state_machine import apply_result, begin_stage, is_done, next_stage, resume_point
from .status_store import StatusStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    # --- run/task setup -------------------------------------------------------
    def create_run(self, run_id: str, lane: ExecutionLane = ExecutionLane.FULL) -> Run:
        run = Run(run_id=run_id, created_at=_now(), updated_at=_now(), lane=lane, state=RunState.RUNNING)
        self.store.save_run(run)
        return run

    def add_task(self, run_id: str, task_id: str, lane: ExecutionLane | None = None) -> Task:
        spec = self.project.task_source.resolve(task_id)
        run = self.store.load_run(run_id)
        task = Task(
            task_id=task_id,
            run_id=run_id,
            created_at=_now(),
            updated_at=_now(),
            state=TaskState.PENDING,
            title=spec.title,
            body=spec.body,
            provider_tag=spec.provider_tag,
            issue_number=spec.issue_number,
            depends_on=spec.depends_on,
            execution_lane=lane or run.lane,
            max_attempts=self.max_attempts,
        )
        self.store.save_task(task)
        run.task_refs.append(TaskRef(task_id=task_id, status_file=f"status-{run_id}-{task_id}.json"))
        run.dependency_graph[task_id] = list(spec.depends_on)
        run.updated_at = _now()
        self.store.save_run(run)
        return task

    # --- ready (DAG + capacity) ----------------------------------------------
    def ready(self, run_id: str, *, util_pct: float = 0.0) -> list[str]:
        run = self.store.load_run(run_id)
        states = {ref.task_id: ref.state for ref in run.task_refs}
        dag = Dag(run.dependency_graph)
        candidates = dag.ready_tasks(states)
        limit = self.capacity.dispatch_limit(util_pct, self.concurrency_ceiling)
        return candidates[:limit]

    # --- dispatch / record ----------------------------------------------------
    def next_work(self, run_id: str, task_id: str, *, util_pct: float = 0.0) -> WorkItem | None:
        if self.capacity.at_capacity(util_pct):
            raise CapacityExhausted(f"at capacity ({util_pct}% >= per-call gate)")
        task = self.store.load_task(run_id, task_id)
        stage = next_stage(task)
        if stage is None:
            return None

        spec = STAGE_SPECS[stage]
        model = self.models.model_for_role(spec.model_role)
        rec = task.stages[stage]
        # Attempt is derived from the persisted stage status, not rec.error:
        #  - RUNNING  -> a crash mid-stage; re-dispatch the SAME attempt (don't reset)
        #  - FAILED   -> a real retry; bump
        #  - else     -> first attempt
        if rec.status is StageStatus.RUNNING:
            attempt = rec.attempt
        elif rec.status is StageStatus.FAILED:
            attempt = rec.attempt + 1
        else:
            attempt = 0
        learnings = "\n".join(task.learnings)
        prompt = render_prompt(
            stage, task_id=task_id, title=task.title, body=task.body, learnings=learnings
        )
        agent = self.project.agent_for(stage, spec.agent_role)
        lane = self.router.lane_for(stage, task)  # execution_mode × provider (§4)
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
        )
        begin_stage(task, stage, now=_now(), model=model, attempt=attempt)
        task.state = TaskState.RUNNING
        task.pending_work_item_id = work.id
        task.pending_content_hash = work.content_hash
        self.store.save_task(task)
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

        # Cost ledger: EVERY call recorded, single authoritative pricing (the ledger
        # and the engine share one model table; compute once, in the ledger).
        cost = self.ledger.record(result)["cost_usd"]

        # Attributed/clean iff the lane actually used is a sanctioned (registered) cell.
        lane_clean = (
            result.lane_used.execution_mode,
            result.lane_used.provider,
        ) in self.registry.sanctioned()

        apply_result(task, result, now=_now(), cost_usd=cost)
        task.pending_work_item_id = None
        task.pending_content_hash = None

        outcome: str
        if result.status is ResultStatus.SUCCESS:
            task.error_signatures = []  # streak resets on a clean stage
            if is_done(task):
                task.state = TaskState.COMPLETED
                outcome = "task_completed"
            else:
                task.state = TaskState.RUNNING
                outcome = "stage_completed"
        else:
            outcome = self._handle_failure(task, result)

        # Durable per-stage log (JSON contract) + human-readable Markdown alongside.
        task.stage_counter += 1
        payload = {
            "work_item_id": result.work_item_id,
            "stage": result.stage.value,
            "task_id": result.task_id,
            "attempt": result.attempt,
            "status": result.status.value,
            "outcome": outcome,
            "model": result.model,
            "lane_used": result.lane_used.model_dump(),
            "cost_usd": cost,
            "structured_output": result.structured_output,
            "raw_output": result.raw_output,
            "error": result.error,
            "completed_at": result.completed_at,
        }
        seq = task.stage_counter
        self.store.write_stage_log(result.task_id, seq, result.stage.value, payload)
        self.store.write_stage_markdown(result.task_id, seq, result.stage.value, render_stage(payload))
        self.store.save_task(task)
        self.store.write_task_index(result.task_id, render_task_index(task))
        self.store.write_run_artifact("cost-summary.md", render_cost_summary(run_id, self.ledger.summary()))
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
                "status": result.status.value,
                "outcome": outcome,
                "lane": result.lane_used.execution_mode.value,
                "provider": result.lane_used.provider.value,
                "cost_usd": cost,
                "task_state": task.state.value,
            },
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

    def _handle_failure(self, task: Task, result: StageResult) -> str:
        failures = None
        if result.structured_output:
            failures = result.structured_output.get("failures")
        sig = error_signature(result.stage, failures=failures, error=result.error)
        task.error_signatures.append(sig)
        task.learnings.append(
            f"{result.stage.value} (attempt {result.attempt}): {result.error or 'failed'}"
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

    # --- resume / status ------------------------------------------------------
    def resume(self, run_id: str) -> dict:
        run = self.store.load_run(run_id)
        out = {}
        for ref in run.task_refs:
            task = self.store.load_task(run_id, ref.task_id)
            rp = resume_point(task)
            out[ref.task_id] = rp.value if rp else None
        return out

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
        return {
            "run_id": run_id,
            "run_state": run.state.value,
            "progress": progress.model_dump(),
            "tasks": tasks,
            "cost": self.ledger.summary(),
            "lane_audit": self.lane_audit(run_id),
        }

    def lane_audit(self, run_id: str) -> dict:
        """Every recorded model call ran on a sanctioned, attributed lane.

        Generalized beyond 3a: 'sanctioned' = the registry's served (mode, provider)
        cells, so the audit holds for headless/codex once those runners are
        registered. The failure mode it catches is a hidden/unattributed call —
        not a deliberately-selected lane (target.md §4: attribution, not abstinence)."""
        rows = self.ledger.rows()
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
    def _set_ref_state(self, run_id: str, task_id: str, state: TaskState) -> None:
        def mut(run: Run) -> None:
            for ref in run.task_refs:
                if ref.task_id == task_id:
                    ref.state = state

        self.store.update_run(run_id, mut)

    def _on_task_completed(self, run_id: str, task: Task) -> None:
        if task.pr_url:
            self.project.task_source.mark_complete(task.task_id, task.pr_url)
        self.store.append_event(
            run_id,
            {"ts": _now(), "type": "task_completed", "run_id": run_id,
             "task_id": task.task_id, "pr_url": task.pr_url},
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
