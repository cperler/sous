"""Stage-commit checkpoint protocol (2026-07-01 design pass §3).

The split under test: the transport wrapper owns ALL git I/O (tag after success,
reset before a retry/crash-resume); the engine only names tags and picks reset
anchors as WorkItem fields. Never skip a stage because its tag exists.
"""

from __future__ import annotations

import subprocess

import pytest

from adapters.execution.transport import RawResult, checkpointing_transport
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    """A main checkout plus a linked worktree (the shape intake produces)."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("v1")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "c1")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "task-branch")
    return main, wt


def _work(*, cwd=None, checkpoint_tag=None, reset_to=None, stage=Stage.IMPLEMENT) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r", task_id="t", stage=stage, prompt="p",
        schema_ref=stage.value, model="m", created_at="now",
        cwd=cwd, checkpoint_tag=checkpoint_tag, reset_to=reset_to,
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )


# --- tagging ------------------------------------------------------------------

def test_success_tags_head_and_stamps_the_checkpoint(repo) -> None:
    _, wt = repo
    transport = checkpointing_transport(lambda w: RawResult({"committed": True}))
    raw = transport(_work(cwd=str(wt), checkpoint_tag="task/r/t/implement/0"))
    assert raw.checkpoint["tag"] == "task/r/t/implement/0"
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert raw.checkpoint["sha"] == head
    assert _git(wt, "tag", "-l", "task/r/t/implement/0").stdout.strip()  # tag exists


def test_intake_tags_the_worktree_named_in_its_own_output(repo) -> None:
    _, wt = repo
    # intake runs with cwd=None (it CREATES the worktree); the wrapper tags the
    # worktree the stage's structured output names.
    transport = checkpointing_transport(lambda w: RawResult({"worktree": str(wt), "branch": "b"}))
    raw = transport(_work(cwd=None, checkpoint_tag="task/r/t/intake/0", stage=Stage.INTAKE))
    assert raw.checkpoint is not None
    assert _git(wt, "tag", "-l", "task/r/t/intake/0").stdout.strip()


def test_failed_stage_is_not_tagged(repo) -> None:
    _, wt = repo
    transport = checkpointing_transport(lambda w: RawResult(None, exit_code=1, error="boom"))
    raw = transport(_work(cwd=str(wt), checkpoint_tag="task/r/t/implement/0"))
    assert raw.checkpoint is None
    assert not _git(wt, "tag", "-l", "task/r/t/implement/0").stdout.strip()


def test_tagging_failure_is_fail_open(tmp_path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    transport = checkpointing_transport(lambda w: RawResult({"ok": True}))
    raw = transport(_work(cwd=str(not_a_repo), checkpoint_tag="task/r/t/implement/0"))
    assert raw.exit_code == 0 and raw.error is None  # the stage still succeeds
    assert raw.checkpoint is None  # just no reset anchor


# --- resetting ----------------------------------------------------------------

def test_reset_gives_the_retry_a_clean_tree_at_the_anchor(repo) -> None:
    _, wt = repo
    _git(wt, "tag", "anchor")
    (wt / "f.txt").write_text("debris")  # tracked modification from the failed attempt
    (wt / "junk.tmp").write_text("x")  # untracked debris

    seen: dict = {}

    def inner(w: WorkItem) -> RawResult:
        seen["tracked"] = (wt / "f.txt").read_text()
        seen["untracked"] = (wt / "junk.tmp").exists()
        return RawResult({"ok": True})

    raw = checkpointing_transport(inner)(_work(cwd=str(wt), reset_to="anchor"))
    assert raw.exit_code == 0
    assert seen == {"tracked": "v1", "untracked": False}  # inner saw the anchor state


def test_reset_refuses_the_main_checkout(repo) -> None:
    main, _ = repo
    inner_called = []
    transport = checkpointing_transport(lambda w: inner_called.append(1) or RawResult({}))
    raw = transport(_work(cwd=str(main), reset_to="HEAD"))
    assert raw.exit_code == 1 and "not a linked git worktree" in raw.error
    assert not inner_called  # never dispatched over a tree we refused to prepare
    raw = transport(_work(cwd=None, reset_to="HEAD"))
    assert raw.exit_code == 1  # and never resets the process CWD


def test_reset_to_a_missing_ref_fails_the_dispatch(repo) -> None:
    _, wt = repo
    inner_called = []
    transport = checkpointing_transport(lambda w: inner_called.append(1) or RawResult({}))
    raw = transport(_work(cwd=str(wt), reset_to="no-such-tag"))
    assert raw.exit_code == 1 and "checkpoint reset failed" in raw.error
    assert not inner_called


# --- engine wiring --------------------------------------------------------------

def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_engine_names_tags_and_anchors_retries(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    intake = eng.next_work("r1", "t1")
    assert intake.checkpoint_tag == "task/r1/t1/intake/0"
    assert intake.reset_to is None  # first attempt: clean tree by construction
    eng.record("r1", make_result(intake, checkpoint={"tag": "task/r1/t1/intake/0", "sha": "aaa"}))

    scope = eng.next_work("r1", "t1")
    assert scope.checkpoint_tag is None  # scope is not git-affecting
    eng.record("r1", make_result(scope))

    impl = eng.next_work("r1", "t1")
    assert impl.checkpoint_tag == "task/r1/t1/implement/0" and impl.reset_to is None
    eng.record("r1", make_result(impl, status=ResultStatus.FAILURE, error="boom"))

    retry = eng.next_work("r1", "t1")
    assert retry.attempt == 1
    assert retry.checkpoint_tag == "task/r1/t1/implement/1"
    assert retry.reset_to == "task/r1/t1/intake/0"  # last SUCCESSFUL checkpoint


def test_crash_resume_of_a_checkpoint_stage_carries_the_anchor(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, checkpoint={"tag": "task/r1/t1/intake/0", "sha": "aaa"}))
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    eng.next_work("r1", "t1")  # implement dispatched, then "crash" (never recorded)
    fresh = Engine(eng.store, eng.ledger, eng.project)
    rerun = fresh.next_work("r1", "t1", resume=True)
    assert rerun.stage is Stage.IMPLEMENT and rerun.attempt == 0
    assert rerun.reset_to == "task/r1/t1/intake/0"  # re-run over a reset tree, never skip


def test_gate_vetoed_checkpoint_is_not_absorbed(tmp_path, project) -> None:
    # A test stage that reports tests_meaningful=false is vetoed to FAILURE by the
    # engine gate — its commits must not become a reset anchor.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    for _ in range(3):  # intake, scope, implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    before = eng.store.load_task("r1", "t1").last_checkpoint
    test_w = eng.next_work("r1", "t1")
    assert test_w.stage is Stage.TEST
    eng.record("r1", make_result(
        test_w,
        structured_output={"passed": True, "failures": [], "tests_meaningful": False},
        checkpoint={"tag": "task/r1/t1/test/0", "sha": "bbb"},
    ))
    assert eng.store.load_task("r1", "t1").last_checkpoint == before


def test_ids_are_sanitized_into_git_safe_tag_names(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("run 1", ExecutionLane.FULL)
    eng.add_task("run 1", "proj#551")
    w = eng.next_work("run 1", "proj#551")
    assert w.checkpoint_tag == "task/run-1/proj-551/intake/0"


def test_checkpoint_fields_do_not_change_the_content_hash() -> None:
    plain = _work()
    with_ckpt = _work(checkpoint_tag="task/r/t/implement/0", reset_to="task/r/t/intake/0")
    assert plain.content_hash == with_ckpt.content_hash  # bookkeeping, not content
