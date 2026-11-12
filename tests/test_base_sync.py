"""#585: the engine syncs a moved base into the task branch before DELIVER.

Before this, trunk moving after the branch was cut failed the task at DELIVER with the work
intact: the deliver prompt refused, and nothing in the pipeline merged the base in. These
tests use real git repos (a bare ``origin``, a seed clone that moves trunk, and a task clone
standing in for the worktree).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.base_sync import sync_base
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Stage, StageStatus, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """(seed, work, base_sha): ``seed`` pushes to trunk, ``work`` is the task branch."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = T\n\temail = t@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "b.txt").write_text("b\n")
    base = _commit(seed, "a.txt", "a\n", "seed")
    _git(seed, "push", "-q", "origin", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "task/t1")
    _commit(work, "a.txt", "a\ntask\n", "task change")
    return seed, work, base


def _move_trunk(seed: Path, *, conflicting: bool, msg: str = "trunk moved") -> str:
    name, text = ("a.txt", "a\ntrunk\n") if conflicting else ("c.txt", "c\n")
    sha = _commit(seed, name, text, msg)
    _git(seed, "push", "-q", "origin", "main")
    return sha


# --- sync_base -------------------------------------------------------------------------


def test_a_branch_that_contains_the_base_is_already_current(repos) -> None:
    _seed, work, base = repos
    out = sync_base(str(work), base_sha=base)
    assert out.outcome == "already_current" and out.base_ref == "origin/main"
    assert out.head_before == out.head_after and out.advance_base_sha is None


def test_a_moved_base_is_merged_in_and_the_base_sha_advances(repos) -> None:
    seed, work, base = repos
    tip = _move_trunk(seed, conflicting=False)
    out = sync_base(str(work), base_sha=base)
    assert out.outcome == "synced" and out.base_tip == tip
    assert out.head_after != out.head_before
    _git(work, "merge-base", "--is-ancestor", tip, "HEAD")  # raises if not contained
    assert out.advance_base_sha == tip
    assert (work / "c.txt").exists()


def test_a_conflict_is_aborted_and_names_the_paths(repos) -> None:
    seed, work, base = repos
    _move_trunk(seed, conflicting=True)
    head = _git(work, "rev-parse", "HEAD")
    out = sync_base(str(work), base_sha=base)
    assert out.outcome == "conflict" and out.conflicts == ("a.txt",)
    assert out.conflicts_count == 1
    assert _git(work, "rev-parse", "HEAD") == head
    assert _git(work, "status", "--porcelain") == ""  # the merge was aborted


def test_no_origin_remote_is_skipped(repos) -> None:
    _seed, work, base = repos
    _git(work, "remote", "remove", "origin")
    out = sync_base(str(work), base_sha=base)
    assert out.outcome == "skipped" and out.reason == "no origin remote"


def test_a_dirty_tree_is_skipped_rather_than_merged_over(repos) -> None:
    seed, work, base = repos
    _move_trunk(seed, conflicting=False)
    (work / "a.txt").write_text("uncommitted\n")
    out = sync_base(str(work), base_sha=base)
    assert out.outcome == "skipped" and "uncommitted" in (out.reason or "")


def test_a_base_sha_off_trunk_is_not_advanced(repos) -> None:
    """A task that composed dependency branches at INTAKE records a base_sha that is not on
    trunk; moving it to the trunk tip would put the dependency code in the task's diff."""
    seed, work, _base = repos
    off_trunk = _git(work, "rev-parse", "HEAD")
    _move_trunk(seed, conflicting=False)
    out = sync_base(str(work), base_sha=off_trunk)
    assert out.outcome == "synced" and out.advance_base_sha is None


def test_a_project_base_branch_wins_over_origin_head(repos) -> None:
    seed, work, base = repos
    _git(seed, "checkout", "-q", "-b", "release")
    _commit(seed, "r.txt", "r\n", "release only")
    _git(seed, "push", "-q", "origin", "release")
    out = sync_base(str(work), base_sha=base, project_base="release")
    assert out.outcome == "synced" and out.base_ref == "origin/release"


def test_a_missing_worktree_is_skipped(tmp_path) -> None:
    out = sync_base(str(tmp_path / "gone"), base_sha=None)
    assert out.outcome == "skipped" and out.reason == "task worktree missing"


# --- the engine ------------------------------------------------------------------------


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path / "store"), CostLedger(tmp_path / "costs.jsonl"),
                  project, **kw)


def _start(eng: Engine, work: Path, base: str, lane=ExecutionLane.FULL) -> None:
    eng.create_run("r1", lane)
    eng.add_task("r1", "t1")
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.INTAKE
    eng.record("r1", make_result(w, structured_output={
        "branch": "task/t1", "worktree": str(work), "base_sha": base,
        "baseline_captured": True,
    }))


def _past_test(eng: Engine) -> None:
    """Record every stage up to and including TEST."""
    while True:
        w = eng.next_work("r1", "t1")
        assert w is not None and w.stage is not Stage.DELIVER
        eng.record("r1", make_result(w))
        if w.stage is Stage.TEST:
            return


def _syncs(eng: Engine) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == "base_sync"]


def test_trunk_moving_after_test_reruns_test_then_delivers(tmp_path, project, repos) -> None:
    seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    tip = _move_trunk(seed, conflicting=False)

    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.TEST
    assert w.review_cycle == 1  # the re-run's log files do not overwrite the first TEST's
    [sync] = _syncs(eng)
    assert sync["outcome"] == "synced" and sync["action"] == "rerun_test"
    assert sync["base_sha_from"] == base and sync["base_sha_to"] == tip
    task = eng.store.load_task("r1", "t1")
    assert task.base_sync_cycles == 1 and task.base_sync_conflicts == 0
    assert task.context["base_sha"] == tip
    _git(work, "merge-base", "--is-ancestor", tip, "HEAD")

    eng.record("r1", make_result(w))  # the merged tree is green
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.DELIVER
    assert [s["outcome"] for s in _syncs(eng)] == ["synced", "already_current"]
    assert eng.store.load_task("r1", "t1").state is not TaskState.FAILED


def test_an_unmoved_trunk_delivers_without_a_rerun(tmp_path, project, repos) -> None:
    _seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.DELIVER
    [sync] = _syncs(eng)
    assert sync["outcome"] == "already_current" and sync["action"] == "deliver"
    assert eng.store.load_task("r1", "t1").base_sync_cycles == 0


def test_a_red_rerun_reopens_implement(tmp_path, project, repos) -> None:
    from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus

    seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    _move_trunk(seed, conflicting=False)
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.TEST
    out = eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="red",
        structured_output={"passed": False, "failures": ["t::x"], "failure_kinds": ["unit"]},
        mode=ExecutionMode.ENGINE, provider=Provider.NONE,
    ))
    assert out["outcome"] == "test_failed_fix_cycle"
    assert eng.next_work("r1", "t1").stage is Stage.IMPLEMENT


def test_a_conflicting_trunk_reopens_implement_with_the_paths(tmp_path, project, repos) -> None:
    seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    _move_trunk(seed, conflicting=True)

    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.IMPLEMENT
    [sync] = _syncs(eng)
    assert sync["outcome"] == "conflict" and sync["action"] == "reopen_implement"
    assert sync["conflicts"] == ["a.txt"] and sync["level"] == "warning"
    task = eng.store.load_task("r1", "t1")
    assert task.base_sync_cycles == 1 and task.base_sync_conflicts == 1
    assert task.session_ref is None
    assert task.stages[Stage.TEST].status is StageStatus.PENDING
    assert "a.txt" in task.learnings[-1] and "git merge origin/main" in task.learnings[-1]
    assert "a.txt" in w.prompt
    assert _git(work, "status", "--porcelain") == ""


def test_repeated_conflicts_park_the_task_for_a_human(tmp_path, project, repos) -> None:
    seed, work, base = repos
    eng = _engine(tmp_path, project, max_attempts=2)
    _start(eng, work, base)
    _past_test(eng)
    _move_trunk(seed, conflicting=True)
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.IMPLEMENT  # one cycle (budget 1)
    eng.record("r1", make_result(w))
    _past_test(eng)  # the implementer did not resolve it

    assert eng.next_work("r1", "t1") is None
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.BLOCKED_ON_HUMAN
    assert "a.txt" in (task.last_error or "")
    assert task.stages[Stage.DELIVER].status is StageStatus.PENDING
    types = [e["type"] for e in eng.store.read_events("r1")]
    assert "base_sync_exhausted" in types and "held_for_approval" in types
    assert any(e["type"] == "notification" for e in eng.store.read_events("r1"))

    # A human merges trunk by hand and approves: DELIVER dispatches.
    _git(work, "fetch", "-q", "origin")
    subprocess.run(["git", "merge", "-q", "origin/main"], cwd=work, capture_output=True)
    (work / "a.txt").write_text("a\ntask\ntrunk\n")
    _git(work, "commit", "-q", "-am", "resolve")
    eng.approve("r1", "t1", approved_by="human")
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.DELIVER


def test_a_resumed_delivery_does_not_sync(tmp_path, project, repos) -> None:
    seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    first = eng.next_work("r1", "t1")
    assert first is not None and first.stage is Stage.DELIVER
    _move_trunk(seed, conflicting=False)
    again = eng.next_work("r1", "t1", resume=True)
    assert again is not None and again.stage is Stage.DELIVER
    assert len(_syncs(eng)) == 1  # only the fresh dispatch synced


def test_the_attribution_scan_ignores_merged_in_trunk_commits(tmp_path, project, repos) -> None:
    """Trunk commits brought in by the sync carry other people's trailers; they are not
    this run's commits and must not be reported as such."""
    seed, work, base = repos
    eng = _engine(tmp_path, project)
    _start(eng, work, base)
    _past_test(eng)
    head_before = _git(work, "rev-parse", "HEAD")
    _move_trunk(seed, conflicting=False,
                msg="trunk moved\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    w = eng.next_work("r1", "t1")
    assert w is not None and w.stage is Stage.TEST
    task = eng.store.load_task("r1", "t1")
    result = make_result(w, checkpoint={"sha": _git(work, "rev-parse", "HEAD")})
    eng._audit_commit_attribution("r1", task, result, since_sha=head_before)
    events = eng.store.read_events("r1")
    assert not [e for e in events if e["type"] == "commit_attribution_trailer_found"]
    scanned = [e for e in events if e["type"] == "commit_attribution_scanned"][-1]
    assert scanned.get("skipped") is None and scanned["range"].startswith(head_before)
    assert scanned["commits"] == 1  # the engine's merge commit, not the trunk commit
