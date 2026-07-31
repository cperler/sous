"""Retire a superseded run (#257).

The gap: a run the human deliberately SUPERSEDED (rebuilt as a successor run) had no
sanctioned way to reach terminal. ``abandon`` needs an outstanding lease a cleanly-recorded
run does not have; ``abandon --disposition rejected`` and ``reject`` publish a "closed
infeasible" note to a GitHub issue that is LIVE in the successor run; ``hold`` only
silences the stale alarms while the run stays ``running`` forever. So the run permanently
occupied the monitor's "needs you" list — the one signal that must stay unignorable.

These tests freeze the three properties that distinguish ``retire`` from every peer path:
it needs no lease, it mutates no task source, and it actually reaches terminal.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.alerting import stale_notifications
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import _progress_str
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
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _clean_run(eng: Engine, *, run: str = "r1", tasks: tuple[str, ...] = ("t1",)) -> None:
    """A run whose tasks are mid-pipeline but QUIESCENT — every dispatch recorded, no lease
    outstanding. The common superseded case: the human recorded a stage, read the result,
    and decided to rebuild the batch elsewhere."""
    eng.create_run(run)
    for task in tasks:
        eng.add_task(run, task)
        eng.record(run, make_result(eng.next_work(run, task)))  # intake completes
    for task in tasks:
        assert eng.store.load_task(run, task).pending_work_item_id is None


def _mid_dispatch(eng: Engine, *, run: str = "r1", task: str = "t1") -> Stage:
    """Drive a task to an OUTSTANDING dispatch (lease held, no result recorded)."""
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake completes
    work = eng.next_work(run, task)  # scope dispatched, never recorded
    assert eng.store.load_task(run, task).pending_work_item_id is not None
    return work.stage


def _events(eng: Engine, run_id: str, type_: str) -> list[dict]:
    return [e for e in eng.store.read_events(run_id) if e.get("type") == type_]


# --- the core transition ------------------------------------------------------------


def test_retire_needs_no_lease_and_drives_every_task_terminal(tmp_path, project) -> None:
    """The property that rules out ``abandon``: a cleanly-recorded run has NO lease, and
    retiring it must still work."""
    eng = _engine(tmp_path, project)
    _clean_run(eng, tasks=("t1", "t2"))

    run = eng.retire("r1", reason="issue body amended; rebuilt as r2", retired_by="craig",
                     superseded_by="r2")

    assert run.state is RunState.SUPERSEDED
    for task_id in ("t1", "t2"):
        task = eng.store.load_task("r1", task_id)
        assert task.state is TaskState.SUPERSEDED
        assert task.state in TERMINAL_TASK_STATES
    # Terminal ⇒ the run stops producing work and stops occupying the operator's attention.
    assert eng.dispatchable("r1") == []
    assert eng.next_work("r1", "t1") is None
    assert run.progress().superseded == 2


def test_retire_leaves_already_terminal_tasks_with_their_own_outcome(tmp_path, project) -> None:
    """A task that genuinely finished before the run was retired keeps its honest result —
    retiring re-labels only what never got to finish."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake completes
    eng.next_work("r1", "t1")  # scope dispatched, never recorded
    eng.abandon("r1", "t1", reason="provider process killed")  # t1 -> FAILED
    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED

    eng.retire("r1", reason="superseded by r2", retired_by="craig", superseded_by="r2")

    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED  # untouched
    assert eng.store.load_task("r1", "t2").state is TaskState.SUPERSEDED


def test_a_held_task_is_retireable_without_a_rejection(tmp_path, project) -> None:
    """``hold`` was the least-wrong workaround, so a run full of held tasks is exactly the
    state retire inherits. Only ``reject`` could previously move those — and it publishes."""
    eng = _engine(tmp_path, project)
    _clean_run(eng)
    eng.hold_for_approval("r1", "t1", what="scope inverted the identity rule")
    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    assert eng.store.load_task("r1", "t1").state is TaskState.SUPERSEDED
    assert eng.store.load_rejection("r1", "t1") is None
    assert project.task_source.notes == []


# --- the defining property: no task-source mutation ---------------------------------


def test_retire_publishes_nothing_to_the_task_source(tmp_path, project) -> None:
    """THE property that rules out reject / abandon --rejected: a superseded run's issues
    are live in the successor run, so "closed infeasible" on them would be actively wrong."""
    eng = _engine(tmp_path, project)
    _clean_run(eng, tasks=("t1", "t2"))

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig", superseded_by="r2")

    assert project.task_source.notes == []  # no publish_note
    assert project.task_source.completed == []  # no mark_complete
    assert project.task_source.followups == []  # nothing filed
    # And no durable rejection artifact — the gate record of a close that never happened.
    for task_id in ("t1", "t2"):
        assert eng.store.load_rejection("r1", task_id) is None
    assert _events(eng, "r1", "rejected") == []
    assert _events(eng, "r1", "rejection_note_published") == []


def test_retire_does_not_cascade_block_dependents(tmp_path, project) -> None:
    """A dependent of a superseded task is superseded by this very call. Cascading would
    stamp it CASCADE_BLOCKED — an execution-failure state that poisons the rollup."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2", depends_on=["t1"])

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    assert eng.store.load_task("r1", "t2").state is TaskState.SUPERSEDED
    assert _events(eng, "r1", "cascade_blocked") == []
    assert eng.store.load_run("r1").progress().cascade_blocked == 0


def test_retire_emits_no_task_failed_alert(tmp_path, project) -> None:
    """Nothing failed. Re-alerting the operator is the opposite of what retiring is for."""
    eng = _engine(tmp_path, project)
    _clean_run(eng)

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    kinds = [e.get("kind") for e in _events(eng, "r1", "notification")]
    assert "task_failed" not in kinds
    assert "run_superseded" in kinds  # the "stop watching this run" ping still fires


# --- the audit trail ----------------------------------------------------------------


def test_retire_records_why_on_the_run_doc_and_in_events(tmp_path, project) -> None:
    """A retired run must EXPLAIN itself, not read as a mystery half-run — and the Run doc
    carries it so a later CLI process (a fresh Engine from defaults, #206) can still say."""
    eng = _engine(tmp_path, project)
    _clean_run(eng)

    eng.retire("r1", reason="SCOPE inverted the content_hash rule; issue amended",
               retired_by="craig", superseded_by="batch-73-review-workflow-2")

    run = eng.store.load_run("r1")
    assert run.superseded_reason == "SCOPE inverted the content_hash rule; issue amended"
    assert run.superseded_by == "batch-73-review-workflow-2"
    assert run.retired_by == "craig"
    assert run.superseded_at

    [ev] = _events(eng, "r1", "run_superseded")
    assert ev["reason"] == "SCOPE inverted the content_hash rule; issue amended"
    assert ev["superseded_by"] == "batch-73-review-workflow-2"
    assert ev["retired_by"] == "craig"
    assert ev["retired_tasks"] == ["t1"]
    [task_ev] = _events(eng, "r1", "task_superseded")
    assert task_ev["task_id"] == "t1"
    assert task_ev["superseded_by"] == "batch-73-review-workflow-2"


def test_status_surfaces_the_supersede_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _clean_run(eng)
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig", superseded_by="r2")

    st = eng.status("r1")
    assert st["run_state"] == "superseded"
    assert st["superseded"] == {
        "reason": "rebuilt as r2", "superseded_by": "r2", "retired_by": "craig",
        "at": eng.store.load_run("r1").superseded_at,
    }


def test_a_run_that_was_never_retired_has_no_supersede_block(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _clean_run(eng)
    assert eng.status("r1")["superseded"] is None


# --- the point: the alarms stop, permanently ----------------------------------------


def test_retiring_clears_the_stale_alarms(tmp_path, project) -> None:
    """The reason the gap mattered: five non-terminal tasks emitted five task_stale alerts
    against work nobody intends to resume. ``status`` never flags a terminal task stale."""
    eng = _engine(tmp_path, project)
    _clean_run(eng, tasks=("t1", "t2"))
    before = eng.status("r1", stale_after_s=0)
    assert [t for t in before["tasks"].values() if t["stale"]]  # the alarm state

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    after = eng.status("r1", stale_after_s=0)
    assert not [t for t in after["tasks"].values() if t["stale"]]
    notes, _ = stale_notifications(after, set())
    assert notes == []


def test_a_superseded_run_ends_a_watch(tmp_path, project) -> None:
    """A terminal state the watch loop does not recognise means polling a finished run
    forever — the failure mode a hand-maintained copy of the terminal set invites."""
    from orchestrator import alerting

    eng = _engine(tmp_path, project)
    _clean_run(eng)
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    def _never(_interval: int) -> None:
        raise AssertionError("watch slept instead of returning on a terminal run")

    assert alerting.watch(eng, "r1", sleeper=_never)["run_state"] == "superseded"


def test_dashboard_counts_a_superseded_task_as_done(tmp_path, project) -> None:
    """A terminal task missing from the board's done-rollup reads as outstanding work that
    will never leave the board."""
    eng = _engine(tmp_path, project)
    _clean_run(eng, tasks=("t1", "t2"))
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    assert _progress_str(eng.store.load_run("r1").progress().model_dump()) == "2/2"


def test_retire_retains_the_run_log_dir(tmp_path, project) -> None:
    """Retiring is about STATE, never cleanup — the run log is the durable audit trail."""
    eng = _engine(tmp_path, project)
    _clean_run(eng)
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    assert eng.store.read_events("r1")
    assert (tmp_path / "cost-summary.md").exists()  # terminal runs get their cost artifacts


# --- the lease guard ----------------------------------------------------------------


def test_retire_refuses_an_outstanding_dispatch_without_force(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)

    with pytest.raises(ContractError) as exc:
        eng.retire("r1", reason="rebuilt as r2", retired_by="craig")
    assert "t1" in str(exc.value) and "--force" in str(exc.value)
    # Refused ⇒ nothing moved: the run is still live and the lease is still held.
    assert eng.store.load_run("r1").state is not RunState.SUPERSEDED
    assert eng.store.load_task("r1", "t1").pending_work_item_id is not None


def test_forced_retire_clears_the_lease_and_bills_it_unmetered(tmp_path, project) -> None:
    """#319 honesty: the orphaned provider process may have burned real spend before being
    retired, so its row must read as UNKNOWN — not as a confident, metered $0.00."""
    eng = _engine(tmp_path, project)
    stage = _mid_dispatch(eng)

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig", force=True)

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.SUPERSEDED
    assert task.pending_work_item_id is None
    assert task.pending_content_hash is None
    assert task.pending_plan is False
    assert task.stages[stage].status is StageStatus.FAILED
    assert "superseded" in (task.stages[stage].error or "")

    rows = eng.run_rows("r1")
    [row] = [r for r in rows if r.get("stage") == stage.value]
    assert row["cost_usd"] == 0.0
    assert row["metered"] is False  # unpriced, not a measured zero
    [ev] = _events(eng, "r1", "task_superseded")
    assert ev["forced"] is True and ev["stage"] == stage.value


def test_forced_retire_writes_the_stage_log(tmp_path, project) -> None:
    """The abandoned attempt still gets its per-stage record, so the run log explains the
    stage's end instead of stopping mid-dispatch."""
    eng = _engine(tmp_path, project)
    stage = _mid_dispatch(eng)
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig", force=True)

    logs = list((tmp_path / "stages" / "t1").glob(f"*{stage.value}*"))
    assert logs, "forced retire wrote no stage log"
    payload = json.loads(next(p for p in logs if p.suffix == ".json").read_text())
    assert payload["outcome"] == "superseded"


# --- terminal is terminal -----------------------------------------------------------


def test_retire_refuses_an_already_terminal_run(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _clean_run(eng)
    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")

    with pytest.raises(ContractError, match="already terminal"):
        eng.retire("r1", reason="again", retired_by="craig")


def test_a_later_finalize_cannot_rewrite_a_retired_run(tmp_path, project) -> None:
    """The run state is DECLARED by the human, not derived. The derived rollup is
    failure-dominated, so without the guard a retired run holding one FAILED task would be
    silently rewritten to FAILED — contradicting its own run_superseded event."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake completes
    eng.next_work("r1", "t1")  # scope dispatched, never recorded
    eng.abandon("r1", "t1", reason="provider process killed")  # t1 FAILED, run still open

    eng.retire("r1", reason="rebuilt as r2", retired_by="craig")
    assert eng.store.load_run("r1").state is RunState.SUPERSEDED

    eng._maybe_finalize_run("r1")  # every task is terminal; the rollup would say FAILED

    assert eng.store.load_run("r1").state is RunState.SUPERSEDED


# --- CLI wiring ---------------------------------------------------------------------


def test_cli_retire(tmp_path, capsys, project) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "r1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    assert main([*base, "add-task", "--task", "#42"]) == 0
    capsys.readouterr()

    rc = main([*base, "retire", "--reason", "rebuilt as r2", "--by", "craig",
               "--superseded-by", "r2"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out == {"retired": "r1", "state": "superseded", "superseded": 1,
                   "superseded_by": "r2", "by": "craig", "reason": "rebuilt as r2"}

    eng = _engine(tmp_path, project)
    assert eng.store.load_task("r1", "#42").state is TaskState.SUPERSEDED
