"""Queue-file ingestion / unattended (cron) batch mode (#1).

Covers the two halves of ``orchestrator.queue_file``:

  * ``QueueFile`` — atomic append, FIFO pop-head, requeue-prepend ordering, and typed
    ``QueueError`` on malformed content.
  * ``drive_queue`` — a full ingest→drive→dequeue cycle against the REAL headless/registry
    e2e infrastructure (reusing ``test_batch_e2e``'s scripted-but-real-effect lane), plus
    requeue-on-ingest-failure and idle-timeout exit.
"""

from __future__ import annotations

import json
import threading

import pytest

from orchestrator.queue_file import (
    QueueError,
    QueueFile,
    drive_queue,
    make_entry,
    run_id_for,
)
from orchestrator.schemas.enums import ExecutionLane
from tests.test_batch_e2e import E2EProject, GhStub, ScriptedLane, _engine, _repo

# --- QueueFile: append / pop / requeue ordering -----------------------------------

def test_append_pop_requeue_ordering(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    # empty queue reads cleanly (no file yet)
    assert qf.read() == []
    assert qf.peek_head() is None
    assert qf.pop_head() is None

    qf.append(make_entry(["a"]))
    qf.append(make_entry(["b"]))
    qf.append(make_entry(["c"]))
    assert [e["tasks"] for e in qf.read()] == [["a"], ["b"], ["c"]]  # FIFO append order

    assert qf.peek_head()["tasks"] == ["a"]
    assert qf.read()  # peek did not mutate
    assert qf.pop_head()["tasks"] == ["a"]  # dequeue the head
    assert [e["tasks"] for e in qf.read()] == [["b"], ["c"]]

    qf.requeue_head(make_entry(["a"]))  # ingest-failure undo re-prepends
    assert [e["tasks"] for e in qf.read()] == [["a"], ["b"], ["c"]]


def test_append_is_atomic_on_disk(tmp_path) -> None:
    # After an append the file is valid JSON (temp-file + os.replace never leaves a partial).
    path = tmp_path / "queue.json"
    QueueFile(path).append(make_entry(["x"], branch="feature"))
    data = json.loads(path.read_text())
    assert data == [
        {"tasks": ["x"], "branch": "feature", "enqueued_at": data[0]["enqueued_at"]}
    ]
    # no stray temp files left behind in the dir
    assert not list(tmp_path.glob(".queue-*.tmp"))


def test_concurrent_producers_do_not_lose_entries(tmp_path) -> None:
    # #113 / #115 regression: append() is a whole-array read-modify-write, so two parallel
    # cron `--enqueue` producers could both read the old array and clobber one entry (a lost
    # update). The exclusive `_with_lock` serializes the read→write, so EVERY producer's
    # entry must survive — including a pre-existing head that none may drop. A barrier fans
    # the writers in together to force maximum contention; flock serializes them across the
    # separate fds each append opens (real mutual exclusion even within one process).
    path = tmp_path / "queue.json"
    QueueFile(path).append(make_entry(["seed"]))  # a head every writer must preserve

    n = 16
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(task_id: str) -> None:
        try:
            barrier.wait(timeout=30)
            QueueFile(path).append(make_entry([task_id]))
        except (QueueError, threading.BrokenBarrierError) as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    tasks = [e["tasks"][0] for e in QueueFile(path).read()]
    assert tasks[0] == "seed"  # the pre-existing entry is never lost
    assert sorted(tasks[1:]) == sorted(f"t{i}" for i in range(n))  # all N producers survived


# --- QueueFile: malformed input → typed QueueError --------------------------------

@pytest.mark.parametrize(
    "content",
    [
        "{not json",  # unparseable
        '{"tasks": ["a"]}',  # top level is an object, not an array
        '[{"branch": "b", "enqueued_at": "t"}]',  # entry missing `tasks`
        '[{"tasks": [], "enqueued_at": "t"}]',  # empty `tasks`
        '[{"tasks": ["a"], "enqueued_at": ""}]',  # blank enqueued_at
        '[{"tasks": ["a"], "branch": 7, "enqueued_at": "t"}]',  # non-string branch
        '[{"tasks": [1, 2], "enqueued_at": "t"}]',  # non-string task ids
    ],
)
def test_malformed_queue_raises_queue_error(tmp_path, content) -> None:
    path = tmp_path / "queue.json"
    path.write_text(content)
    with pytest.raises(QueueError):
        QueueFile(path).read()


def test_make_entry_rejects_empty_tasks() -> None:
    with pytest.raises(QueueError):
        make_entry([])


# --- run id derivation: stable across restarts ------------------------------------

def test_run_id_is_stable_for_a_given_enqueued_at() -> None:
    entry = make_entry(["t1"], enqueued_at="2026-07-04T12:30:00+00:00")
    rid = run_id_for(entry)
    assert rid == run_id_for(entry)  # deterministic — a restarted driver reuses the run
    assert rid.startswith("queue-")
    assert ":" not in rid and "+" not in rid  # punctuation normalized to a safe run id


# --- drive_queue: full ingest → drive → dequeue cycle -----------------------------

def test_drive_queue_ingests_drives_and_dequeues(tmp_path) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    eng = _engine(tmp_path, project)
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["t1", "t2"], branch="batch-1"))

    gh = GhStub()
    lane = ScriptedLane(project, gh)  # routes deterministic stages to the REAL executors
    summary = drive_queue(eng, qf, lane, lane=ExecutionLane.FULL)

    # the batch was ingested into a single derived run and driven to terminal…
    assert summary["batches_processed"] == 1
    assert summary["runs_created"] == 1
    (row,) = summary["runs"]
    assert row["tasks"] == ["t1", "t2"]
    assert row["branch"] == "batch-1"
    assert row["final_state"] == "completed"

    # …the run really completed both tasks through the engine…
    status = eng.status(row["run_id"])
    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("t1", "t2"))

    # …and the queue is now empty (the head was dequeued, not left behind).
    assert qf.read() == []


def test_drive_queue_reuses_run_on_reingest(tmp_path) -> None:
    # Regression (project norm): a re-appended identical batch derives the SAME stable run
    # id, so a second drive REUSES the completed run and adds nothing — it must not raise a
    # duplicate-task error (the resumability contract for a crashed-then-relaunched driver).
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    eng = _engine(tmp_path, project)
    qf = QueueFile(tmp_path / "queue.json")
    entry = make_entry(["t1"], enqueued_at="2026-07-04T09:00:00+00:00")
    qf.append(dict(entry))

    gh = GhStub()
    first = drive_queue(eng, qf, ScriptedLane(project, gh), lane=ExecutionLane.FULL)
    assert first["runs"][0]["final_state"] == "completed"

    # re-enqueue the very same batch (same enqueued_at → same run id) and drive again.
    qf.append(dict(entry))
    second = drive_queue(eng, qf, ScriptedLane(project, gh), lane=ExecutionLane.FULL)
    assert second["batches_processed"] == 1
    assert second["runs_created"] == 0  # the run already existed — reused, not recreated
    assert second["runs"][0]["added"] == []  # t1 already present — nothing re-added
    assert second["runs"][0]["final_state"] == "completed"
    assert qf.read() == []


# --- drive_queue: requeue on ingest failure ---------------------------------------

def test_drive_queue_requeues_head_on_ingest_failure(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    eng = _engine(tmp_path, project)
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["bad"], branch="b"))

    def _boom(*a, **k):
        raise RuntimeError("task source is down")

    monkeypatch.setattr(eng, "add_task", _boom)

    gh = GhStub()
    with pytest.raises(QueueError):
        drive_queue(eng, qf, ScriptedLane(project, gh), lane=ExecutionLane.FULL)

    # the popped batch was re-prepended at the head, not dropped.
    assert [e["tasks"] for e in qf.read()] == [["bad"]]
    assert qf.peek_head()["branch"] == "b"


# --- drive_queue: peek-then-pop invariant holds under `python -O` -----------------

def test_drive_queue_raises_when_pop_misses_after_peek() -> None:
    # Guard (issue #114): peek saw a head but pop returned None — a single-consumer
    # invariant violation (concurrent mutation). The old `assert entry is not None`
    # was a no-op under `python -O`; the guard must raise a typed QueueError in every
    # execution mode. engine/runner are never reached before the raise — pass sentinels.
    class _PopMissesQueue:
        def peek_head(self):
            return make_entry(["t1"])  # a head is visible…

        def pop_head(self):
            return None  # …but it vanished before we could pop it

    with pytest.raises(QueueError, match="vanished between peek_head and pop_head"):
        drive_queue(object(), _PopMissesQueue(), None)


# --- drive_queue: idle-timeout exit on an empty queue -----------------------------

def test_drive_queue_idle_timeout_exits(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")  # empty
    slept: list[int] = []

    # engine/runner are never touched when the queue is empty — pass sentinels.
    summary = drive_queue(
        object(), qf, None, sleeper=slept.append,
        idle_timeout_s=45, poll_interval_s=15,
    )
    assert summary["idle_timed_out"] is True
    assert summary["batches_processed"] == 0
    assert slept == [15, 15, 15]  # polled every 15s up to the 45s timeout, then exited


def test_drive_queue_without_sleeper_is_single_pass(tmp_path) -> None:
    # No sleeper = one drain pass, then return (the caller re-launches later). An empty
    # queue returns immediately without idling.
    qf = QueueFile(tmp_path / "queue.json")
    summary = drive_queue(object(), qf, None)
    assert summary == {
        "batches_processed": 0, "runs_created": 0, "runs": [], "idle_timed_out": False,
    }
