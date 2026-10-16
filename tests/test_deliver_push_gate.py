"""#500 — a DELIVER that reports success must have PUSHED the commit it checkpointed.

On ``ff-v1-b32`` task 254 the DELIVER agent ran ``git fetch origin task/254`` (a bare refspec
updates ``FETCH_HEAD``, not the stale ``origin/task/254`` tracking ref), read two reports of
an unpushed commit as "already up to date", and recorded success without pushing. PR 264
sat one commit behind the delivered checkpoint, and the only trace was a completion-time
advisory event a human had to go read.

These tests drive the engine with a real task worktree and a real bare ``origin``: the
engine reads the remote itself with ``git ls-remote`` and fails the stage on a mismatch, so
the ordinary retry re-opens DELIVER with the mismatch as learnings. It never pushes.
"""

from __future__ import annotations

import subprocess

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

BRANCH = "task/254"


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _commit(cwd, name: str) -> str:
    (cwd / name).write_text(name)
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-qm", f"add {name}")
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _repo(tmp_path, *, with_origin: bool = True):
    """A task worktree on BRANCH with one pushed commit; returns (worktree, pushed sha)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", BRANCH)
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    sha = _commit(wt, "base.txt")
    if with_origin:
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(wt, "remote", "add", "origin", str(origin))
        assert _git(wt, "push", "-q", "origin", BRANCH).returncode == 0
    return wt, sha


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path / "store"),
                  CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _to_deliver(eng: Engine, worktree: str):
    """Record every stage before DELIVER green, so the next dispatch is DELIVER."""
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    intake = {"branch": BRANCH, "baseline_captured": True, "worktree": worktree}
    eng.record("r1", make_result(eng.next_work("r1", "t1"), structured_output=intake))
    while (work := eng.next_work("r1", "t1")).stage is not Stage.DELIVER:
        eng.record("r1", make_result(work))
    return work


def _events(eng: Engine, etype: str) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == etype]


def test_an_unpushed_checkpoint_fails_deliver_and_retries_with_the_mismatch(
    tmp_path, project
) -> None:
    """The #254 repro: the checkpoint is one commit ahead of what origin (and the PR) hold."""
    wt, pushed = _repo(tmp_path)
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(wt))
    local = _commit(wt, "fix.txt")  # committed, never pushed

    summary = eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": local}))

    assert summary["outcome"] != "stage_completed"
    (event,) = _events(eng, "deliver_push_mismatch")
    assert event["level"] == "warning"
    assert (event["expected_sha"], event["remote_sha"], event["branch"]) == (
        local, pushed, BRANCH)
    task = eng.store.load_task("r1", "t1")
    assert any(local in entry and "ls-remote" in entry for entry in task.learnings)
    retry = eng.next_work("r1", "t1")
    assert (retry.stage, retry.attempt) == (Stage.DELIVER, work.attempt + 1)
    # Verify, never perform: the engine did not push on DELIVER's behalf.
    remote = _git(wt, "ls-remote", "origin", f"refs/heads/{BRANCH}").stdout.split()[0]
    assert remote == pushed


def test_a_pushed_checkpoint_passes_with_a_receipt(tmp_path, project) -> None:
    wt, _ = _repo(tmp_path)
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(wt))
    sha = _commit(wt, "fix.txt")
    _git(wt, "push", "-q", "origin", BRANCH)

    summary = eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert summary["outcome"] == "stage_completed"
    (event,) = _events(eng, "deliver_push_verified")
    assert event["remote_sha"] == sha == event["expected_sha"]
    assert _events(eng, "deliver_push_mismatch") == []


def test_a_branch_missing_on_the_remote_fails_deliver(tmp_path, project) -> None:
    wt, _ = _repo(tmp_path)
    _git(wt, "push", "-q", "origin", "--delete", BRANCH)
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(wt))
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()

    summary = eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert summary["outcome"] != "stage_completed"
    (event,) = _events(eng, "deliver_push_mismatch")
    assert event["remote_sha"] is None


def test_no_origin_leaves_the_stage_green_but_unverified(tmp_path, project) -> None:
    """Fail-open: a check that could not run does not fail the stage, but says so."""
    wt, sha = _repo(tmp_path, with_origin=False)
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(wt))

    summary = eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert summary["outcome"] == "stage_completed"
    (event,) = _events(eng, "deliver_push_unverified")
    assert (event["level"], event["reason"]) == ("warning", "ls_remote_failed")


def test_a_missing_worktree_or_checkpoint_is_unverified(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(tmp_path / "absent"))

    summary = eng.record("r1", make_result(work))

    assert summary["outcome"] == "stage_completed"
    (event,) = _events(eng, "deliver_push_unverified")
    assert event["reason"] == "no_checkpoint"


def test_a_vetoed_deliver_skips_the_push_check(tmp_path, project) -> None:
    """No PR opened is already the failure to retry on; no ls-remote round trip is spent."""
    wt, _ = _repo(tmp_path)
    eng = _engine(tmp_path, project)
    work = _to_deliver(eng, str(wt))
    local = _commit(wt, "fix.txt")

    eng.record("r1", make_result(work, structured_output={"pr_number": 0, "pr_url": ""},
                                 checkpoint={"tag": "cp", "sha": local}))

    assert not [e for e in eng.store.read_events("r1")
                if str(e["type"]).startswith("deliver_push_")]
