"""Sanctioned abandon path for a run killed mid-dispatch (#82).

Engine.abandon() finalizes a task that is holding an outstanding dispatch lease
(``pending_work_item_id``) because its run died mid-dispatch — the operator need the
contract guards (record/hold/reject all correctly refuse) otherwise force through a
hand-crafted synthetic StageResult. This exercises:
  - the lease is released and the task reaches a terminal state (FAILED / CLOSED_INFEASIBLE);
  - a rejected abandon writes the durable rejection artifact, read back by status();
  - a $0 cost row + a stage log with outcome ``dispatch_abandoned`` are written;
  - a ``dispatch_abandoned`` event is emitted and the run finalizes;
  - the liveness guard refuses while the provider stream looks alive, and --force overrides;
  - abandoning with no outstanding dispatch / on a terminal task raises ContractError.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import (
    TERMINAL_TASK_STATES,
    RunState,
    Stage,
    StageStatus,
    TaskState,
)
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir, stream_filename
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _mid_dispatch(eng: Engine, *, run: str = "r1", task: str = "t1") -> Stage:
    """Drive a task to an OUTSTANDING dispatch on the scope stage (lease held, no result
    recorded) — the exact zombie state a killed-mid-dispatch run leaves behind."""
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake completes
    work = eng.next_work(run, task)  # scope dispatched; pending_work_item_id set, NOT recorded
    assert work.stage is Stage.SCOPE
    doc = eng.store.load_task(run, task)
    assert doc.pending_work_item_id is not None
    return work.stage


def test_abandon_releases_lease_and_reaches_failed_terminal(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    task = eng.abandon("r1", "t1", reason="supervisor killed; machine rebooted")

    assert task.state is TaskState.FAILED
    assert task.state in TERMINAL_TASK_STATES
    # The lease is released — nothing is left outstanding.
    assert task.pending_work_item_id is None
    assert task.pending_content_hash is None
    # The dispatched stage is folded to FAILED with the abandon reason.
    assert task.stages[Stage.SCOPE].status is StageStatus.FAILED
    assert "dispatch_abandoned" in (task.stages[Stage.SCOPE].error or "")
    # Terminal ⇒ not dispatchable; the run finalizes FAILED (an execution death).
    assert eng.dispatchable("r1") == []
    assert eng.next_work("r1", "t1") is None
    assert eng.store.load_run("r1").state is RunState.FAILED


def test_abandon_rejected_reaches_closed_infeasible_and_writes_rejection(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    reason = "run walked away; the task is being closed as infeasible"
    task = eng.abandon("r1", "t1", reason=reason, disposition="rejected")

    assert task.state is TaskState.CLOSED_INFEASIBLE
    assert task.state in TERMINAL_TASK_STATES
    # The durable rejection artifact IS the gate record — read it BACK (round-trips like reject).
    record = eng.store.load_rejection("r1", "t1")
    assert record is not None
    assert record["reason"] == reason
    assert record["rejected_by"] == "abandon"
    # status() surfaces the reason read back from the artifact.
    st = eng.status("r1")["tasks"]["t1"]
    assert st["state"] == "closed_infeasible"
    assert st["rejection_reason"] == reason
    # A rejection-only run rolls up to the honest non-failure terminal (#67).
    assert eng.store.load_run("r1").state is RunState.COMPLETED_WITH_REJECTIONS


def test_abandon_writes_zero_cost_row_and_dispatch_abandoned_stage_log(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    task = eng.abandon("r1", "t1", reason="orphaned")

    # A $0 cost-ledger row was written for the abandoned dispatch (honest: no model ran).
    scope_rows = [r for r in eng.ledger.rows() if r["stage"] == "scope"]
    assert len(scope_rows) == 1
    assert scope_rows[0]["cost_usd"] == 0.0
    assert scope_rows[0]["input_tokens"] == 0 and scope_rows[0]["output_tokens"] == 0

    # A per-stage log with outcome dispatch_abandoned at $0 sits under the task's stages dir.
    seq = task.stage_counter
    log = json.loads(
        (tmp_path / "stages" / "t1" / f"{seq:02d}-scope.json").read_text()
    )
    assert log["outcome"] == "dispatch_abandoned"
    assert log["cost_usd"] == 0.0
    assert "orphaned" in (log["raw_output"] or "")
    assert (tmp_path / "stages" / "t1" / f"{seq:02d}-scope.md").exists()

    # #151: the stage log surfaces the effort attribution field, agreeing with the
    # cost-ledger row (both source it from the same synthetic result).
    assert "effort" in log
    assert log["effort"] == scope_rows[0]["effort"]

    # The event stream carries a dispatch_abandoned row for the audit trail.
    events = [e for e in eng.store.read_events("r1") if e["type"] == "dispatch_abandoned"]
    assert len(events) == 1
    assert events[0]["task_id"] == "t1"
    assert events[0]["stage"] == "scope"
    assert events[0]["reason"] == "orphaned"
    assert events[0]["disposition"] == "failed"


def test_abandon_attributes_the_dispatched_effort_on_the_cost_row(tmp_path, project) -> None:
    # #138: an abandoned dispatch echoes its model onto the $0 cost row — effort must ride
    # along symmetrically. begin_stage persists the dispatched effort on the stage record, so
    # the synthetic StageResult can read it back even though no runner echoed a result.
    eng = _engine(tmp_path, project)
    stage = _mid_dispatch(eng)  # scope dispatched at the spec-default effort ("high")
    # The lease is outstanding: the stage record already carries the dispatched effort.
    assert eng.store.load_task("r1", "t1").stages[stage].effort == "high"

    eng.abandon("r1", "t1", reason="orphaned")

    scope_rows = [r for r in eng.ledger.rows() if r["stage"] == "scope"]
    assert len(scope_rows) == 1
    # Effort is attributed on the abandoned row, not silently dropped to None.
    assert scope_rows[0]["effort"] == "high"


def test_abandon_liveness_guard_refuses_when_stream_recent_and_force_overrides(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    # Simulate a still-live dispatch: its provider stream file just grew (default mtime = now).
    d = stages_dir(eng.store.root, "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / stream_filename("scope", 0)).write_text('{"type":"assistant"}\n')

    with pytest.raises(ContractError, match="appears alive"):
        eng.abandon("r1", "t1", reason="looks dead but stream just grew")
    # The lease is untouched by a refused abandon.
    assert eng.store.load_task("r1", "t1").pending_work_item_id is not None

    # --force overrides the liveness guard (the operator knows the process is dead).
    task = eng.abandon("r1", "t1", reason="confirmed dead", force=True)
    assert task.state is TaskState.FAILED
    assert task.pending_work_item_id is None


def test_abandon_liveness_guard_passes_when_stream_idle_past_window(tmp_path, project) -> None:
    import os
    import time

    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    d = stages_dir(eng.store.root, "t1")
    d.mkdir(parents=True, exist_ok=True)
    stream = d / stream_filename("scope", 0)
    stream.write_text('{"type":"assistant"}\n')
    # Age the stream well past the idle window — the dispatch is provably dead.
    old = time.time() - 10_000
    os.utime(stream, (old, old))

    task = eng.abandon("r1", "t1", reason="idle past window", min_idle_s=300)
    assert task.state is TaskState.FAILED
    # The event records how long the stream had been idle (last_event_at present).
    ev = [e for e in eng.store.read_events("r1") if e["type"] == "dispatch_abandoned"][0]
    assert ev["stream_last_grew"] is not None


def test_abandon_with_no_outstanding_dispatch_raises(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # Fresh task, nothing dispatched — nothing to abandon.
    with pytest.raises(ContractError, match="no outstanding dispatch"):
        eng.abandon("r1", "t1", reason="nothing leased")


def test_abandon_on_a_terminal_task_raises(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    eng.abandon("r1", "t1", reason="first abandon")  # -> FAILED (terminal)
    with pytest.raises(ContractError, match="terminal"):
        eng.abandon("r1", "t1", reason="already gone")


def test_abandon_unknown_disposition_raises(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    with pytest.raises(ContractError, match="disposition"):
        eng.abandon("r1", "t1", reason="bad arg", disposition="closed")


def _task_failed_notifications(eng: Engine, run: str = "r1") -> list[dict]:
    return [
        e for e in eng.store.read_events(run)
        if e["type"] == "notification" and e.get("kind") == "task_failed"
    ]


def test_abandon_failed_emits_task_failed_notification(tmp_path, project) -> None:
    # #110/#107: the failed abandon path now fires the SAME task_failed alert record()'s
    # terminal-failure path emits (previously abandon silently skipped it). The notification
    # audit row is always appended even when no notify hook is installed.
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    eng.abandon("r1", "t1", reason="orphaned")

    notes = _task_failed_notifications(eng)
    assert len(notes) == 1
    assert notes[0]["task_id"] == "t1"
    assert notes[0]["stage"] == "scope"  # the abandoned dispatch's stage
    assert "orphaned" in notes[0]["reason"]  # last_error carries the abandon reason


def test_abandon_failed_calls_notify_hook_when_installed(tmp_path, project) -> None:
    # The duck-typed notify(kind, payload) hook (the old monitor's email/desktop seam) is
    # invoked on the failed abandon, not just the audit row.
    seen: list[tuple[str, dict]] = []
    project.notify = lambda kind, payload: seen.append((kind, payload))  # type: ignore[attr-defined]
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    eng.abandon("r1", "t1", reason="orphaned")

    task_failed = [payload for kind, payload in seen if kind == "task_failed"]
    assert len(task_failed) == 1
    assert task_failed[0]["task_id"] == "t1"


def test_abandon_rejected_surfaces_rejection_and_publishes_note(tmp_path, project) -> None:
    # #110/#109: a rejected abandon now routes through _surface_rejection — it publishes a
    # rejection note to the task source with the reason READ BACK from the durable artifact
    # (not just an inline task_index write), exactly as reject() does.
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    reason = "run walked away; closing as infeasible"
    eng.abandon("r1", "t1", reason=reason, disposition="rejected")

    # A note was published, carrying the round-tripped reason and who closed it.
    notes = project.task_source.notes
    assert len(notes) == 1
    body = notes[0]["body"]
    assert reason in body  # read back from load_rejection, not the in-hand arg
    assert "infeasible" in body.lower()
    # The publish is audited.
    published = [e for e in eng.store.read_events("r1") if e["type"] == "rejection_note_published"]
    assert len(published) == 1
    # A deliberate human close is NOT an execution failure ⇒ no task_failed alert (mirrors
    # reject()'s silence).
    assert _task_failed_notifications(eng) == []


def test_abandon_applies_mutation_via_locked_update_task(tmp_path, project, monkeypatch) -> None:
    # #108: the terminal transition is applied through the per-task locked read-modify-write
    # (store.update_task on the FRESH doc), not a bare load_task + in-place mutate + save_task
    # that could clobber a concurrent writer.
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)

    update_calls: list[tuple[str, str]] = []
    save_calls: list[str] = []
    real_update = eng.store.update_task
    real_save = eng.store.save_task

    def spy_update(run_id, task_id, mutator):
        update_calls.append((run_id, task_id))
        return real_update(run_id, task_id, mutator)

    def spy_save(task):
        save_calls.append(task.task_id)
        return real_save(task)

    monkeypatch.setattr(eng.store, "update_task", spy_update)
    monkeypatch.setattr(eng.store, "save_task", spy_save)

    task = eng.abandon("r1", "t1", reason="orphaned")

    # The mutation went through the locked update_task path...
    assert ("r1", "t1") in update_calls
    # ...and NOT through a bare save_task (the old load+mutate+save escape is gone).
    assert save_calls == []
    # The transition still landed atomically: lease cleared, terminal state, counter bumped.
    assert task.state is TaskState.FAILED
    assert task.pending_work_item_id is None
    assert task.stage_counter >= 1
    # The persisted doc reflects the same transition (read-modify-write committed under lock).
    persisted = eng.store.load_task("r1", "t1")
    assert persisted.state is TaskState.FAILED
    assert persisted.pending_work_item_id is None


def test_cli_abandon_releases_lease_and_finalizes(tmp_path, capsys) -> None:
    from orchestrator.cli import main
    from orchestrator.schemas.work import WorkItem

    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]

    def run(*argv):
        rc = main(list(argv))
        assert rc == 0
        out = capsys.readouterr().out.strip()
        return json.loads(out) if out and out != "null" else None

    run(*base, "init-run", "--lane", "full")
    run(*base, "add-task", "--task", "#42")
    # Drain intake, then dispatch scope and DO NOT record it (simulate a killed supervisor).
    work = run(*base, "next", "--task", "#42")  # scope WorkItem
    assert WorkItem.model_validate(work).stage is Stage.SCOPE

    out = run(*base, "abandon", "--task", "#42", "--reason", "supervisor killed")
    assert out["abandoned"] == "#42"
    assert out["state"] == "failed"
    assert out["disposition"] == "failed"

    status = run(*base, "status")
    assert status["tasks"]["#42"]["state"] == "failed"
    assert status["run_state"] == "failed"
