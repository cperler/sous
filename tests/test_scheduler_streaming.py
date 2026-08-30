"""As-completed harvesting (#318): a finished stage is recorded on its OWN completion.

The defect these pin: ``Scheduler.tick`` dispatched a batch and waited for EVERY member
before recording ANY of them, so on ``batch-headless-2`` two stages that finished at ~17:24
were not recorded until 17:33 — holding their leases, and therefore their concurrency slots,
for 8-9 minutes of dead wall-clock. The list-in/list-out ``Runner`` signature could not even
express a partial answer, so the fix is a wider (streaming) runner contract plus a scheduler
loop that records each result as it lands and refills the freed slot mid-flight.
"""

from __future__ import annotations

import json
import threading

import pytest

from adapters.execution.base import Registry
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.runners import registry_runner
from adapters.execution.transport import RawResult
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.scheduler import ListRunnerPool, Scheduler, as_streaming
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, StageResult, TokenUsage, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, _default_output, make_result

POLICY_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _engine(tmp_path, **kw) -> Engine:
    return Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject(), **kw
    )


def _three_task_run(eng: Engine) -> None:
    """Three INDEPENDENT tasks — the DAG must not be what serializes them."""
    eng.create_run("r1", ExecutionLane.FULL)
    for task_id in ("A", "B", "C"):
        eng.add_task("r1", task_id)


def _records(tmp_path) -> list[tuple[str, str]]:
    """(task_id, recorded-at) for every ``stage_recorded``, in append order."""
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [
        (e["task_id"], e["ts"])
        for e in (json.loads(line) for line in path.read_text().splitlines())
        if e.get("type") == "stage_recorded"
    ]


class ScriptedPool:
    """Streaming runner with a SCRIPTED completion order — no threads, no clock.

    Delivers exactly one result per ``harvest``: the earliest task still named in ``order``
    that is currently in flight, FIFO once the script is exhausted. ``on_deliver`` is called
    at delivery time, i.e. BEFORE the scheduler has recorded that result — which is how the
    tests observe what had already been recorded when each stage completed.
    """

    def __init__(self, order: list[str], on_deliver=None) -> None:
        self.order = list(order)
        self.on_deliver = on_deliver
        self.queued: list[WorkItem] = []
        self.delivered: list[str] = []
        self.max_in_flight = 0
        self.closed = 0

    def submit(self, work: list[WorkItem]) -> None:
        self.queued.extend(work)
        self.max_in_flight = max(self.max_in_flight, len(self.queued))

    def pending(self) -> int:
        return len(self.queued)

    def harvest(self, *, block: bool = True) -> list[StageResult]:
        if not self.queued:
            return []
        idx = 0
        if self.order:
            match = [i for i, w in enumerate(self.queued) if w.task_id == self.order[0]]
            if match:
                idx = match[0]
                self.order.pop(0)
        work = self.queued.pop(idx)
        self.delivered.append(work.task_id)
        if self.on_deliver is not None:
            self.on_deliver(work)
        return [make_result(work)]

    def close(self) -> None:
        self.closed += 1


class HoldingPool:
    """Streaming runner that never delivers ``hold``'s result while anything else can move.

    Models the pathological sibling: one stage that runs far longer than its batch-mates.
    Records who was submitted while the held item was still in flight, which is the
    observable for "a freed slot refilled mid-flight".
    """

    def __init__(self, hold: str) -> None:
        self.hold = hold
        self.queued: list[WorkItem] = []
        self.refills: list[tuple[str, tuple[str, ...]]] = []  # (submitted, in-flight then)
        self.max_in_flight = 0
        self._released = False

    def submit(self, work: list[WorkItem]) -> None:
        in_flight = tuple(w.task_id for w in self.queued)
        if in_flight:
            self.refills += [(w.task_id, in_flight) for w in work]
        self.queued.extend(work)
        self.max_in_flight = max(self.max_in_flight, len(self.queued))

    def pending(self) -> int:
        return len(self.queued)

    def harvest(self, *, block: bool = True) -> list[StageResult]:
        for i, work in enumerate(self.queued):
            if work.task_id == self.hold and not self._released:
                continue
            return [make_result(self.queued.pop(i))]
        # Nothing else left to move — let the long stage finish so the run can end.
        self._released = True
        return [make_result(self.queued.pop(0))] if self.queued else []

    def close(self) -> None:
        return None


class DroppingPool:
    """Streaming runner that accepts work and never answers for it (a dropped result)."""

    def submit(self, work: list[WorkItem]) -> None:
        return None

    def pending(self) -> int:
        return 0

    def harvest(self, *, block: bool = True) -> list[StageResult]:
        return []

    def close(self) -> None:
        return None


# --- as-completed recording ------------------------------------------------------
def test_early_finisher_is_recorded_before_its_slower_siblings(tmp_path) -> None:
    """Completion order, not submission order, drives recording — and each result is
    recorded on its own completion rather than at the batch barrier."""
    eng = _engine(tmp_path)
    _three_task_run(eng)
    seen: list[tuple[str, tuple[str, ...]]] = []
    # The batch is dispatched A, B, C; the THIRD submitted finishes first.
    pool = ScriptedPool(
        ["C", "B", "A"],
        on_deliver=lambda w: seen.append(
            (w.task_id, tuple(t for t, _ts in _records(tmp_path)))
        ),
    )
    status = Scheduler(eng, max_concurrent=3).run("r1", pool)

    recorded = _records(tmp_path)
    # Wave 1 recorded in completion order, NOT submission order.
    assert [t for t, _ts in recorded][:3] == ["C", "B", "A"]
    # ...and each landed on its own completion: when C finished nothing was recorded yet;
    # when B finished only C was; when A (the slow one) finished, C and B already were.
    assert seen[:3] == [("C", ()), ("B", ("C",)), ("A", ("C", "B"))]
    # Recorded-at timestamps agree with completion order (the barrier stamped them together).
    stamps = [ts for _t, ts in recorded][:3]
    assert stamps == sorted(stamps)
    assert stamps[0] <= stamps[-1]

    # Completion ORDER does not change the final DAG state or the work done.
    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("A", "B", "C"))
    assert status["lane_audit"]["total_calls"] == 21  # 3 tasks x 7 stages, none re-run
    assert pool.max_in_flight <= 3  # the concurrency cap still binds


def test_freed_slot_is_refilled_while_a_sibling_is_still_in_flight(tmp_path) -> None:
    """B's stage never finishes; A drains and completes, and C — the next
    dependency-ready task — starts while B is STILL in flight."""
    eng = _engine(tmp_path)
    _three_task_run(eng)
    pool = HoldingPool(hold="B")
    status = Scheduler(eng, max_concurrent=2).run("r1", pool)

    # Wave 1 fills both slots with A and B; every later dispatch is a mid-flight refill.
    assert ("A", ("B",)) in pool.refills  # A's next stage, dispatched while B hangs
    assert ("C", ("B",)) in pool.refills  # the queued task got the slot A freed
    # A and C were both dispatched (and completed) before B's single stage ever landed.
    assert pool.max_in_flight <= 2  # in-flight work still counts against max_concurrent
    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("A", "B", "C"))


# --- contract fail-fasts ---------------------------------------------------------
def test_dropped_result_raises_instead_of_waiting_forever(tmp_path) -> None:
    """Both runner forms: a work item that never yields a result must raise, not hang."""
    eng = _engine(tmp_path)
    _three_task_run(eng)
    with pytest.raises(ContractError) as exc:
        Scheduler(eng, max_concurrent=1).tick("r1", lambda work: [])  # list form
    assert "no StageResult" in str(exc.value)

    eng2 = _engine(tmp_path / "two")
    _three_task_run(eng2)
    with pytest.raises(ContractError) as exc2:
        Scheduler(eng2, max_concurrent=1).tick("r1", DroppingPool())  # streaming form
    assert "no StageResult" in str(exc2.value)


def test_result_for_an_unrecognized_work_item_is_refused(tmp_path) -> None:
    """Recording is irreversible, so a result we never dispatched never reaches the engine."""
    eng = _engine(tmp_path)
    _three_task_run(eng)

    def stray(work: list[WorkItem]) -> list[StageResult]:
        return [make_result(w).model_copy(update={"work_item_id": "wi-bogus"}) for w in work]

    with pytest.raises(ContractError) as exc:
        Scheduler(eng, max_concurrent=1).tick("r1", stray)
    assert "wi-bogus" in str(exc.value)


# --- the narrow (list) runner form is untouched -----------------------------------
def test_plain_list_runner_still_drives_a_full_batch(tmp_path) -> None:
    """The interactive shim and every list-shaped test fake keep working unchanged."""
    eng = _engine(tmp_path)
    _three_task_run(eng)
    seen: list[str] = []

    def list_runner(work: list[WorkItem]) -> list[StageResult]:
        seen.extend(w.task_id for w in work)
        return [make_result(w) for w in work]

    status = Scheduler(eng, max_concurrent=3).run("r1", list_runner)

    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("A", "B", "C"))
    assert status["lane_audit"]["total_calls"] == 21
    assert seen  # it really was the list runner that did the work


def test_as_streaming_wraps_only_the_narrow_form() -> None:
    pool = ScriptedPool([])
    assert as_streaming(pool) is pool  # already streams — passed through untouched
    assert isinstance(as_streaming(lambda work: []), ListRunnerPool)


# --- the registry pool harvests out of submission order ---------------------------
def _wi(task_id: str) -> WorkItem:
    return WorkItem.create(
        id=f"wi-{task_id}", run_id="r1", task_id=task_id, stage=Stage.IMPLEMENT, prompt="p",
        schema_ref="implement", model="claude-opus-5", lane_policy=POLICY_HEADLESS,
        created_at="2026-07-30T00:00:00Z",
    )


def test_registry_pool_yields_the_fast_dispatch_before_the_slow_one() -> None:
    """The adapter half of the fix: ``[f.result() for f in futures]`` collected in
    SUBMISSION order, so a finished future behind a slow one was not even observed."""
    gate = threading.Event()

    def transport(work: WorkItem) -> RawResult:
        if work.task_id == "slow":
            assert gate.wait(timeout=10), "gate never opened"
        return RawResult(
            structured_output=_default_output(work.stage),
            usage=TokenUsage(input=1, output=1),
            invocation="fake",
        )

    registry = Registry()
    registry.register_runner(HeadlessClaudeRunner(transport))
    pool = registry_runner(registry)
    try:
        pool.submit([_wi("slow"), _wi("fast")])  # slow submitted FIRST
        assert [r.task_id for r in pool.harvest(block=True)] == ["fast"]
        assert pool.pending() == 1
        gate.set()
        assert [r.task_id for r in pool.harvest(block=True)] == ["slow"]
        assert pool.pending() == 0
    finally:
        gate.set()
        pool.close()


def test_registry_pool_is_reusable_after_close() -> None:
    """``run``/``tick`` close the pool they were handed on every exit path, so closing must
    not poison a pool the caller may drive again."""
    registry = Registry()
    registry.register_runner(
        HeadlessClaudeRunner(
            lambda w: RawResult(structured_output=_default_output(w.stage), invocation="fake")
        )
    )
    pool = registry_runner(registry)
    try:
        assert [r.task_id for r in pool([_wi("t1")])] == ["t1"]
        pool.close()
        pool.close()  # idempotent
        assert [r.task_id for r in pool([_wi("t2")])] == ["t2"]
    finally:
        pool.close()
