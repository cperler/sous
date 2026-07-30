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
batch left off, INCLUDING a kill that caught dispatches mid-flight: ``run()`` claims
the run's driver record and reclaims the leases its own dead driver left behind
before the first tick (#313). See ``Scheduler.run`` for the limits of that guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .alerting import NOTIFY_RUN_BLOCKED, stale_notifications
from .engine import Engine
from .errors import CapacityExhausted, ContractError
from .schemas.enums import TERMINAL_TASK_STATES
from .schemas.work import StageResult, WorkItem

Runner = Callable[[list[WorkItem]], list[StageResult]]

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
        for _ in range(max_ticks):
            if self.engine.store.load_run(run_id).state.value == "paused":
                exit_reason = EXIT_PAUSED
                break  # human-gated: unpause first
            # Stall alerting (#55): poll the liveness sensor each pass and notify ONCE
            # per task per stall episode. The shared alerting core owns the dedupe (the
            # `watch` CLI feeds it the same way), so a re-ping is impossible until the
            # task moves again and re-stalls.
            stale_sent = self._alert_stale(run_id, stale_sent, stale_after_s)
            if not self.dispatchable(run_id):
                # Nothing dispatchable — but a rate-limit cooldown is a wait, not an end.
                wait = self._cooldown_wait(run_id)
                if wait is not None and sleeper is not None:
                    sleeper(wait)
                    continue
                # #313: "nothing dispatchable" has TWO very different causes. If tasks are
                # still holding dispatch leases we could not reclaim, this loop is giving
                # up on a run that is not finished — say so instead of dumping a status
                # that looks like success.
                exit_reason = (
                    EXIT_BLOCKED_ORPHANED if self.engine.in_flight(run_id) else EXIT_DONE
                )
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
            if res["dispatched"] == 0:
                # Capacity-throttled tick (limit 0). Wait out the window if we can.
                if sleeper is not None:
                    sleeper(drain_wait_s)
                    continue
                exit_reason = EXIT_CAPACITY
                break  # caller retries later
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
