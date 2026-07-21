"""Consolidation of ALL post-terminal effects onto the shared ``_finalize_task_terminal``
helper (#133) plus the two folded polish follow-ups (#131, #132).

``record``'s terminal-FAILURE branch used to re-implement the post-terminal effects
(``task_failed`` alert, cascade-block, port release, learnings harvest, finalize) inline —
a parallel copy of the helper the operator finalize paths (``reject``/``abandon``) already
use. That parallel copy was the last divergence point where the two paths could drift.
These tests pin that:

  - #133: ``record``'s terminal-failure routes through ``_finalize_task_terminal`` with
    ``disposition="failed"`` — the SINGLE source of truth — and still emits the alert +
    finalizes the run with the same reason as before;
  - #131: the helper's ``disposition`` parameter is statically typed
    ``Literal["rejected", "failed"]`` (a free static layer over the runtime guard);
  - #132: a REJECTED abandon no longer writes the task index inline before the helper — it
    is written exactly once, by ``_surface_rejection`` reading back the durable artifact.
"""

from __future__ import annotations

import typing

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, RunState, Stage, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _drive_to_implement(eng: Engine, run="r1", task="t1") -> None:
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake
    eng.record(run, make_result(eng.next_work(run, task)))  # scope


# --- #133: record()'s terminal-failure routes through the shared helper ---------------

def test_record_terminal_failure_routes_through_finalize_helper(tmp_path, project, monkeypatch) -> None:
    # The model-result terminal-failure path must delegate to _finalize_task_terminal (the
    # SAME helper reject/abandon use), not re-run the post-terminal effects inline.
    eng = _engine(tmp_path, project, max_attempts=1)
    _drive_to_implement(eng)

    calls: list[tuple[str, str]] = []
    real_finalize = eng._finalize_task_terminal

    def spy_finalize(run_id, task, *, disposition, reason):
        calls.append((disposition, reason))
        return real_finalize(run_id, task, disposition=disposition, reason=reason)

    monkeypatch.setattr(eng, "_finalize_task_terminal", spy_finalize)

    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.IMPLEMENT
    out = eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom",
                                       structured_output={}))

    assert out["outcome"] == "task_failed_max_attempts"
    assert out["task_state"] == "failed"
    # Delegated exactly once, with the failed disposition and the effective error as reason.
    assert calls == [("failed", "boom")]


def test_record_terminal_failure_still_emits_alert_and_finalizes(tmp_path) -> None:
    # Behavioural equivalence after the consolidation: the task_failed alert still fires with
    # the same reason, and the run still finalizes FAILED (the effects moved into the helper,
    # they did not disappear).
    project = FakeProject()
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    eng = _engine(tmp_path, project, max_attempts=1)
    _drive_to_implement(eng)

    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom",
                                 structured_output={}))

    failed = next(p for k, p in calls if k == "task_failed")
    assert failed["task_id"] == "t1"
    assert failed["reason"] == "boom"  # effective.error, unchanged by the routing
    assert failed["stage"] == "implement"
    assert "run_finalized" in [k for k, _ in calls]
    assert eng.store.load_run("r1").state is RunState.FAILED


def test_record_completion_still_finalizes_without_the_failed_alert(tmp_path) -> None:
    # The non-failure branch is untouched by the #133 split: a clean run completes, marks the
    # task complete, finalizes, and never emits a task_failed alert.
    project = FakeProject()
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))

    kinds = [k for k, _ in calls]
    assert "task_failed" not in kinds
    assert "run_finalized" in kinds
    assert eng.store.load_run("r1").state is RunState.COMPLETED
    assert [tid for tid, _pr in project.task_source.completed] == ["t1"]  # mark_complete ran


# --- #184: the failed-alert reason is the caller's reason, not a stale last_error -------

def test_finalize_failed_alert_reason_ignores_stale_last_error(tmp_path) -> None:
    # #184: task.last_error can still hold an earlier review-rejection message
    # (_apply_review_rejection sets it and never clears it). The task_failed alert must
    # report the reason for THIS terminal transition — the caller's ``reason`` — not the
    # stale last_error, so a max-attempts death is not misreported as the prior rejection.
    project = FakeProject()
    calls: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: calls.append((kind, payload))
    eng = _engine(tmp_path, project)
    _drive_to_implement(eng)

    task = eng.store.load_task("r1", "t1")
    task.last_error = "review rejected: some earlier blocking issue"  # the stale value
    eng.store.save_task(task)

    eng._finalize_task_terminal(
        "r1", task, disposition="failed", reason="implement failed: fresh runner error"
    )

    failed = next(p for k, p in calls if k == "task_failed")
    assert failed["reason"] == "implement failed: fresh runner error"
    assert "review rejected" not in failed["reason"]  # the stale last_error did NOT leak


# --- #213: begin_stage clears the task-level last_error at each fresh attempt ----------

def test_begin_stage_clears_stale_last_error() -> None:
    # #213 (root-cause follow-up to #184): nothing cleared task.last_error, so a stale
    # prior-attempt / review-rejection error could leak into a later reader across a
    # review-fix cycle. begin_stage now resets it at the start of every fresh stage attempt;
    # apply_result re-sets it only if THIS attempt fails.
    from orchestrator.schemas.status import Task
    from orchestrator.state_machine import begin_stage

    task = Task(task_id="t1", run_id="r1", created_at="t0", updated_at="t0")
    task.last_error = "review rejected: some earlier blocking issue"  # stale from a prior cycle

    begin_stage(task, Stage.IMPLEMENT, now="t1", model="claude-opus-4-8", effort="high")

    assert task.last_error is None  # cleared at attempt start; fails if the reset regresses


# --- #131: the helper's disposition parameter is a Literal ----------------------------

def test_finalize_helper_disposition_is_literal() -> None:
    hints = typing.get_type_hints(Engine._finalize_task_terminal)
    disposition = hints["disposition"]
    assert typing.get_origin(disposition) is typing.Literal
    assert set(typing.get_args(disposition)) == {"rejected", "failed"}


# --- #132: a rejected abandon writes the task index exactly once (via the helper) ------

def _mid_dispatch(eng: Engine, run="r1", task="t1") -> None:
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake
    eng.next_work(run, task)  # scope dispatched, NOT recorded — a held lease


def test_rejected_abandon_writes_task_index_once_via_surface_rejection(tmp_path, project, monkeypatch) -> None:
    # #132: the inline write_task_index on the rejected-abandon path is dropped — the index is
    # rendered exactly once, by _surface_rejection reading the reason back from the durable
    # artifact (the prior inline write was immediately overwritten with identical content).
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)

    index_writes: list[str] = []
    real_write = eng.store.write_task_index

    def spy_write(task_id, content):
        index_writes.append(task_id)
        return real_write(task_id, content)

    monkeypatch.setattr(eng.store, "write_task_index", spy_write)

    reason = "run walked away; closing as infeasible"
    task = eng.abandon("r1", "t1", reason=reason, disposition="rejected")

    assert task.state is TaskState.CLOSED_INFEASIBLE
    assert index_writes == ["t1"]  # written once — not the old inline-then-overwrite double
    # And the single render carries the round-tripped rejection reason.
    assert eng.status("r1")["tasks"]["t1"]["rejection_reason"] == reason


def test_failed_abandon_still_writes_task_index_inline(tmp_path, project, monkeypatch) -> None:
    # The FAILED abandon path has no _surface_rejection, so it must KEEP rendering the index
    # inline (the #132 skip is scoped to the rejected disposition only).
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)

    index_writes: list[str] = []
    real_write = eng.store.write_task_index

    def spy_write(task_id, content):
        index_writes.append(task_id)
        return real_write(task_id, content)

    monkeypatch.setattr(eng.store, "write_task_index", spy_write)

    task = eng.abandon("r1", "t1", reason="orphaned")  # disposition defaults to 'failed'

    assert task.state is TaskState.FAILED
    assert index_writes == ["t1"]  # still rendered exactly once on the failed path
