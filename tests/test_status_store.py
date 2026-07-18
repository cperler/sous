"""Tests for the StatusStore persistence layer (§7 status store)."""

from __future__ import annotations

import glob
import json
from datetime import UTC, datetime

import pytest

from orchestrator.errors import StatusStoreError
from orchestrator.schemas.enums import SCHEMA_VERSION, Stage, StageStatus, TaskState
from orchestrator.schemas.status import Run, StageRecord, Task, TaskRef
from orchestrator.status_store import StatusStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _make_run(run_id: str = "r1") -> Run:
    ts = _now()
    return Run(
        run_id=run_id,
        created_at=ts,
        updated_at=ts,
        task_refs=[TaskRef(task_id="t1", status_file=f"status-{run_id}-t1.json")],
        dependency_graph={"t1": []},
    )


def _make_task(run_id: str = "r1", task_id: str = "t1") -> Task:
    ts = _now()
    return Task(task_id=task_id, run_id=run_id, created_at=ts, updated_at=ts)


def test_run_roundtrip(tmp_path):
    store = StatusStore(tmp_path)
    run = _make_run()
    store.save_run(run)
    loaded = store.load_run(run.run_id)
    assert loaded == run


def test_task_roundtrip(tmp_path):
    store = StatusStore(tmp_path)
    task = _make_task()
    store.save_task(task)
    loaded = store.load_task(task.run_id, task.task_id)
    assert loaded == task


def test_atomic_write_leaves_no_tmp(tmp_path):
    store = StatusStore(tmp_path)
    store.save_run(_make_run())
    store.save_task(_make_task())
    leftovers = glob.glob(str(tmp_path / "*.tmp.*"))
    assert leftovers == []


def test_update_task_mutates_bumps_and_persists(tmp_path):
    store = StatusStore(tmp_path)
    task = _make_task()
    store.save_task(task)
    old_updated = task.updated_at

    def mutator(t: Task) -> None:
        t.state = TaskState.RUNNING

    returned = store.update_task(task.run_id, task.task_id, mutator)
    assert returned.state == TaskState.RUNNING
    assert returned.updated_at != old_updated

    reloaded = store.load_task(task.run_id, task.task_id)
    assert reloaded.state == TaskState.RUNNING
    assert reloaded.updated_at == returned.updated_at


def test_update_run_mutates_and_persists(tmp_path):
    store = StatusStore(tmp_path)
    run = _make_run()
    store.save_run(run)
    old_updated = run.updated_at

    def mutator(r: Run) -> None:
        r.metadata["touched"] = True

    returned = store.update_run(run.run_id, mutator)
    assert returned.metadata["touched"] is True
    assert returned.updated_at != old_updated
    assert store.load_run(run.run_id).metadata["touched"] is True


def test_load_missing_run_raises(tmp_path):
    store = StatusStore(tmp_path)
    with pytest.raises(StatusStoreError):
        store.load_run("nope")


def test_load_missing_task_raises(tmp_path):
    store = StatusStore(tmp_path)
    with pytest.raises(StatusStoreError):
        store.load_task("r1", "nope")


def test_append_event_appends_n_lines(tmp_path):
    store = StatusStore(tmp_path)
    n = 5
    for i in range(n):
        store.append_event("r1", {"seq": i, "kind": "test"})
    events_path = tmp_path / "events.jsonl"
    lines = events_path.read_text().splitlines()
    assert len(lines) == n
    parsed = [json.loads(line) for line in lines]
    assert [e["seq"] for e in parsed] == list(range(n))


def test_commit_task_events_writes_events_and_task(tmp_path):
    store = StatusStore(tmp_path)
    store.save_task(_make_task())

    def _mutate(t):
        t.pending_work_item_id = "wi-1"

    store.commit_task_events(
        "r1",
        "t1",
        _mutate,
        lambda t: [{"type": "stage_dispatched", "work_item_id": t.pending_work_item_id}],
    )
    assert store.load_task("r1", "t1").pending_work_item_id == "wi-1"
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"type": "stage_dispatched", "work_item_id": "wi-1"}
    ]


def test_commit_task_events_events_precede_task_commit(tmp_path):
    """Invariant (#174): a crash in the task-doc write must not orphan the events.
    Inject a failure at the task-doc write step and assert the events are already
    durable while the task doc still reflects the PRE-dispatch state — i.e. no
    task-mutated-but-event-missing orphan is possible."""
    store = StatusStore(tmp_path)
    store.save_task(_make_task())

    boom = RuntimeError("crash during task-doc write")

    def _explode(_task):
        raise boom

    store._write_task = _explode  # type: ignore[method-assign]

    def _mutate(t):
        t.pending_work_item_id = "wi-1"

    with pytest.raises(RuntimeError):
        store.commit_task_events(
            "r1",
            "t1",
            _mutate,
            [{"type": "stage_dispatched", "work_item_id": "wi-1"}],
        )

    # Event is durable...
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"type": "stage_dispatched", "work_item_id": "wi-1"}
    ]
    # ...but the task doc never advanced — so no orphan (task claims a dispatch whose
    # event is missing) can exist. The reverse (event present, task not) is safe.
    del store._write_task  # restore the class method for a clean load
    assert store.load_task("r1", "t1").pending_work_item_id is None


def test_v0_dict_loads_via_migrate(tmp_path):
    store = StatusStore(tmp_path)
    ts = _now()
    raw = {
        "document_type": "task",
        "task_id": "t1",
        "run_id": "r1",
        "created_at": ts,
        "updated_at": ts,
    }
    assert "schema_version" not in raw
    (tmp_path / "status-r1-t1.json").write_text(json.dumps(raw))
    loaded = store.load_task("r1", "t1")
    assert loaded.schema_version == SCHEMA_VERSION


def test_stage_started_at_explicit_key_survives_roundtrip(tmp_path):
    store = StatusStore(tmp_path)
    task = _make_task()
    assert task.stages[Stage.INTAKE].started_at is None
    store.save_task(task)

    raw = json.loads((tmp_path / "status-r1-t1.json").read_text())
    intake = raw["stages"]["intake"]
    assert "started_at" in intake
    assert intake["started_at"] is None

    reloaded = store.load_task("r1", "t1")
    assert reloaded.stages[Stage.INTAKE].started_at is None


def test_stage_started_at_value_survives_roundtrip(tmp_path):
    store = StatusStore(tmp_path)
    task = _make_task()
    task.stages[Stage.INTAKE] = StageRecord(
        status=StageStatus.RUNNING, started_at="2026-06-20T00:00:00+00:00"
    )
    store.save_task(task)
    reloaded = store.load_task("r1", "t1")
    assert reloaded.stages[Stage.INTAKE].started_at == "2026-06-20T00:00:00+00:00"


def test_sequential_update_task_counter_reaches_two(tmp_path):
    store = StatusStore(tmp_path)
    task = _make_task()
    store.save_task(task)

    def bump(t: Task) -> None:
        t.attempt += 1

    store.update_task(task.run_id, task.task_id, bump)
    store.update_task(task.run_id, task.task_id, bump)
    assert store.load_task(task.run_id, task.task_id).attempt == 2


def test_with_lock_is_reentrant_across_distinct_paths(tmp_path):
    store = StatusStore(tmp_path)
    p1 = tmp_path / "a"
    p2 = tmp_path / "b"
    with store.with_lock(p1), store.with_lock(p2):
        pass
    # locks released cleanly; a fresh acquire must succeed
    with store.with_lock(p1):
        pass
