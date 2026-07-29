"""Crash-idempotency of ``Engine.record`` (#277).

``record()``'s persistence sequence is: ledger row (idempotent on
``(work_item_id, phase)``) -> per-stage log/markdown (atomic overwrites) ->
audit-event batch (``stage_recorded`` last) -> task doc (the single durable
commit point, via ``commit_task_events``) -> derived artifacts (task index,
run-doc ref state, progress).

These tests inject a failure at each persistence boundary and assert the run
RECOVERS: replaying the same StageResult converges — at most one ledger row per
model call, the lease cleared exactly once, exactly one ``stage_recorded`` for
the dispatch — instead of double-counting (the defect: a crash after the ledger
append but before the task commit left the lease active, and the replay charged
the same model call again).
"""

from __future__ import annotations

import json
import threading

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


class Boom(RuntimeError):
    """The injected crash."""


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _implement_dispatch(eng: Engine):
    """Drive a fresh task to an outstanding IMPLEMENT dispatch and return the WorkItem."""
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    work = eng.next_work("r1", "t1")
    assert work is not None and work.stage is Stage.IMPLEMENT
    return work


def _raise_once(monkeypatch, obj, name: str, *, only_type: str | None = None) -> None:
    """Patch ``obj.name`` to raise Boom on its first (matching) call, then pass through."""
    orig = getattr(obj, name)
    state = {"armed": True}

    def wrapper(*args, **kwargs):
        if state["armed"] and (
            only_type is None
            or any(isinstance(a, dict) and a.get("type") == only_type for a in args)
        ):
            state["armed"] = False
            raise Boom(name)
        return orig(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapper)


def _assert_converged(eng: Engine, work) -> None:
    """The #277 acceptance shape: one ledger row, one stage_recorded, lease cleared."""
    rows = [r for r in eng.ledger.rows() if r["work_item_id"] == work.id]
    assert len(rows) == 1  # the model call is charged exactly once
    recorded = [
        e for e in eng.store.read_events("r1")
        if e["type"] == "stage_recorded" and e.get("work_item_id") == work.id
    ]
    assert len(recorded) == 1  # the audit trail converged too
    task = eng.store.load_task("r1", "t1")
    assert task.pending_work_item_id is None  # the lease was cleared exactly once
    assert task.stages[Stage.IMPLEMENT].status.value == "completed"


# --- crashes BEFORE the task-doc commit: the lease survives, the replay converges ---


@pytest.mark.parametrize("boundary", ["write_stage_log", "write_stage_markdown"])
def test_crash_at_stage_artifact_write_replay_converges(
    tmp_path, project, monkeypatch, boundary
) -> None:
    """A crash while writing the per-stage artifacts (after the ledger append) leaves
    the lease held; replaying the same result converges instead of double-charging."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    _raise_once(monkeypatch, eng.store, boundary)
    with pytest.raises(Boom):
        eng.record("r1", result)
    # The partial commit the issue describes: charged, but the transition never landed.
    assert len([r for r in eng.ledger.rows() if r["work_item_id"] == work.id]) == 1
    assert eng.store.load_task("r1", "t1").pending_work_item_id == work.id

    eng.record("r1", result)  # replay after restart
    _assert_converged(eng, work)
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.TEST  # the pipeline moves on


def test_crash_at_stage_recorded_append_replay_converges(
    tmp_path, project, monkeypatch
) -> None:
    """A crash inside the event-batch append (before the task doc) is recoverable: the
    replay appends the batch once and commits the transition."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    _raise_once(monkeypatch, eng.store, "append_event", only_type="stage_recorded")
    with pytest.raises(Boom):
        eng.record("r1", result)
    assert eng.store.load_task("r1", "t1").pending_work_item_id == work.id

    eng.record("r1", result)
    _assert_converged(eng, work)


def test_crash_at_task_doc_write_after_ledger_and_events_replay_converges(
    tmp_path, project, monkeypatch
) -> None:
    """THE #277 reproduction: crash at the task-doc write, after both the ledger row and
    the ``stage_recorded`` event landed. The lease is still held, so the replay is
    accepted — and it must converge (no second ledger row, no second stage_recorded)
    rather than duplicate, which is exactly what the pre-fix sequence did."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    _raise_once(monkeypatch, eng.store, "_write_task")
    with pytest.raises(Boom):
        eng.record("r1", result)
    # The observed post-crash state from the issue: one ledger row, lease still active.
    assert len([r for r in eng.ledger.rows() if r["work_item_id"] == work.id]) == 1
    assert eng.store.load_task("r1", "t1").pending_work_item_id == work.id

    eng.record("r1", result)  # the replay the issue showed double-counting
    _assert_converged(eng, work)
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.TEST


def test_crash_mid_ledger_append_leaving_torn_line_replay_converges(
    tmp_path, project, monkeypatch
) -> None:
    """Failure injection AT the ledger-append boundary itself (#277 fix cycle 1): the
    interrupted write (crash/ENOSPC) leaves a torn trailing line. Before the fix the
    replay did not converge — it WEDGED: every later record() for the run raised
    ``JSONDecodeError`` scanning the file. Now the locked scan truncates the tear and
    the replay re-appends the lost row."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    orig = CostLedger.record_rows
    state = {"armed": True}

    def torn_append(self, res, **kw):
        rows = orig(self, res, **kw)
        if state["armed"]:
            state["armed"] = False
            # Rewind the just-appended final line to a partial write and crash — the
            # on-disk shape an interruption mid-``fh.write`` leaves behind.
            *keep, last = self.path.read_bytes().splitlines(keepends=True)
            self.path.write_bytes(b"".join(keep) + last[: len(last) // 2])
            raise Boom("ledger append torn")
        return rows

    monkeypatch.setattr(CostLedger, "record_rows", torn_append)
    with pytest.raises(Boom):
        eng.record("r1", result)
    assert eng.store.load_task("r1", "t1").pending_work_item_id == work.id

    eng.record("r1", result)  # replay after restart
    _assert_converged(eng, work)
    # The tear was physically repaired: every ledger line decodes again.
    raw = eng.ledger.path.read_text()
    assert raw.endswith("\n")
    assert len([json.loads(line) for line in raw.splitlines()]) == 3  # intake+scope+implement
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.TEST


# --- crashes AFTER the commit: derived artifacts only; the replay is a stale replay ---


@pytest.mark.parametrize("boundary", ["write_task_index", "update_run"])
def test_crash_after_commit_replay_rejected_without_duplicates(
    tmp_path, project, monkeypatch, boundary
) -> None:
    """A crash in the post-commit derived writes (task index / run-doc ref state) leaves
    the transition durable: the replay gets the ordinary ContractError replay rejection
    and nothing is double-counted."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    _raise_once(monkeypatch, eng.store, boundary)
    with pytest.raises(Boom):
        eng.record("r1", result)
    _assert_converged(eng, work)  # the commit itself landed

    with pytest.raises(ContractError):
        eng.record("r1", result)  # no outstanding dispatch -> replay rejected
    _assert_converged(eng, work)
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.TEST


# --- concurrent duplicate records: exactly one winner, one charge -----------------


def test_concurrent_duplicate_records_charge_once(tmp_path, project, monkeypatch) -> None:
    """Two duplicate record() calls that BOTH pass the optimistic pre-check: exactly one
    clears the lease and charges the call; the loser gets the existing ContractError
    replay rejection (the authoritative lease check runs under the task lock)."""
    eng = _engine(tmp_path, project)
    work = _implement_dispatch(eng)
    result = make_result(work)

    # Hold both threads at the ledger append until each has passed the lock-free
    # pre-validation — the interleaving the lease check alone cannot serialize.
    barrier = threading.Barrier(2)
    orig_record = CostLedger.record

    def synced(self, res, **kw):
        barrier.wait(timeout=10)
        return orig_record(self, res, **kw)

    monkeypatch.setattr(CostLedger, "record", synced)

    outcomes: list[str] = []

    def call() -> None:
        try:
            eng.record("r1", result)
            outcomes.append("recorded")
        except ContractError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) == ["recorded", "rejected"]
    _assert_converged(eng, work)  # one row, one stage_recorded, lease cleared once
