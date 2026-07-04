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

    # The event stream carries a dispatch_abandoned row for the audit trail.
    events = [e for e in eng.store.read_events("r1") if e["type"] == "dispatch_abandoned"]
    assert len(events) == 1
    assert events[0]["task_id"] == "t1"
    assert events[0]["stage"] == "scope"
    assert events[0]["reason"] == "orphaned"
    assert events[0]["disposition"] == "failed"


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
