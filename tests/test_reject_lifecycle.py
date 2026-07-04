"""CLOSED_INFEASIBLE terminal state + rejection-reason surfaced end-to-end (#53, #52).

Engine.reject() closes a held task the human confirms is infeasible. This exercises:
  - the transition lands in the dedicated terminal ``CLOSED_INFEASIBLE`` state (NOT FAILED);
  - status() surfaces the reason, read back from the durable rejection artifact;
  - the batch circuit breaker never counts a rejection as a system failure;
  - the human-readable task index renders the reason;
  - the published rejection note carries the read-back reason;
  - old/legacy status docs (predating the enum member) still load.
"""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_task_index
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import (
    TERMINAL_TASK_STATES,
    ResultStatus,
    RunState,
    Stage,
    TaskState,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _hold_and_reject(eng: Engine, reason: str, *, by: str = "craig") -> None:
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="scope reported infeasible")
    eng.reject("r1", "t1", rejected_by=by, reason=reason)


def test_reject_lands_in_closed_infeasible_terminal_state(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _hold_and_reject(eng, "cannot be done without an upstream migration")
    task = eng.store.load_task("r1", "t1")
    # A dedicated terminal state — distinct from a generic execution FAILED (#53).
    assert task.state is TaskState.CLOSED_INFEASIBLE
    assert task.state is not TaskState.FAILED
    assert task.state in TERMINAL_TASK_STATES
    # Terminal ⇒ not dispatchable and no further work is emitted.
    assert eng.dispatchable("r1") == []
    assert eng.next_work("r1", "t1") is None
    # The run finalizes (does not stay open forever).
    assert eng.store.load_run("r1").state is RunState.FAILED
    assert eng.store.load_run("r1").progress().closed_infeasible == 1


def test_status_surfaces_the_rejection_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _hold_and_reject(eng, "depends on a third-party API that is being deprecated")
    st = eng.status("r1")["tasks"]["t1"]
    assert st["state"] == "closed_infeasible"
    # Read back from the durable rejection artifact (#52 — load_rejection now has a caller).
    assert st["rejection_reason"] == "depends on a third-party API that is being deprecated"
    assert st["stale"] is False  # a terminal task is never flagged stale


def test_task_index_renders_the_rejection_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    reason = "requires product decisions outside this task's scope"
    _hold_and_reject(eng, reason)
    index = (tmp_path / "stages" / "t1" / "index.md").read_text()
    assert "closed_infeasible" in index
    assert "Closed as infeasible" in index
    assert reason in index
    # The renderer itself is pure: no reason ⇒ no line (backward-compatible default).
    plain = render_task_index(eng.store.load_task("r1", "t1"))
    assert "Closed as infeasible" not in plain


def test_reject_publishes_a_note_with_the_readback_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    reason = "the feature was descoped upstream"
    _hold_and_reject(eng, reason, by="craig")
    notes = project.task_source.notes  # FakeTaskSource records publish_note calls
    assert len(notes) == 1
    body = notes[0]["body"]
    assert "closed as infeasible" in body.lower()
    assert reason in body  # the reason, round-tripped through load_rejection
    assert "craig" in body  # who closed it


def test_no_note_published_when_the_task_has_no_issue(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # A task with no issue to comment on: the note is skipped (but the close still happens).
    eng.store.update_task("r1", "t1", lambda t: setattr(t, "issue_number", None))
    eng.hold_for_approval("r1", "t1", what="infeasible")
    eng.reject("r1", "t1", rejected_by="craig", reason="no issue backing this task")
    assert project.task_source.notes == []
    assert eng.store.load_task("r1", "t1").state is TaskState.CLOSED_INFEASIBLE


def test_scheduler_breaker_does_not_count_a_rejection(tmp_path, project) -> None:
    # threshold=2 ⇒ it takes TWO consecutive genuine failures to pause. One real failure
    # PLUS one human rejection must stay UNDER the threshold: a deliberate close is not a
    # system failure and never advances the breaker (it is out-of-band, producing no
    # scheduler outcome at all).
    eng = _engine(tmp_path, project, max_attempts=1, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t_fail")
    eng.add_task("r1", "t_inf")
    # Human closes t_inf as infeasible, out of band, before scheduling.
    eng.hold_for_approval("r1", "t_inf", what="scope infeasible")
    eng.reject("r1", "t_inf", rejected_by="craig", reason="not worth doing")

    def runner(work):
        return [
            make_result(w, status=ResultStatus.FAILURE, error="boom")
            if w.stage is not Stage.INTAKE else make_result(w)
            for w in work
        ]

    sched = Scheduler(eng, max_concurrent=1, batch_failure_threshold=2)
    status = sched.run("r1", runner)

    # 1 genuine failure + 1 rejection == 1 counted failure < threshold 2 ⇒ never paused.
    assert status["run_state"] == "failed"
    assert [e for e in eng.store.read_events("r1") if e["type"] == "run_paused"] == []
    assert eng.store.load_task("r1", "t_fail").state is TaskState.FAILED
    assert eng.store.load_task("r1", "t_inf").state is TaskState.CLOSED_INFEASIBLE


def test_closed_infeasible_roundtrips_and_legacy_docs_still_load(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _hold_and_reject(eng, "infeasible")
    # The new terminal state persists and reloads through a FRESH store (no in-memory state).
    fresh = StatusStore(tmp_path)
    assert fresh.load_task("r1", "t1").state is TaskState.CLOSED_INFEASIBLE
    assert fresh.load_run("r1").progress().closed_infeasible == 1
    # Backward-compat: a legacy doc predating the enum member (no schema_version, an old
    # terminal state) must still validate — the enum only GAINED a member (additive).
    path = fresh._task_path("r1", "t1")
    legacy = json.loads(path.read_text())
    legacy.pop("schema_version", None)
    legacy["state"] = "failed"
    path.write_text(json.dumps(legacy))
    assert fresh.load_task("r1", "t1").state is TaskState.FAILED
