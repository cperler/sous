"""Queue-file ingestion / unattended (cron) batch mode (#1, #279, #281).

Covers the two halves of ``orchestrator.queue_file``:

  * ``QueueFile`` — atomic append, the claim-in-place consumer protocol
    (``claim_head`` / ``complete_head`` / ``unclaim_head``, #279), and typed
    ``QueueError`` on malformed content.
  * ``drive_queue`` — a full claim→ingest→drive→complete cycle against the REAL
    headless/registry e2e infrastructure (reusing ``test_batch_e2e``'s
    scripted-but-real-effect lane) with a per-run engine factory (#281), plus
    restart-at-every-boundary simulations, two-consumer exclusion,
    unclaim-on-ingest-failure, per-run store isolation, and idle-timeout exit.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import subprocess
import threading
from pathlib import Path

import pytest

from orchestrator.engine import Engine
from orchestrator.errors import StatusStoreError
from orchestrator.queue_file import (
    _HAVE_FCNTL,
    QueueError,
    QueueFile,
    _ingest_batch,
    consumer_guard,
    drive_queue,
    make_entry,
    run_id_for,
)
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ExecutionLane
from tests.test_batch_e2e import E2EProject, GhStub, ScriptedLane, _engine, _repo

# --- QueueFile: append / claim / complete / unclaim ordering -----------------------

def test_append_claim_complete_unclaim_ordering(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    # empty queue reads cleanly (no file yet)
    assert qf.read() == []
    assert qf.peek_head() is None

    qf.append(make_entry(["a"]))
    qf.append(make_entry(["b"]))
    qf.append(make_entry(["c"]))
    assert [e["tasks"] for e in qf.read()] == [["a"], ["b"], ["c"]]  # FIFO append order

    assert qf.peek_head()["tasks"] == ["a"]
    assert len(qf.read()) == 3  # peek did not mutate

    # claim stamps the head IN PLACE — the entry stays in the queue (the #279 invariant:
    # no instant exists where the batch's only durable representation is gone).
    claimed = qf.claim_head("cron", "run-a")
    assert claimed["tasks"] == ["a"]
    assert claimed["claim"]["run_id"] == "run-a"
    assert claimed["claim"]["owner"] == "cron"
    assert claimed["claim"]["claimed_at"]
    assert isinstance(claimed["claim"]["host"], str) and claimed["claim"]["host"]
    assert isinstance(claimed["claim"]["pid"], int) and claimed["claim"]["pid"] > 0
    assert [e["tasks"] for e in qf.read()] == [["a"], ["b"], ["c"]]  # still queued

    # unclaim strips the claim in place (the ingest-failure undo) — entry stays head.
    unclaimed = qf.unclaim_head("cron", "run-a")
    assert "claim" not in unclaimed
    assert qf.peek_head()["tasks"] == ["a"] and "claim" not in qf.peek_head()

    # complete pops the head only under a matching claim.
    qf.claim_head("cron", "run-a")
    done = qf.complete_head("cron", "run-a")
    assert done["tasks"] == ["a"]
    assert [e["tasks"] for e in qf.read()] == [["b"], ["c"]]


def test_claim_head_is_idempotent_for_same_owner_and_run(tmp_path) -> None:
    # A consumer that crashed between claim and ingest re-claims its own head on restart.
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["a"]))
    first = qf.claim_head("cron", "run-a")
    again = qf.claim_head("cron", "run-a")
    assert again["claim"] == first["claim"]  # unchanged, not re-stamped
    assert len(qf.read()) == 1


def test_claim_head_refuses_foreign_claim(tmp_path) -> None:
    # Two-consumer exclusion: a head claimed by anyone else cannot be re-claimed.
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["a"]))
    qf.claim_head("consumer-1", "run-a")
    with pytest.raises(QueueError, match="already claimed by owner 'consumer-1'"):
        qf.claim_head("consumer-2", "run-a")
    # same owner asking for a DIFFERENT run id is also a conflict, not a silent restamp
    with pytest.raises(QueueError, match="already claimed"):
        qf.claim_head("consumer-1", "run-b")
    assert qf.peek_head()["claim"]["owner"] == "consumer-1"  # untouched


def test_release_dead_owner_claim_allows_another_owner_to_claim(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["a"]))
    stale = qf.claim_head("retired-cron", "run-a")["claim"]

    with pytest.raises(QueueError, match="not expected owner"):
        qf.release_head_claim(expect_owner="other-owner")
    assert qf.peek_head()["claim"] == stale

    released = qf.release_head_claim()

    assert released == stale
    assert "claim" not in qf.peek_head()
    assert qf.claim_head("replacement-cron", "run-b")["claim"]["owner"] == (
        "replacement-cron"
    )


@pytest.mark.skipif(not _HAVE_FCNTL, reason="consumer liveness guard requires fcntl")
def test_release_refuses_live_owner_unless_forced(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["a"]))

    with consumer_guard(qf, "cron"):
        claim = qf.claim_head("cron", "run-a")["claim"]
        with pytest.raises(QueueError, match="owner 'cron' is live"):
            qf.release_head_claim()
        assert qf.peek_head()["claim"] == claim

        assert qf.release_head_claim(force=True) == claim
        assert "claim" not in qf.peek_head()


def test_complete_and_unclaim_require_a_matching_claim(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    with pytest.raises(QueueError, match="queue is empty"):
        qf.complete_head("cron", "run-a")
    with pytest.raises(QueueError, match="queue is empty"):
        qf.claim_head("cron", "run-a")

    qf.append(make_entry(["a"]))
    with pytest.raises(QueueError, match="not claimed"):
        qf.complete_head("cron", "run-a")
    with pytest.raises(QueueError, match="not claimed"):
        qf.unclaim_head("cron", "run-a")

    qf.claim_head("cron", "run-a")
    with pytest.raises(QueueError, match="does not match"):
        qf.complete_head("other", "run-a")
    with pytest.raises(QueueError, match="does not match"):
        qf.complete_head("cron", "run-b")
    with pytest.raises(QueueError, match="does not match"):
        qf.unclaim_head("other", "run-a")
    assert len(qf.read()) == 1  # every refusal left the queue untouched


def test_claim_protocol_rejects_blank_identity(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["a"]))
    for owner, run_id in (("", "run-a"), ("cron", ""), ("  ", "run-a")):
        with pytest.raises(QueueError):
            qf.claim_head(owner, run_id)


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
        '[{"tasks": ["a"], "enqueued_at": "t", "claim": "run-1"}]',  # claim not an object
        '[{"tasks": ["a"], "enqueued_at": "t", "claim": {"run_id": "r"}}]',  # claim missing owner
        '[{"tasks": ["a"], "enqueued_at": "t", '
        '"claim": {"run_id": "r", "owner": "", "claimed_at": "t"}}]',  # blank claim owner
        '[{"tasks": ["a"], "enqueued_at": "t", '
        '"claim": {"run_id": "r", "owner": "o", "claimed_at": "t", "host": ""}}]',
        '[{"tasks": ["a"], "enqueued_at": "t", '
        '"claim": {"run_id": "r", "owner": "o", "claimed_at": "t", "pid": 0}}]',
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


# --- drive_queue harness: per-run engine factory over the e2e lane (#281) ---------

class RunStampedLane(ScriptedLane):
    """``ScriptedLane`` whose IMPLEMENT content differs per RUN: a task id reused across
    two derived runs shares one task branch in the product repo, so run-identical content
    would make the second run's commit empty. Stamping the run id keeps the real-effect
    commit genuine in both runs."""

    def _implement(self, w) -> None:
        wt = w.cwd
        assert wt, "implement dispatched without a folded worktree cwd (context-plane gap)"
        Path(wt, "change.txt").write_text(f"impl {w.run_id} {w.task_id} attempt {w.attempt}\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm",
             f"impl {w.run_id} {w.task_id}"],
            cwd=wt, check=True,
        )


def _factory(tmp_path, project, gh, *, lane_cls=ScriptedLane):
    """A ``drive_queue`` engine factory: per derived run id, a FRESH engine rooted at
    ``<tmp_path>/runs/<run_id>/`` (the #281 isolation model) plus the scripted-but-
    real-effect lane runner. Records every built engine so tests can assert per-run."""
    engines: dict[str, Engine] = {}

    def factory(run_id: str) -> tuple[Engine, ScriptedLane]:
        eng = _engine(tmp_path / "runs" / run_id, project)
        engines[run_id] = eng
        return eng, lane_cls(project, gh)

    return factory, engines


def _never_called_factory(run_id: str):  # pragma: no cover - must not be reached
    raise AssertionError(f"engine_factory must not be called (run_id={run_id})")


# --- drive_queue: full claim → ingest → drive → complete cycle --------------------

def test_drive_queue_ingests_drives_and_completes(tmp_path) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["t1", "t2"], branch="batch-1"))

    factory, engines = _factory(tmp_path, project, GhStub())
    summary = drive_queue(qf, factory, lane=ExecutionLane.FULL)

    # the batch was ingested into a single derived run and driven to terminal…
    assert summary["batches_processed"] == 1
    assert summary["runs_created"] == 1
    (row,) = summary["runs"]
    assert row["tasks"] == ["t1", "t2"]
    assert row["branch"] == "batch-1"
    assert row["final_state"] == "completed"

    # …the run really completed both tasks through its OWN per-run engine…
    eng = engines[row["run_id"]]
    status = eng.status(row["run_id"])
    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("t1", "t2"))

    # …and the queue is now empty (the head was completed, not left behind).
    assert qf.read() == []


def test_drive_queue_reuses_run_on_reingest(tmp_path) -> None:
    # Regression (project norm): a re-appended identical batch derives the SAME stable run
    # id, so a second drive REUSES the completed run and adds nothing — it must not raise a
    # duplicate-task error (the resumability contract for a crashed-then-relaunched driver).
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    entry = make_entry(["t1"], enqueued_at="2026-07-04T09:00:00+00:00")
    qf.append(dict(entry))

    factory, _engines = _factory(tmp_path, project, GhStub())
    first = drive_queue(qf, factory, lane=ExecutionLane.FULL)
    assert first["runs"][0]["final_state"] == "completed"

    # re-enqueue the very same batch (same enqueued_at → same run id) and drive again.
    qf.append(dict(entry))
    second = drive_queue(qf, factory, lane=ExecutionLane.FULL)
    assert second["batches_processed"] == 1
    assert second["runs_created"] == 0  # the run already existed — reused, not recreated
    assert second["runs"][0]["added"] == []  # t1 already present — nothing re-added
    assert second["runs"][0]["final_state"] == "completed"
    assert qf.read() == []


# --- drive_queue: restart at every kill boundary (#279) ---------------------------
#
# The claim protocol's whole point: at EVERY boundary from peek through scheduler start,
# either the queue entry or the claimed run (or both) is durably on disk, and a restart
# with the same owner converges. Each test drives the protocol to a boundary by hand
# ("the process died here"), asserts durability, then relaunches drive_queue.

def test_restart_after_claim_resumes_the_claimed_run(tmp_path) -> None:
    # Boundary (a): claim written, death before any run state exists. The entry (with its
    # claim naming the future run id) IS the durable representation; a restart with the
    # same owner adopts claim.run_id, ingests, drives, completes.
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    entry = qf.append(make_entry(["t1"], enqueued_at="2026-07-05T10:00:00+00:00"))
    run_id = run_id_for(entry)
    # The first consumer owns the process-lifetime lock when it writes the claim. Leaving
    # the context simulates its death: the kernel releases the lock, so a same-owner
    # relaunch must be able to acquire it and adopt this claim.
    with consumer_guard(qf, "cron"):
        qf.claim_head("cron", run_id)  # … and the process dies here

    # durable: the claimed entry is still in the queue file on disk
    on_disk = json.loads((tmp_path / "queue.json").read_text())
    assert on_disk[0]["claim"]["run_id"] == run_id
    assert not (tmp_path / "runs" / run_id).exists()  # no run state yet — entry carries it

    factory, _engines = _factory(tmp_path, project, GhStub())
    summary = drive_queue(qf, factory, owner="cron", lane=ExecutionLane.FULL)
    assert summary["batches_processed"] == 1
    assert summary["runs"][0]["run_id"] == run_id  # adopted, not re-derived
    assert summary["runs"][0]["final_state"] == "completed"
    assert qf.read() == []


@pytest.mark.skipif(not _HAVE_FCNTL, reason="consumer liveness guard requires fcntl")
def test_second_live_consumer_with_same_owner_is_refused(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["t1"]))

    # Two independently opened fds contend even in one process, so this models an
    # overlapping invocation without a subprocess or a scheduler race.
    with consumer_guard(qf, "cron"), pytest.raises(
        QueueError, match="owner 'cron' is already live"
    ):
        drive_queue(qf, _never_called_factory, owner="cron")

    assert "claim" not in qf.peek_head()


def test_restart_after_partial_ingest_converges(tmp_path) -> None:
    # Boundary (b): death after the run exists and tasks were added, before the scheduler
    # started. Both representations exist (claimed entry + run doc); the restart re-ingests
    # idempotently (create_or_reuse_run + skip-already-added) and drives to terminal.
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    entry = qf.append(make_entry(["t1", "t2"], enqueued_at="2026-07-05T11:00:00+00:00"))
    run_id = run_id_for(entry)
    qf.claim_head("cron", run_id)

    factory, _engines = _factory(tmp_path, project, GhStub())
    eng, _runner = factory(run_id)
    added, created = _ingest_batch(eng, entry, run_id, lane=ExecutionLane.FULL)
    assert created is True and added == ["t1", "t2"]
    # … and the process dies here. Durable: claimed entry AND the ingested run doc.
    assert json.loads((tmp_path / "queue.json").read_text())[0]["claim"]["run_id"] == run_id
    assert eng.store.run_exists(run_id)

    summary = drive_queue(qf, factory, owner="cron", lane=ExecutionLane.FULL)
    assert summary["batches_processed"] == 1
    assert summary["runs_created"] == 0  # reused the already-created run
    assert summary["runs"][0]["run_id"] == run_id
    assert summary["runs"][0]["added"] == []  # idempotent re-ingest added nothing
    assert summary["runs"][0]["final_state"] == "completed"
    assert qf.read() == []


def test_restart_mid_scheduler_resumes_the_run(tmp_path) -> None:
    # Boundary (c): death mid-scheduler. The claimed entry survives, the run has partial
    # progress; the restart adopts the claim and Scheduler.run's resumability finishes it.
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    entry = qf.append(make_entry(["t1"], enqueued_at="2026-07-05T12:00:00+00:00"))
    run_id = run_id_for(entry)
    qf.claim_head("cron", run_id)

    gh = GhStub()
    factory, _engines = _factory(tmp_path, project, gh)
    eng, _runner = factory(run_id)
    _ingest_batch(eng, entry, run_id, lane=ExecutionLane.FULL)
    # a few scheduler ticks only — then the process dies mid-run
    Scheduler(eng).run(run_id, ScriptedLane(project, gh), max_ticks=2)
    partial = eng.status(run_id)
    assert partial["run_state"] == "running"  # genuinely mid-flight, not terminal
    assert json.loads((tmp_path / "queue.json").read_text())[0]["claim"]["run_id"] == run_id

    summary = drive_queue(qf, factory, owner="cron", lane=ExecutionLane.FULL)
    assert summary["batches_processed"] == 1
    assert summary["runs"][0]["run_id"] == run_id
    assert summary["runs"][0]["final_state"] == "completed"
    assert qf.read() == []


def test_relaunch_after_complete_is_a_noop(tmp_path) -> None:
    # Boundary (d): after complete_head the entry is gone; a re-launch processes nothing
    # and touches no engine (the factory must not even be called).
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["t1"], enqueued_at="2026-07-05T13:00:00+00:00"))

    factory, _engines = _factory(tmp_path, project, GhStub())
    first = drive_queue(qf, factory, owner="cron", lane=ExecutionLane.FULL)
    assert first["batches_processed"] == 1
    assert qf.read() == []

    second = drive_queue(qf, _never_called_factory, owner="cron")
    assert second == {
        "batches_processed": 0, "runs_created": 0, "runs": [], "idle_timed_out": False,
        "consumer_guard": "held",
    }


def test_drive_queue_refuses_a_foreign_claim(tmp_path) -> None:
    # Two-consumer exclusion: a head claimed by a DIFFERENT owner is never processed —
    # drive_queue raises and leaves the entry (and its claim) untouched.
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["t1"]))
    qf.claim_head("other-consumer", "their-run")

    with pytest.raises(QueueError, match="claimed by another consumer"):
        drive_queue(qf, _never_called_factory, owner="cron")
    head = qf.peek_head()  # entry untouched — still queued, still theirs
    assert head["tasks"] == ["t1"]
    assert head["claim"]["owner"] == "other-consumer"
    assert head["claim"]["run_id"] == "their-run"


# --- #112 at the ingest caller: corrupt run docs surface, never get overwritten ---

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


# --- drive_queue: unclaim on ingest failure ---------------------------------------

def test_drive_queue_unclaims_head_on_ingest_failure(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    qf.append(make_entry(["bad"], branch="b"))

    def _boom(*a, **k):
        raise RuntimeError("task source is down")

    base_factory, _engines = _factory(tmp_path, project, GhStub())

    def failing_factory(run_id: str):
        eng, runner = base_factory(run_id)
        monkeypatch.setattr(eng, "add_task", _boom)
        return eng, runner

    with pytest.raises(QueueError, match="claim released, entry stays queued"):
        drive_queue(qf, failing_factory, owner="cron", lane=ExecutionLane.FULL)

    # the claim was stripped in place — the entry is still the head, ready for retry.
    head = qf.peek_head()
    assert head["tasks"] == ["bad"] and head["branch"] == "b"
    assert "claim" not in head


# --- #281: two batches → two self-contained run stores ----------------------------

def test_two_batches_get_isolated_per_run_stores(tmp_path) -> None:
    # #281 acceptance: two queued batches SHARING a task id produce two self-contained
    # run directories; per-run ledgers and stages/<task>/ artifacts don't collide; each
    # run's status reports only its own calls.
    repo = _repo(tmp_path)
    project = E2EProject(repo_root=str(repo))
    qf = QueueFile(tmp_path / "queue.json")
    e1 = qf.append(make_entry(["t1"], enqueued_at="2026-07-06T08:00:00+00:00"))
    e2 = qf.append(make_entry(["t1"], enqueued_at="2026-07-06T09:00:00+00:00"))
    rid1, rid2 = run_id_for(e1), run_id_for(e2)
    assert rid1 != rid2

    factory, engines = _factory(tmp_path, project, GhStub(), lane_cls=RunStampedLane)
    summary = drive_queue(qf, factory, lane=ExecutionLane.FULL)
    assert summary["batches_processed"] == 2
    assert summary["runs_created"] == 2
    assert [r["run_id"] for r in summary["runs"]] == [rid1, rid2]
    assert all(r["final_state"] == "completed" for r in summary["runs"])

    # two distinct, self-contained run dirs under the runs-root…
    for rid in (rid1, rid2):
        run_root = tmp_path / "runs" / rid
        assert (run_root / "stage-costs.jsonl").is_file()  # per-run ledger
        # per-run stage artifacts for the SHARED task id — no cross-run overwrite
        stage_logs = list((run_root / "store" / "stages").rglob("*.json"))
        assert stage_logs, f"run {rid} has no stage logs of its own"
        # …whose ledger rows all belong to that run alone
        rows = engines[rid].ledger.rows()
        assert rows and all(row["run_id"] == rid for row in rows)

    # each run's status counts ONLY its own calls (they completed the identical pipeline,
    # so equal counts — the shared-ledger bug reported the SUM in both).
    s1 = engines[rid1].status(rid1)
    s2 = engines[rid2].status(rid2)
    assert s1["cost"]["total_invocations"] == s2["cost"]["total_invocations"]
    assert s1["cost"]["total_invocations"] == len(engines[rid1].ledger.rows())
    assert s1["lane_audit"]["total_calls"] == s2["lane_audit"]["total_calls"]


# --- drive_queue: idle-timeout exit on an empty queue -----------------------------

def test_drive_queue_idle_timeout_exits(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")  # empty
    slept: list[int] = []

    summary = drive_queue(
        qf, _never_called_factory, sleeper=slept.append,
        idle_timeout_s=45, poll_interval_s=15,
    )
    assert summary["idle_timed_out"] is True
    assert summary["batches_processed"] == 0
    assert slept == [15, 15, 15]  # polled every 15s up to the 45s timeout, then exited


def test_drive_queue_without_sleeper_is_single_pass(tmp_path) -> None:
    # No sleeper = one drain pass, then return (the caller re-launches later). An empty
    # queue returns immediately without idling.
    qf = QueueFile(tmp_path / "queue.json")
    summary = drive_queue(qf, _never_called_factory)
    assert summary == {
        "batches_processed": 0, "runs_created": 0, "runs": [], "idle_timed_out": False,
        "consumer_guard": "held",
    }


def test_drive_queue_reports_when_consumer_guard_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("orchestrator.queue_file._HAVE_FCNTL", False)
    summary = drive_queue(QueueFile(tmp_path / "queue.json"), _never_called_factory)
    assert summary["consumer_guard"] == "unavailable"


def test_drive_queue_rejects_blank_owner(tmp_path) -> None:
    qf = QueueFile(tmp_path / "queue.json")
    with pytest.raises(QueueError, match="`owner` must be a non-empty string"):
        drive_queue(qf, _never_called_factory, owner="  ")
