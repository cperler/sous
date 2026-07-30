"""Task registration is recoverable as a unit (#278).

``add_task`` writes the run's TaskRef + dependency edge BEFORE the task document (so a
duplicate can never clobber persisted progress). A crash between those two writes used to
be permanent: the ref existed, its document did not, and every retry path treated the bare
ref as "already added" — the queue's re-ingest skipped it and ``add_task`` rejected it as a
duplicate, so the missing document was never rebuilt.

These tests pin the recovery: failure injection after EITHER write converges on retry, a
partial registration is repaired in place (one ref, one loadable document, intact edges, an
evented repair), a genuine duplicate is still rejected without clobbering progress, and
``_ingest_batch`` verifies document existence + identity before skipping an existing ref.

They also pin the other half of the invariant: because the repair path keys on the on-disk
shape "ref present, doc absent", that shape must be reachable ONLY by a crash. Both writes
therefore happen under one held task lock, and the race tests at the bottom widen the window
between them to prove a concurrent add is serialized (told "already added") rather than
repairing its way into a silent clobber of the first caller's lane/pins/deps.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError, StatusNotFoundError, StatusStoreError
from orchestrator.queue_file import _ingest_batch, make_entry
from orchestrator.schemas.enums import ExecutionLane, Stage, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _eng(tmp_path) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())


def _crash_after_ref_write(eng: Engine, monkeypatch) -> None:
    """Make the task-document write fail, simulating a process death between ``add_task``'s
    ref write and its doc write."""

    def _boom(task):  # noqa: ANN001 - matches StatusStore.write_task_locked
        raise RuntimeError("killed before the task doc was written")

    monkeypatch.setattr(eng.store, "write_task_locked", _boom)


def _repair_events(eng: Engine, run_id: str) -> list[dict]:
    return [
        e for e in eng.store.read_events(run_id) if e["type"] == "task_registration_repaired"
    ]


# --- (a) failure injection AFTER the ref write converges on retry ------------------

def test_retry_after_ref_write_crash_repairs_the_registration(tmp_path, monkeypatch) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    _crash_after_ref_write(eng, monkeypatch)
    with pytest.raises(RuntimeError):
        eng.add_task("r1", "t2", depends_on=["t1"])
    # The half-written state the old code could never recover from: ref present, doc absent.
    assert [ref.task_id for ref in eng.store.load_run("r1").task_refs] == ["t1", "t2"]
    with pytest.raises(StatusNotFoundError):
        eng.store.load_task("r1", "t2")

    monkeypatch.undo()
    task = eng.add_task("r1", "t2", depends_on=["t1"])  # the retry

    run = eng.store.load_run("r1")
    assert [ref.task_id for ref in run.task_refs] == ["t1", "t2"]  # exactly one ref each
    assert run.dependency_graph["t2"] == ["t1"]  # the edge survived/refreshed
    assert task.state is TaskState.PENDING
    assert eng.store.load_task("r1", "t2").depends_on == ["t1"]  # doc now loadable
    # The repair is never silent, and it is evented once.
    (event,) = _repair_events(eng, "r1")
    assert event["task_id"] == "t2" and event["reason"] == "status_document_missing"
    # …and the run drives normally from there.
    assert eng.dispatchable("r1") == ["t1"]


def test_repaired_registration_can_reroute_lane_and_pins(tmp_path, monkeypatch) -> None:
    # The retry is a full add_task, so the rebuilt document carries the retry's arguments —
    # the repair completes the registration, it does not resurrect a phantom shape.
    eng = _eng(tmp_path)
    eng.create_run("r1")
    _crash_after_ref_write(eng, monkeypatch)
    with pytest.raises(RuntimeError):
        eng.add_task("r1", "t1")
    monkeypatch.undo()

    task = eng.add_task("r1", "t1", ExecutionLane.MICRO)
    assert task.execution_lane is ExecutionLane.MICRO
    assert eng.store.load_task("r1", "t1").execution_lane is ExecutionLane.MICRO


# --- (b) failure injection AFTER the doc write: the duplicate guard still holds -----

def test_retry_after_doc_write_rejects_the_duplicate_and_keeps_progress(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")  # both writes landed; "the process died right here"
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # advance past intake
    before = eng.store._task_path("r1", "t1").read_bytes()

    with pytest.raises(ContractError, match="already added"):
        eng.add_task("r1", "t1")  # the retry: a genuine duplicate

    assert eng.store._task_path("r1", "t1").read_bytes() == before  # progress untouched
    assert eng.store.load_task("r1", "t1").stages[Stage.INTAKE].status.value == "completed"
    assert len(eng.store.load_run("r1").task_refs) == 1
    assert _repair_events(eng, "r1") == []  # nothing was "repaired"


def test_missing_doc_under_a_non_pending_ref_is_refused_not_reset(tmp_path) -> None:
    # A ref that records progress its (now unusable) document can no longer back is NOT a
    # partial registration — re-registering would invent a fresh PENDING state. Surface it.
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    assert eng.store.load_run("r1").task_refs[0].state is not TaskState.PENDING
    eng.store._task_path("r1", "t1").unlink()

    with pytest.raises(ContractError, match="refusing to re-register"):
        eng.add_task("r1", "t1")


# --- registered_task_ids: existence + identity, and #112 (corrupt is not absent) ----

def test_registered_task_ids_requires_a_matching_document(tmp_path) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    assert eng.registered_task_ids("r1") == {"t1", "t2"}

    eng.store._task_path("r1", "t2").unlink()
    assert eng.registered_task_ids("r1") == {"t1"}  # ref alone is not registration

    # A document that disagrees with the ref's identity is not that task's document.
    raw = json.loads(eng.store._task_path("r1", "t1").read_text(encoding="utf-8"))
    eng.store._task_path("r1", "t1").write_text(
        json.dumps({**raw, "task_id": "somebody-else"}), encoding="utf-8"
    )
    assert eng.registered_task_ids("r1") == set()


def test_registered_task_ids_propagates_a_corrupt_document(tmp_path) -> None:
    # #112: an unreadable/corrupt doc is never reported as "absent", because a re-add would
    # overwrite bytes that may hold real progress.
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.store._task_path("r1", "t1").write_text("{ half-written", encoding="utf-8")

    with pytest.raises(StatusStoreError):
        eng.registered_task_ids("r1")
    with pytest.raises(StatusStoreError):
        eng.add_task("r1", "t1")
    assert eng.store._task_path("r1", "t1").read_text(encoding="utf-8") == "{ half-written"


# --- (c) _ingest_batch validates the document before skipping an existing ref -------

def test_ingest_batch_readds_a_task_whose_document_is_missing(tmp_path) -> None:
    eng = _eng(tmp_path)
    entry = make_entry(["t1", "t2"], enqueued_at="2026-07-29T09:00:00+00:00")
    added, created = _ingest_batch(eng, entry, "r1", lane=ExecutionLane.FULL)
    assert created is True and added == ["t1", "t2"]

    eng.store._task_path("r1", "t1").unlink()  # the crash-between-writes shape
    added, created = _ingest_batch(eng, entry, "r1", lane=ExecutionLane.FULL)

    assert created is False and added == ["t1"]  # re-added, not skipped forever
    run = eng.store.load_run("r1")
    assert [ref.task_id for ref in run.task_refs] == ["t1", "t2"]  # no duplicate ref
    assert eng.store.load_task("r1", "t1").task_id == "t1"
    assert sorted(eng.dispatchable("r1")) == ["t1", "t2"]  # the run still drives


def test_ingest_batch_skips_fully_registered_tasks(tmp_path) -> None:
    eng = _eng(tmp_path)
    entry = make_entry(["t1"], enqueued_at="2026-07-29T10:00:00+00:00")
    _ingest_batch(eng, entry, "r1", lane=ExecutionLane.FULL)
    eng.record("r1", make_result(eng.next_work("r1", "t1")))

    added, created = _ingest_batch(eng, entry, "r1", lane=ExecutionLane.FULL)
    assert created is False and added == []  # verified registration → skipped
    assert eng.store.load_task("r1", "t1").stages[Stage.INTAKE].status.value == "completed"


# --- concurrent duplicate adds still converge on one ref, no clobber ----------------

def _slow_doc_write(eng: Engine, monkeypatch, delay: float = 0.25) -> None:
    """Widen the window between ``add_task``'s ref write and its doc write.

    Without this the race below almost always resolves on the fast path (the second caller
    arrives after the first has finished both writes), so a test that did not widen it would
    pass on timing rather than on the guarantee. The delay is spent INSIDE the doc write,
    i.e. exactly in the interval where the on-disk shape is "ref present, doc absent"."""
    real = eng.store.write_task_locked

    def _slow(task):  # noqa: ANN001 - matches StatusStore.write_task_locked
        time.sleep(delay)
        real(task)

    monkeypatch.setattr(eng.store, "write_task_locked", _slow)


def _race_add_task(eng: Engine, calls: list[dict]) -> tuple[list, list[ContractError]]:
    """Fire one ``add_task(**kwargs)`` per entry in ``calls`` simultaneously; return
    ``(tasks_returned, duplicate_rejections)`` and re-raise anything else."""
    barrier = threading.Barrier(len(calls))
    won: list = []
    rejected: list[ContractError] = []
    other: list[BaseException] = []
    guard = threading.Lock()

    def _add(kwargs: dict) -> None:
        barrier.wait()
        try:
            task = eng.add_task("r1", "t1", **kwargs)
        except ContractError as exc:
            with guard:
                rejected.append(exc)
        except BaseException as exc:  # noqa: BLE001 - any other failure is a real defect
            with guard:
                other.append(exc)
        else:
            with guard:
                won.append(task)

    threads = [threading.Thread(target=_add, args=(kw,)) for kw in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if other:
        raise other[0]
    return won, rejected


def test_concurrent_duplicate_adds_leave_one_ref_and_no_lost_progress(
    tmp_path, monkeypatch
) -> None:
    eng = _eng(tmp_path)
    eng.create_run("r1")
    _slow_doc_write(eng, monkeypatch)

    won, rejected = _race_add_task(eng, [{} for _ in range(4)])

    # Exactly ONE caller may register the task; every other caller is told so.
    assert len(won) == 1
    assert len(rejected) == 3
    assert all("already added" in str(exc) for exc in rejected)
    assert [ref.task_id for ref in eng.store.load_run("r1").task_refs] == ["t1"]
    assert eng.store.load_task("r1", "t1").state is TaskState.PENDING
    assert eng.registered_task_ids("r1") == {"t1"}
    # A live race is NOT a crash: nothing may be reported as a recovered registration.
    assert _repair_events(eng, "r1") == []


def test_concurrent_adds_with_differing_args_never_silently_clobber(
    tmp_path, monkeypatch
) -> None:
    # The clobber this pins is invisible when every caller passes identical arguments, so
    # each racer asks for a DIFFERENT lane/deps. "Ref present + doc absent + PENDING" is the
    # crash signature add_task repairs in place; if that shape were reachable by a live
    # second caller, both callers would be told they succeeded while only the last doc write
    # survived — one caller's lane/deps silently replaced by the other's.
    eng = _eng(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "dep")
    _slow_doc_write(eng, monkeypatch)

    won, rejected = _race_add_task(
        eng,
        [
            {"lane": ExecutionLane.MICRO},
            {"lane": ExecutionLane.FULL, "depends_on": ["dep"]},
        ],
    )

    assert len(won) == 1 and len(rejected) == 1
    winner = won[0]
    # What the winning caller was told it registered is exactly what is on disk…
    doc = eng.store.load_task("r1", "t1")
    assert doc.execution_lane is winner.execution_lane
    assert doc.pipeline == winner.pipeline
    assert doc.depends_on == winner.depends_on
    # …including the run-side dependency edge, which the loser must not have rewritten.
    run = eng.store.load_run("r1")
    assert run.dependency_graph["t1"] == winner.depends_on
    assert [ref.task_id for ref in run.task_refs] == ["dep", "t1"]
    assert _repair_events(eng, "r1") == []
