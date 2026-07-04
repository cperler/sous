"""Salvage committed work on a timeout/infra failure instead of resetting it away (#59).

The split under test mirrors the checkpoint protocol it extends: the transport wrapper
owns the git READ (report the commits a failed attempt left past the anchor); the engine
owns the STATE decision (keep by failure kind, cap the keeps, suppress the reset). A
plain FAILURE (real test failure) still resets — the committed code may BE the defect.
"""

from __future__ import annotations

import subprocess

import pytest

from adapters.execution.transport import (
    RawResult,
    _salvageable_commits,
    checkpointing_transport,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.status import Task
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

ANCHOR = {"tag": "anchor-tag", "sha": "0" * 40}


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path):
    """A main checkout plus a linked worktree with an ``anchor`` tag at the baseline."""
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
    _git(wt, "tag", "anchor")
    return main, wt


def _commit(wt, name, body, subject) -> None:
    (wt / name).write_text(body)
    _git(wt, "add", ".")
    _git(wt, "commit", "-qm", subject)


def _fail_work(*, cwd, anchor="anchor") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
        schema_ref="implement", model="m", created_at="now", cwd=cwd,
        salvage_anchor=anchor,
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _drive_to_implement(eng) -> WorkItem:
    """Run a LITE task through intake (so a baseline checkpoint anchor exists) and hand
    back the dispatched IMPLEMENT WorkItem."""
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    assert intake.stage is Stage.INTAKE
    eng.record("r1", make_result(intake, checkpoint=ANCHOR))  # sets task.last_checkpoint
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.IMPLEMENT
    assert work.salvage_anchor == ANCHOR["tag"]  # anchor threaded to the runner
    return work


def _salvage(count=2) -> dict:
    commits = [
        {"sha": "abc123def0aaa", "subject": "add core feature"},
        {"sha": "def456abc0bbb", "subject": "wip tests"},
    ][:count]
    return {"anchor": ANCHOR["tag"], "count": count, "commits": commits}


# --- transport: the git read (report commits past the anchor) -----------------

def test_transport_reports_committed_work_on_a_failed_attempt(repo) -> None:
    _, wt = repo
    _commit(wt, "feature.py", "code", "implement the feature")
    transport = checkpointing_transport(lambda w: RawResult(None, exit_code=124, error="timed out"))
    raw = transport(_fail_work(cwd=str(wt)))
    assert raw.salvage is not None
    assert raw.salvage["count"] == 1
    assert raw.salvage["commits"][0]["subject"] == "implement the feature"
    assert len(raw.salvage["commits"][0]["sha"]) == 40  # full sha


def test_transport_ignores_uncommitted_scraps(repo) -> None:
    _, wt = repo
    (wt / "dirty.py").write_text("un-vetted scrap")  # dirty, never committed
    _git(wt, "add", "dirty.py")
    transport = checkpointing_transport(lambda w: RawResult(None, exit_code=124, error="timed out"))
    raw = transport(_fail_work(cwd=str(wt)))
    assert raw.salvage is None  # salvage is COMMITTED work only


def test_transport_reports_nothing_when_no_commits(repo) -> None:
    _, wt = repo
    transport = checkpointing_transport(lambda w: RawResult(None, exit_code=124, error="timed out"))
    raw = transport(_fail_work(cwd=str(wt)))
    assert raw.salvage is None


def test_transport_does_not_report_salvage_on_success(repo) -> None:
    _, wt = repo
    _commit(wt, "feature.py", "code", "done")
    transport = checkpointing_transport(lambda w: RawResult({"ok": True}))
    raw = transport(_fail_work(cwd=str(wt)))
    assert raw.salvage is None  # a success is checkpointed, not salvaged


def test_salvageable_commits_is_fail_open_on_a_bad_anchor(repo) -> None:
    _, wt = repo
    assert _salvageable_commits(str(wt), "no-such-ref") is None  # git error -> None, not a raise


# --- engine: the state decision (keep / discard / cap) ------------------------

def test_timeout_with_commits_keeps_the_work(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng)
    out = eng.record("r1", make_result(
        work, status=ResultStatus.TIMEOUT, error="timed out after 300s", salvage=_salvage()))
    assert out["outcome"] == "stage_failed_will_retry"

    t = eng.store.load_task("r1", "t1")
    assert t.salvage_count == 1
    assert t.salvage_in_place is True
    learning = t.learnings[-1]
    assert "KEPT" in learning
    assert "abc123def" in learning and "add core feature" in learning  # sha + subject
    assert "def456abc" in learning and "wip tests" in learning

    kept = [e for e in eng.store.read_events("r1") if e["type"] == "salvage_kept"]
    assert len(kept) == 1
    assert kept[0]["kind"] == "timeout"
    assert kept[0]["shas"] == ["abc123def", "def456abc"]  # bounded to 9 chars

    # the retry is dispatched WITHOUT a reset (work stays in place)
    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.IMPLEMENT
    assert retry.reset_to is None
    # ...and the flag was consumed by the dispatch
    assert eng.store.load_task("r1", "t1").salvage_in_place is False


def test_test_failure_with_commits_is_reset_not_salvaged(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng)
    # a plain FAILURE (the committed code may BE the defect) — not a salvageable kind
    out = eng.record("r1", make_result(
        work, status=ResultStatus.FAILURE, error="assertion failed", salvage=_salvage()))
    assert out["outcome"] == "stage_failed_will_retry"

    t = eng.store.load_task("r1", "t1")
    assert t.salvage_count == 0
    assert t.salvage_in_place is False

    discarded = [e for e in eng.store.read_events("r1") if e["type"] == "salvage_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["reason"] == "kind_not_salvageable"

    # the retry resets to the checkpoint anchor (work discarded)
    retry = eng.next_work("r1", "t1")
    assert retry.reset_to == ANCHOR["tag"]


def test_second_consecutive_salvage_hits_the_cap_and_resets(tmp_path, project) -> None:
    # distinct error strings keep the breaker from tripping on identical signatures,
    # so the cap (not the breaker) is what's under test.
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng)
    eng.record("r1", make_result(
        work, status=ResultStatus.TIMEOUT, error="timed out after 300s", salvage=_salvage()))
    assert eng.store.load_task("r1", "t1").salvage_count == 1

    retry = eng.next_work("r1", "t1")
    assert retry.reset_to is None  # first salvage kept the work
    out = eng.record("r1", make_result(
        retry, status=ResultStatus.TIMEOUT, error="timed out after 360s", salvage=_salvage()))
    assert out["outcome"] == "stage_failed_will_retry"

    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is False  # budget spent -> discard
    discarded = [e for e in eng.store.read_events("r1") if e["type"] == "salvage_discarded"]
    assert discarded[-1]["reason"] == "budget_exhausted"

    # the third attempt resets fully (no infinite pile of half-work)
    third = eng.next_work("r1", "t1")
    assert third.reset_to == ANCHOR["tag"]


def test_timeout_with_no_commits_is_a_plain_reset(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng)
    # a timeout that committed nothing (salvage report is None) resets silently
    out = eng.record("r1", make_result(
        work, status=ResultStatus.TIMEOUT, error="timed out after 300s", salvage=None))
    assert out["outcome"] == "stage_failed_will_retry"

    t = eng.store.load_task("r1", "t1")
    assert t.salvage_count == 0
    assert t.salvage_in_place is False
    events = eng.store.read_events("r1")
    assert not [e for e in events if e["type"].startswith("salvage_")]  # no salvage noise

    retry = eng.next_work("r1", "t1")
    assert retry.reset_to == ANCHOR["tag"]  # plain reset


def test_a_clean_stage_refreshes_the_salvage_budget(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng)
    eng.record("r1", make_result(
        work, status=ResultStatus.TIMEOUT, error="timed out after 300s", salvage=_salvage()))
    assert eng.store.load_task("r1", "t1").salvage_count == 1
    retry = eng.next_work("r1", "t1")
    eng.record("r1", make_result(retry, checkpoint={"tag": "impl", "sha": "1" * 40}))
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_count == 0  # a clean stage refreshed the keep budget
    assert t.salvage_in_place is False


def test_salvage_counters_serialize_on_task(tmp_path) -> None:
    store = StatusStore(tmp_path)
    task = Task(
        task_id="t1", run_id="r1", created_at="now", updated_at="now",
        title="x", body="y", execution_lane=ExecutionLane.LITE,
        salvage_count=2, salvage_in_place=True,
    )
    store.save_task(task)
    reloaded = store.load_task("r1", "t1")
    assert reloaded.salvage_count == 2
    assert reloaded.salvage_in_place is True
