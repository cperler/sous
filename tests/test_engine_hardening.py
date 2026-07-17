"""Regression tests for the code-review fixes (findings 1-9)."""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage, StageStatus
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


# Finding 2 — replayed StageResult rejected once no dispatch is outstanding
def test_replayed_result_rejected(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")
    result = make_result(work)
    eng.record("r1", result)  # first time: ok, clears pending
    with pytest.raises(ContractError):
        eng.record("r1", result)  # replay: no outstanding dispatch -> rejected


# Finding 9 — the issue body reaches the rendered prompt
def test_body_reaches_prompt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    scope = eng.next_work("r1", "t1")
    assert scope.stage is Stage.SCOPE
    assert "do it" in scope.prompt  # FakeTaskSource body


# Finding 8 — the agent persona is resolved onto the WorkItem
def test_agent_persona_on_workitem(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(2):  # advance to implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    impl = eng.next_work("r1", "t1")
    assert impl.stage is Stage.IMPLEMENT
    assert impl.agent == "impl-agent"  # FakeProject.agent_for(IMPLEMENT, "implement")


# Finding 3 — a crash mid-stage does NOT reset the attempt counter
def test_crash_does_not_reset_attempt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    w1 = eng.next_work("r1", "t1")  # implement attempt 0
    eng.record("r1", make_result(w1, status=ResultStatus.FAILURE, error="boom", structured_output={}))
    w2 = eng.next_work("r1", "t1")  # retry -> attempt 1
    assert w2.attempt == 1
    # simulate a crash: w2 dispatched (RUNNING) but never recorded. A normal
    # next_work now refuses (the lease is held); recovery is the explicit resume path.
    with pytest.raises(ContractError):
        eng.next_work("r1", "t1")
    w3 = eng.next_work("r1", "t1", resume=True)
    assert w3.stage is Stage.IMPLEMENT and w3.attempt == 1  # SAME attempt, not reset to 0


# Finding 6 + 7 — transitive cascade wired; multi-task run finalizes
def test_failed_task_cascade_blocks_dependent_and_run_fails(tmp_path, project) -> None:
    project.task_source.deps = {"B": ["A"]}
    eng = _engine(tmp_path, project, max_attempts=1)
    eng.create_run("r1")
    eng.add_task("r1", "A")
    eng.add_task("r1", "B")  # depends on A
    # fail A on its first stage (max_attempts=1 -> immediate fail)
    wA = eng.next_work("r1", "A")
    out = eng.record("r1", make_result(wA, status=ResultStatus.FAILURE, error="nope", structured_output={}))
    assert out["task_state"] == "failed"

    status = eng.status("r1")
    assert status["tasks"]["B"]["state"] == "cascade_blocked"  # D14 transitive cascade wired
    assert status["run_state"] == "failed"  # multi-task run finalizes (not stuck RUNNING)


def test_multi_task_run_completes(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.MICRO)
    eng.add_task("r1", "A")
    eng.add_task("r1", "B")
    for t in ("A", "B"):
        while (w := eng.next_work("r1", t)) is not None:
            eng.record("r1", make_result(w))
    assert eng.status("r1")["run_state"] == "completed"  # not stuck at len<=1 bug


# #142 — a normal (non-resume) dispatch leaves no orphan stage_dispatched: every
# dispatched work_item_id gets a matching stage_recorded, and no lease is superseded.
def test_normal_dispatch_leaves_no_orphan_stage_dispatched(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    events = eng.store.read_events("r1")
    dispatched = [e for e in events if e["type"] == "stage_dispatched"]
    recorded = [e for e in events if e["type"] == "stage_recorded"]
    # one dispatch per recorded stage: no superseded lease inflates the dispatch count
    assert len(dispatched) == len(recorded)
    assert not [e for e in events if e["type"] == "lease_superseded"]
    assert not [e for e in dispatched if e.get("resume")]


# #142 — a resume re-lease is self-describing: the re-dispatch carries the resume marker
# + the lease it supersedes, and the superseded (never-recorded) lease gets its own
# `lease_superseded` event so a naive consumer can discount it.
def test_resume_release_marks_superseded_lease(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    w1 = eng.next_work("r1", "t1")  # implement dispatched, then "crash" (never recorded)
    w2 = eng.next_work("r1", "t1", resume=True)  # supervisor resumes -> fresh WorkItem
    assert w2.id != w1.id

    events = eng.store.read_events("r1")
    superseded = [e for e in events if e["type"] == "lease_superseded"]
    assert len(superseded) == 1
    assert superseded[0]["work_item_id"] == w1.id  # the retired, never-recorded lease
    assert superseded[0]["superseded_by"] == w2.id
    # the re-dispatch event is stamped as a resume of the old lease
    redispatch = [
        e for e in events
        if e["type"] == "stage_dispatched" and e["work_item_id"] == w2.id
    ]
    assert len(redispatch) == 1
    assert redispatch[0].get("resume") is True
    assert redispatch[0].get("supersedes") == w1.id
    # the FIRST (superseded) dispatch was NOT retroactively marked as a resume
    first = [e for e in events if e["type"] == "stage_dispatched" and e["work_item_id"] == w1.id]
    assert len(first) == 1 and not first[0].get("resume")


# Finding (cleanup) — single pricing source: ledger and engine agree
def test_single_pricing_source(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    assert eng.ledger.model_table is eng.models


# begin_stage clears the crash marker so a re-dispatched RUNNING stage is detectable
def test_begin_stage_clears_completed_at_on_retry(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake done
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope done
    w = eng.next_work("r1", "t1")  # implement
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="x", structured_output={}))
    eng.next_work("r1", "t1")  # retry dispatched, not recorded
    task = eng.store.load_task("r1", "t1")
    rec = task.stages[Stage.IMPLEMENT]
    assert rec.status is StageStatus.RUNNING and rec.completed_at is None  # valid crash marker
