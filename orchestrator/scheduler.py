"""Batch scheduler (target.md §6 / plan §3b).

Drives a multi-task run over the DAG: each tick picks the dependency-satisfied,
non-terminal tasks (bounded by the engine's capacity-derived dispatch limit and
MAX_CONCURRENT), advances each by one stage via the engine, and records the
results. All the hard logic — retry-with-learnings, transitive cascade-blocking,
circuit breaker, run finalization — lives in the engine (built + tested in 3a);
the scheduler is the thin hub-and-spoke loop on top.

Dispatch is abstracted behind a runner. Two forms are accepted (#318): the original
``Runner`` (a batch of WorkItems -> StageResults, list-in/list-out) and the wider
``StreamingRunner`` protocol (submit work, harvest results AS THEY COMPLETE). The list
form is a strict barrier — it cannot express partial harvest, so a stage that finished
early sat completed-but-unrecorded until the slowest member of its batch landed (measured
at 8-9 minutes of dead wall-clock per wave on ``batch-headless-2``), holding its lease and
therefore its concurrency slot. A list runner is adapted into the streaming protocol by
``as_streaming`` and keeps its old batch-shaped behavior exactly; a real streaming pool
(``adapters.execution.runners.RegistryPool``) lets ``run`` record each result the moment it
lands and refill the freed slot while its siblings are still in flight.

The interactive lane passes a Workflow-shim-backed list runner; tests pass a simulated
one. Because all state is persisted by the engine, the scheduler is resumable —
constructing a fresh Scheduler on the same run directory continues where a killed
batch left off, INCLUDING a kill that caught dispatches mid-flight: ``run()`` claims
the run's driver record and reclaims the leases its own dead driver left behind
before the first tick (#313). See ``Scheduler.run`` for the limits of that guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from .alerting import NOTIFY_RUN_BLOCKED, stale_notifications
from .engine import Engine
from .errors import CapacityExhausted, ContractError
from .schemas.enums import TERMINAL_TASK_STATES
from .schemas.work import StageResult, WorkItem

Runner = Callable[[list[WorkItem]], list[StageResult]]


@runtime_checkable
class StreamingRunner(Protocol):
    """A dispatch pool: work goes in incrementally, results come out AS THEY COMPLETE.

    The wider contract the scheduler drives (#318). The narrow ``Runner`` (list-in/
    list-out) cannot express partial harvest at all, so every result of a batch waited on
    the batch's slowest member before ANY of them could be recorded.

    Contract:
      * ``submit`` accepts work at any time, including while earlier work is still in
        flight, and must not block on completion (a list-runner adapter is the exception:
        it necessarily blocks, which is exactly the old behavior).
      * ``harvest(block=True)`` returns every result available now, waiting for at least
        one when work is outstanding. It MUST NOT return empty while ``pending()`` is
        non-zero and more results are still coming — the scheduler would spin.
      * ``pending()`` counts submitted work whose result has not yet been handed back by
        ``harvest``. It may reach zero WITHOUT delivering a result (a runner that dropped
        one); the scheduler detects exactly that and raises ``ContractError`` rather than
        waiting forever.
      * ``close()`` is idempotent, and a pool may be reused after it.
    """

    def submit(self, work: list[WorkItem]) -> None: ...

    def harvest(self, *, block: bool = True) -> list[StageResult]: ...

    def pending(self) -> int: ...

    def close(self) -> None: ...


AnyRunner = Runner | StreamingRunner


class ListRunnerPool:
    """A plain list-in/list-out ``Runner`` seen through the streaming protocol.

    Preserves the batch semantics exactly — ``submit`` runs the whole batch synchronously
    and buffers its results — so the interactive Workflow shim and every list-shaped test
    fake behave as they always have. Only the *recording* changes: the scheduler hands each
    buffered result to the engine one at a time instead of joining them into one batch.
    """

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._buffer: list[StageResult] = []

    def submit(self, work: list[WorkItem]) -> None:
        self._buffer.extend(self._runner(work))

    def harvest(self, *, block: bool = True) -> list[StageResult]:
        out, self._buffer = self._buffer, []
        return out

    def pending(self) -> int:
        return len(self._buffer)

    def close(self) -> None:
        return None


def as_streaming(runner: AnyRunner) -> StreamingRunner:
    """The runner as a dispatch pool — itself when it already streams, wrapped otherwise.

    The isinstance check is structural (``runtime_checkable`` Protocol): a bare callable
    lane runner has no ``submit``/``harvest`` and gets the adapter.
    """
    if isinstance(runner, StreamingRunner):
        return runner
    return ListRunnerPool(runner)

# Why ``Scheduler.run`` stopped looping (#313). Reported under the returned status'
# ``scheduler`` block so the end-of-run dump is never ambiguous — the silent failure this
# vocabulary exists for was "nothing dispatchable because every task holds an orphaned
# lease", whose output was byte-indistinguishable from an ordinary finished run.
EXIT_DONE = "nothing_dispatchable"  # nothing left to do (finished, or blocked on a human)
EXIT_BLOCKED_ORPHANED = "blocked_on_orphaned_dispatches"  # leases held, none reclaimable
EXIT_PAUSED = "run_paused"  # the run doc says paused (human gate / budget)
EXIT_BREAKER = "circuit_breaker"  # this loop tripped the batch breaker and paused the run
EXIT_CAPACITY = "capacity_stalled"  # dispatch limit 0 and no sleeper to wait it out
EXIT_MAX_TICKS = "max_ticks"  # the loop bound was hit — a real run should never see this


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

    def tick(self, run_id: str, runner: AnyRunner, *, util_pct: float = 0.0) -> dict:
        """Advance up to `dispatch_limit` ready tasks by one stage each.

        One dispatch batch, drained before returning — but each result is RECORDED THE
        MOMENT IT LANDS (#318) rather than after the whole batch joins. The single-batch
        shape is kept for direct callers (the per-tick supervisor loop in tests); ``run``
        drives the same primitives with mid-flight refill on top.
        """
        pool = as_streaming(runner)
        outstanding: dict[str, WorkItem] = {}
        try:
            step = self._dispatch(run_id, pool, outstanding, util_pct=util_pct)
            outcomes: list[str] = []
            while outstanding:
                outcomes += self._harvest(run_id, pool, outstanding)
            return {"dispatched": step["dispatched"], "recorded": len(outcomes),
                    "ready": step["ready"], "limit": step["limit"], "outcomes": outcomes}
        finally:
            pool.close()

    def _dispatch(
        self,
        run_id: str,
        pool: StreamingRunner,
        outstanding: dict[str, WorkItem],
        *,
        util_pct: float,
    ) -> dict:
        """Fill the free concurrency slots with dependency-ready work, leases and all.

        ``outstanding`` (submitted-but-unharvested work, keyed by work item id) is OURS to
        maintain: it is subtracted from the dispatch limit, because the cap is on
        CONCURRENT dispatches and mid-flight refill would otherwise silently exceed it.
        (``dispatchable`` already excludes leased tasks, so the in-flight ones are never
        re-selected — but they must still count against the cap.)
        """
        ready = self.dispatchable(run_id)
        limit = self.engine.capacity.dispatch_limit(util_pct, self.max_concurrent)
        selected = ready[: max(0, limit - len(outstanding))]

        work: list[WorkItem] = []
        for task_id in selected:
            try:
                w = self.engine.next_work(run_id, task_id, util_pct=util_pct)
            except CapacityExhausted:
                break
            if w is not None:  # None => task already terminal/skipped this round
                work.append(w)

        if work:
            pool.submit(work)
            outstanding.update({w.id: w for w in work})
        return {"dispatched": len(work), "ready": len(ready), "limit": limit}

    def _harvest(
        self, run_id: str, pool: StreamingRunner, outstanding: dict[str, WorkItem]
    ) -> list[str]:
        """Record every result available now (blocking for at least one), and return their
        outcomes in COMPLETION order.

        Recording per result — not per batch — is the whole point of #318: it releases that
        task's lease immediately, so it can advance and its slot can refill while its
        siblings are still running.
        """
        outcomes: list[str] = []
        for result in pool.harvest(block=True):
            if outstanding.pop(result.work_item_id, None) is None:
                # Recording is irreversible, so an unrecognized (or already-harvested)
                # result is refused before it reaches the engine.
                raise ContractError(
                    "runner returned a StageResult for an unknown or already-recorded "
                    f"work item: {result.work_item_id}"
                )
            outcomes.append(self.engine.record(run_id, result)["outcome"])
        # The runner contract is one StageResult per dispatched WorkItem. A missing
        # result would leave the task RUNNING with an outstanding dispatch and re-
        # dispatch forever — fail fast instead of silently looping. Under as-completed
        # harvesting "missing" means the pool has drained without delivering it, which is
        # also what keeps a dropped result from parking the loop in a forever-wait.
        if outstanding and pool.pending() == 0:
            raise ContractError(
                f"runner returned no StageResult for work item(s): {sorted(outstanding)}"
            )
        return outcomes

    def _drain(
        self, run_id: str, pool: StreamingRunner, outstanding: dict[str, WorkItem]
    ) -> None:
        """Record everything still in flight before leaving the loop, so an exit for any
        reason (paused, breaker, capacity) still banks the stages that already finished
        instead of abandoning their leases for the next driver to reclaim."""
        while outstanding:
            self._harvest(run_id, pool, outstanding)

    def run(
        self,
        run_id: str,
        runner: AnyRunner,
        *,
        util_pct: float = 0.0,
        util_provider: Callable[[], float] | None = None,
        sleeper: Callable[[int], None] | None = None,
        drain_wait_s: int = 300,
        stale_after_s: int = 1800,
        max_ticks: int = 10_000,
    ) -> dict:
        """Loop until no task is dispatchable (all terminal or capacity-stalled).

        FOREGROUND: this call owns the run for its whole duration — it dispatches
        synchronously and, on the headless lane, the provider processes are its children.
        Killing it (Ctrl-C) kills them. Monitor from a SEPARATE terminal (``orchestrator
        watch``).

        With a ``sleeper`` (e.g. ``time.sleep``), capacity stalls and rate-limit
        cooldowns are WAITED OUT instead of ending the run — the old capacity_wait_loop
        behavior: sleep, re-probe (``util_provider`` re-reads utilization each tick),
        continue. Without one (the default), the caller owns retrying later — the
        pre-existing behavior.

        Resumable after a kill (#313), with a stated limit. At startup this claims the
        run's driver record and reclaims the dispatch leases left by a driver that is now
        gone, re-dispatching each at the SAME attempt (no retry budget spent). It reclaims
        ONLY leases whose owner is provably this process or a dead process on this host —
        an unclaimed run (one driven task-by-task through the CLI supervisor, whose
        background invocations hold live leases), a live driver, or a foreign-host claim
        reclaims nothing, because stealing a live lease would double-dispatch the stage.
        In that case the loop does not pretend to be finished: it returns
        ``scheduler.exit_reason == "blocked_on_orphaned_dispatches"`` (``run-headless``
        exits non-zero), naming the tasks and pointing at ``orchestrator abandon``.

        Batch-wide circuit breaker (#58): ``batch_failure_threshold`` consecutive task
        failures (no completion in between) PAUSE the run and stop dispatching — a
        systemic cause fails fast instead of burning every task's retry budget. A
        paused run refuses to schedule until ``orchestrator unpause``.

        Returns the final engine status with an added ``scheduler`` block: why the loop
        stopped (``exit_reason``, one of the ``EXIT_*`` constants), what was reclaimed at
        startup, and any leases still outstanding.
        """
        consecutive_failures = 0
        stale_sent: set[str] = set()
        # #5: reclaim any port blocks left behind by crashed/terminal runs before we start
        # dispatching, so this batch doesn't starve on ports a dead run never released.
        # Best-effort + opt-in (a no-op for projects without port needs).
        self.engine.reclaim_stale_ports(run_id)
        # #313: free the dispatch leases OUR dead driver left behind, THEN stamp this
        # process as the driver. Order matters: the reclaim classifies the claim that is
        # on disk (the killed driver's), so claiming first would erase the very evidence
        # that proves those leases are orphans.
        reclaim = self.engine.reclaim_orphaned_dispatches(run_id)
        self.engine.claim_run_driver(run_id)
        exit_reason = EXIT_MAX_TICKS
        # #318: ONE pool for the whole loop, and one ``outstanding`` map alongside it, so a
        # freed slot is refilled while its siblings are still running. With a list runner
        # the pool is the batch-shaped adapter and this degrades to the old behavior.
        pool = as_streaming(runner)
        outstanding: dict[str, WorkItem] = {}
        try:
            for _ in range(max_ticks):
                if self.engine.store.load_run(run_id).state.value == "paused":
                    exit_reason = EXIT_PAUSED
                    break  # human-gated: unpause first
                # Stall alerting (#55): poll the liveness sensor each pass and notify ONCE
                # per task per stall episode. The shared alerting core owns the dedupe (the
                # `watch` CLI feeds it the same way), so a re-ping is impossible until the
                # task moves again and re-stalls.
                stale_sent = self._alert_stale(run_id, stale_sent, stale_after_s)
                if not self.dispatchable(run_id) and not outstanding:
                    # Nothing dispatchable and nothing of ours in flight — but a rate-limit
                    # cooldown is a wait, not an end.
                    wait = self._cooldown_wait(run_id)
                    if wait is not None and sleeper is not None:
                        sleeper(wait)
                        continue
                    # #313: "nothing dispatchable" has TWO very different causes. If tasks
                    # are still holding dispatch leases we could not reclaim, this loop is
                    # giving up on a run that is not finished — say so instead of dumping a
                    # status that looks like success. (Our OWN in-flight work is excluded
                    # above, so it can never be mistaken for an orphaned lease.)
                    exit_reason = (
                        EXIT_BLOCKED_ORPHANED if self.engine.in_flight(run_id) else EXIT_DONE
                    )
                    break
                util = util_provider() if util_provider is not None else util_pct
                res = self._dispatch(run_id, pool, outstanding, util_pct=util)
                outcomes = self._harvest(run_id, pool, outstanding) if outstanding else []
                # CONSTRAINT (#53): only a genuine EXECUTION failure may advance the breaker.
                # A human closing a task as infeasible (Engine.reject → CLOSED_INFEASIBLE) is
                # a deliberate decision, not a system failure — and it is an OUT-OF-BAND
                # transition that never runs through record(), so it produces no ``outcome``
                # here and structurally cannot increment ``consecutive_failures``. Guarded
                # belt-and-suspenders below: only ``task_failed*`` increments, so even if a
                # close-style outcome ever reached this loop it could not trip the breaker.
                for outcome in outcomes:
                    if outcome.startswith("task_failed"):
                        consecutive_failures += 1
                    elif outcome == "task_completed":
                        consecutive_failures = 0  # real progress resets the streak
                if (
                    self.batch_failure_threshold
                    and consecutive_failures >= self.batch_failure_threshold
                ):
                    reason = (
                        f"batch circuit breaker: {consecutive_failures} consecutive task "
                        f"failures — check for a systemic cause (env, base branch), then "
                        f"`orchestrator unpause`"
                    )
                    self.engine.pause_run(run_id, reason)
                    # Alerting (#55): the breaker pause is a scheduler-layer event (no engine
                    # transition to hang it on), so it emits here — a paused batch is exactly
                    # the unattended stall a human needs told about.
                    self.engine.emit_notification(
                        run_id, "run_paused",
                        {"run_id": run_id, "kind": "run_paused", "reason": reason,
                         "summary": f"run {run_id} PAUSED by the batch circuit breaker "
                                    f"({consecutive_failures} consecutive failures)"},
                    )
                    exit_reason = EXIT_BREAKER
                    break
                if res["dispatched"] == 0 and not outcomes and not outstanding:
                    # Capacity-throttled tick (limit 0). Wait out the window if we can.
                    # Only a stall when the pass did NOTHING: no dispatch, no result
                    # recorded, nothing of ours still running. Under as-completed
                    # harvesting a pass that only drains in-flight work is progress (the
                    # last stage of a run is exactly that pass), not a throttle.
                    if sleeper is not None:
                        sleeper(drain_wait_s)
                        continue
                    exit_reason = EXIT_CAPACITY
                    break  # caller retries later
            # Bank anything still in flight (a paused/breaker/capacity exit may leave work
            # out) so no finished stage is dropped on the way out.
            self._drain(run_id, pool, outstanding)
        finally:
            pool.close()
        return self._final_status(run_id, exit_reason, reclaim)

    def _final_status(self, run_id: str, exit_reason: str, reclaim: dict) -> dict:
        """The end-of-run status dump, annotated with WHY the loop stopped (#313).

        The blocked case is also emitted (a warning-grade ``scheduler_exit_blocked`` event
        plus a notification), because the operator who needs to know is the one who is no
        longer watching this terminal.
        """
        in_flight = self.engine.in_flight(run_id)
        block = {
            "exit_reason": exit_reason,
            "reclaimed": reclaim.get("reclaimed", []),
            "reclaim_skipped": reclaim.get("skipped", []),
            # The claim as FOUND at startup — i.e. whose leases the reclaim was judging.
            # (The current owner is the status' own top-level ``driver``: normally us.)
            "driver_at_start": reclaim.get("driver"),
            "in_flight": in_flight,
        }
        if exit_reason == EXIT_BLOCKED_ORPHANED:
            driver = reclaim.get("driver") or {}
            block["message"] = (
                f"stopped with nothing dispatchable while task(s) {', '.join(in_flight)} "
                f"still hold a dispatch lease this driver may not reclaim (driver claim: "
                f"{driver.get('state')}). A live driver elsewhere may be finishing them; "
                f"otherwise release each with `orchestrator abandon --run {run_id} --task "
                f"<id> --reason ...` (terminal) once you know the process is dead."
            )
            self.engine.store.append_event(
                run_id,
                {"ts": datetime.now(UTC).isoformat(), "type": "scheduler_exit_blocked",
                 "severity": "warning",
                 "run_id": run_id, "exit_reason": exit_reason, "in_flight": in_flight,
                 "driver": driver, "message": block["message"]},
            )
            self.engine.emit_notification(
                run_id, NOTIFY_RUN_BLOCKED,
                {"run_id": run_id, "kind": NOTIFY_RUN_BLOCKED,
                 "summary": f"run {run_id} scheduler stopped: {block['message']}",
                 "in_flight": in_flight, "driver": driver},
            )
        status = self.engine.status(run_id)
        status["scheduler"] = block
        return status

    def _alert_stale(self, run_id: str, sent: set[str], stale_after_s: int) -> set[str]:
        """Poll the liveness sensor and fire any newly-due stall notifications, returning
        the updated dedupe set. Shares the pure ``stale_notifications`` core with the
        ``watch`` CLI so both apply identical once-per-episode semantics."""
        status = self.engine.status(run_id, stale_after_s=stale_after_s)
        notes, sent = stale_notifications(status, sent)
        for note in notes:
            self.engine.emit_notification(run_id, note["kind"], note)
        return sent

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
