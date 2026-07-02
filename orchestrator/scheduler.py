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

from .engine import Engine
from .errors import CapacityExhausted, ContractError
from .schemas.work import StageResult, WorkItem

Runner = Callable[[list[WorkItem]], list[StageResult]]


class Scheduler:
    def __init__(self, engine: Engine, *, max_concurrent: int = 3) -> None:
        self.engine = engine
        self.max_concurrent = max_concurrent

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
            return {"dispatched": 0, "recorded": 0, "ready": len(ready), "limit": limit}

        results = runner(work)
        by_id = {r.work_item_id: r for r in results}
        # The runner contract is one StageResult per dispatched WorkItem. A missing
        # result would leave the task RUNNING with an outstanding dispatch and re-
        # dispatch forever — fail fast instead of silently looping.
        missing = [w.id for w in work if w.id not in by_id]
        if missing:
            raise ContractError(f"runner returned no StageResult for work item(s): {missing}")
        for w in work:
            self.engine.record(run_id, by_id[w.id])
        return {"dispatched": len(work), "recorded": len(work), "ready": len(ready), "limit": limit}

    def run(self, run_id: str, runner: Runner, *, util_pct: float = 0.0, max_ticks: int = 10_000) -> dict:
        """Loop until no task is dispatchable (all terminal or capacity-stalled).

        Returns the final engine status. Resumable: call again on the same run to
        continue after a kill — the engine's persisted state is the source of truth.
        """
        for _ in range(max_ticks):
            if not self.dispatchable(run_id):
                break
            res = self.tick(run_id, runner, util_pct=util_pct)
            if res["dispatched"] == 0:
                break  # nothing advanced this tick (e.g. capacity 0) — caller retries later
        return self.engine.status(run_id)
