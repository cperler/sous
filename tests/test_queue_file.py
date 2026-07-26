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
import multiprocessing as mp
import threading

import pytest

from orchestrator.errors import StatusNotFoundError, StatusStoreError
from orchestrator.queue_file import (
    _HAVE_FCNTL,
    QueueError,
    QueueFile,
    _ingest_batch,
    _run_exists,
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


def _append_in_subprocess(path: str, task_id: str, barrier) -> None:
    """Module-level (so it survives the ``spawn`` start method's pickle-by-reference) worker
    for the cross-process locking test: barrier-sync with the siblings so every producer piles
    into ``append`` at once, then append one entry. A raised exception exits non-zero, which
    the parent asserts on via ``exitcode``."""
    barrier.wait(timeout=30)
    QueueFile(path).append(make_entry([task_id]))


@pytest.mark.skipif(not _HAVE_FCNTL, reason="cross-process flock semantics require fcntl")
def test_cross_process_producers_do_not_lose_entries(tmp_path) -> None:
    # #128 / #113: the concurrent-producers thread test proves within-process serialization,
    # but the real threat is two SEPARATE `--enqueue` cron invocations — distinct processes,
    # each opening its own fd on the .lock sentinel. That is exactly what `fcntl.flock` (an
    # OS-level advisory lock keyed on the inode, not the fd) is for. Fork N real subprocesses
    # that barrier-synchronize and all append at once; the flock must serialize their
    # whole-array read-modify-writes so no producer's entry is lost — the precise
    # cross-process semantics that motivated the lock. `spawn` gives each child a fresh
    # interpreter (no shared address space), so this can only pass via the kernel lock.
    path = tmp_path / "queue.json"
    QueueFile(path).append(make_entry(["seed"]))  # a head every writer must preserve

    ctx = mp.get_context("spawn")
    n = 4
    barrier = ctx.Barrier(n)
    procs = [
        ctx.Process(target=_append_in_subprocess, args=(str(path), f"p{i}", barrier))
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)

    for p in procs:
        assert p.exitcode == 0, f"subprocess {p.name} exited {p.exitcode} (append raised?)"

    tasks = [e["tasks"][0] for e in QueueFile(path).read()]
    assert tasks[0] == "seed"  # the pre-existing entry is never lost
    assert sorted(tasks[1:]) == sorted(f"p{i}" for i in range(n))  # all N producers survived


@pytest.mark.skipif(not _HAVE_FCNTL, reason="fcntl lock path uses the .lock sentinel file")
def test_lock_sentinel_is_not_truncated_on_acquire(tmp_path) -> None:
    # #125: the flock sentinel is opened in append mode, so acquiring the lock never truncates
    # it. Pre-seed the sentinel and confirm an append (which takes the lock) leaves it intact.
    path = tmp_path / "queue.json"
    lock_file = path.with_name(f"{path.name}.lock")
    lock_file.write_text("keep-me")
    QueueFile(path).append(make_entry(["a"]))
    assert lock_file.read_text() == "keep-me"  # 'a' mode, not 'w' — no truncation


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


# --- _run_exists: a corrupt store must NOT read as "run absent" (#112) ------------

def test_run_exists_false_only_for_genuine_not_found(tmp_path) -> None:
    # A run id with no doc on disk is genuinely absent → False (the create-or-reuse path
    # relies on this to know when to create). StatusNotFoundError is the narrow signal.
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, E2EProject(repo_root=str(repo)))
    assert _run_exists(eng, "never-created") is False


def test_run_exists_raises_on_corrupt_run_doc(tmp_path) -> None:
    # Regression (#112): a corrupt (unparseable) run doc must RAISE, not be swallowed as
    # "run not found". Swallowing it would let _ingest_batch call create_run and overwrite
    # the partially-valid on-disk state.
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, E2EProject(repo_root=str(repo)))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.store._run_path("r1").write_text("{ this is not valid json", encoding="utf-8")

    with pytest.raises(StatusStoreError) as excinfo:
        _run_exists(eng, "r1")
    # the corrupt-file error, NOT the narrow not-found subclass, propagates.
    assert not isinstance(excinfo.value, StatusNotFoundError)


def test_ingest_batch_does_not_recreate_over_a_corrupt_run(tmp_path) -> None:
    # #112 at the caller: with a corrupt run doc, _ingest_batch must surface the error
    # instead of silently writing a fresh run over it. Since #280 the run doc on disk is
    # the proof — creation refuses on path existence (under the write lock), so the
    # corrupt bytes must come back untouched.
    repo = _repo(tmp_path)
    eng = _engine(tmp_path, E2EProject(repo_root=str(repo)))
    eng.create_run("r1", ExecutionLane.FULL)
    eng.store._run_path("r1").write_text("not json at all", encoding="utf-8")

    with pytest.raises(StatusStoreError):
        _ingest_batch(eng, make_entry(["t1"]), "r1", lane=ExecutionLane.FULL)
    assert eng.store._run_path("r1").read_text(encoding="utf-8") == "not json at all"


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
