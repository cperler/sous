"""Batch scheduler (target.md §6 / plan §3b).

Drives a multi-task run over the DAG: each tick picks the dependency-satisfied,
non-terminal tasks (bounded by the engine's capacity-derived dispatch limit and
MAX_CONCURRENT), advances each by one stage via the engine, and records the
results. All the hard logic — retry-with-learnings, transitive cascade-blocking,
circuit breaker, run finalization — lives in the engine (built + tested in 3a);
the scheduler is the thin hub-and-spoke loop on top.

Dispatch is abstracted behind a ``Runner`` (a batch of WorkItems -> StageResults):
the interactive lane passes a Workflow-shim-backed runner; tests pass a simulated
one. Because all state is persisted by the engine, the scheduler is resumable —
constructing a fresh Scheduler on the same run directory continues where a killed
batch left off.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .engine import Engine
from .errors import CapacityExhausted, ContractError
from .schemas.enums import TERMINAL_TASK_STATES
from .schemas.work import StageResult, WorkItem

Runner = Callable[[list[WorkItem]], list[StageResult]]


class Scheduler:
    def __init__(
        self, engine: Engine, *, max_concurrent: int = 3, batch_failure_threshold: int = 3
    ) -> None:
        self.engine = engine
        self.max_concurrent = max_concurrent
        # Batch-wide circuit breaker (#58, ports batch-orchestrator.sh:784-811): after
        # this many CONSECUTIVE task failures (no completion in between) the run is
        # PAUSED — a systemic cause (broken env, bad base branch) must not burn every
        # task's full retry budget. 0 disables.
        self.batch_failure_threshold = batch_failure_threshold

    def dispatchable(self, run_id: str) -> list[str]:
        """Non-terminal, dependency-satisfied, unleased tasks.

        Delegates to the engine's single eligibility predicate — the scheduler stays a
        thin loop and there is one source of truth for "what's dispatchable" (fixes the
        prior Engine.ready / Scheduler.dispatchable divergence).
        """
        return self.engine.dispatchable(run_id)

    def tick(self, run_id: str, runner: Runner, *, util_pct: float = 0.0) -> dict:
        """Advance up to `dispatch_limit` ready tasks by one stage each."""
        ready = self.dispatchable(run_id)
        limit = self.engine.capacity.dispatch_limit(util_pct, self.max_concurrent)
        selected = ready[:limit]

        work: list[WorkItem] = []
        for task_id in selected:
            try:
                w = self.engine.next_work(run_id, task_id, util_pct=util_pct)
            except CapacityExhausted:
                break
            if w is not None:  # None => task already terminal/skipped this round
                work.append(w)

        if not work:
            return {"dispatched": 0, "recorded": 0, "ready": len(ready), "limit": limit,
                    "outcomes": []}

        results = runner(work)
        by_id = {r.work_item_id: r for r in results}
        # The runner contract is one StageResult per dispatched WorkItem. A missing
        # result would leave the task RUNNING with an outstanding dispatch and re-
        # dispatch forever — fail fast instead of silently looping.
        missing = [w.id for w in work if w.id not in by_id]
        if missing:
            raise ContractError(f"runner returned no StageResult for work item(s): {missing}")
        outcomes = [self.engine.record(run_id, by_id[w.id])["outcome"] for w in work]
        return {"dispatched": len(work), "recorded": len(work), "ready": len(ready),
                "limit": limit, "outcomes": outcomes}

    def run(
        self,
        run_id: str,
        runner: Runner,
        *,
        util_pct: float = 0.0,
        util_provider: Callable[[], float] | None = None,
        sleeper: Callable[[int], None] | None = None,
        drain_wait_s: int = 300,
        max_ticks: int = 10_000,
    ) -> dict:
        """Loop until no task is dispatchable (all terminal or capacity-stalled).

        With a ``sleeper`` (e.g. ``time.sleep``), capacity stalls and rate-limit
        cooldowns are WAITED OUT instead of ending the run — the old capacity_wait_loop
        behavior: sleep, re-probe (``util_provider`` re-reads utilization each tick),
        continue. Without one (the default), the caller owns retrying later — the
        pre-existing behavior. Returns the final engine status. Resumable: call again
        on the same run to continue after a kill.

        Batch-wide circuit breaker (#58): ``batch_failure_threshold`` consecutive task
        failures (no completion in between) PAUSE the run and stop dispatching — a
        systemic cause fails fast instead of burning every task's retry budget. A
        paused run refuses to schedule until ``orchestrator unpause``.
        """
        consecutive_failures = 0
        for _ in range(max_ticks):
            if self.engine.store.load_run(run_id).state.value == "paused":
                break  # human-gated: unpause first
            if not self.dispatchable(run_id):
                # Nothing dispatchable — but a rate-limit cooldown is a wait, not an end.
                wait = self._cooldown_wait(run_id)
                if wait is not None and sleeper is not None:
                    sleeper(wait)
                    continue
                break
            util = util_provider() if util_provider is not None else util_pct
            res = self.tick(run_id, runner, util_pct=util)
            # CONSTRAINT (#53): only a genuine EXECUTION failure may advance the breaker.
            # A human closing a task as infeasible (Engine.reject → CLOSED_INFEASIBLE) is a
            # deliberate decision, not a system failure — and it is an OUT-OF-BAND
            # transition that never runs through record()/tick(), so it produces no
            # ``outcome`` here and structurally cannot increment ``consecutive_failures``.
            # Guarded belt-and-suspenders below: only ``task_failed*`` increments, so even
            # if a close-style outcome ever reached this loop it could not trip the breaker.
            for outcome in res.get("outcomes", []):
                if outcome.startswith("task_failed"):
                    consecutive_failures += 1
                elif outcome == "task_completed":
                    consecutive_failures = 0  # real progress resets the streak
            if self.batch_failure_threshold and consecutive_failures >= self.batch_failure_threshold:
                self.engine.pause_run(
                    run_id,
                    f"batch circuit breaker: {consecutive_failures} consecutive task "
                    f"failures — check for a systemic cause (env, base branch), then "
                    f"`orchestrator unpause`",
                )
                break
            if res["dispatched"] == 0:
                # Capacity-throttled tick (limit 0). Wait out the window if we can.
                if sleeper is not None:
                    sleeper(drain_wait_s)
                    continue
                break  # caller retries later
        return self.engine.status(run_id)

    def _cooldown_wait(self, run_id: str) -> int | None:
        """Seconds until the SOONEST rate-limit cooldown among non-terminal tasks
        expires (None when no task is cooling — the run is genuinely done/stalled)."""
        run = self.engine.store.load_run(run_id)
        now = datetime.now(UTC)
        waits: list[int] = []
        for ref in run.task_refs:
            if ref.state in TERMINAL_TASK_STATES:
                continue
            doc = self.engine.store.load_task(run_id, ref.task_id)
            if not doc.not_before:
                continue
            try:
                until = datetime.fromisoformat(doc.not_before)
            except ValueError:
                continue
            if until > now:
                waits.append(int((until - now).total_seconds()) + 1)
        return min(waits) if waits else None
