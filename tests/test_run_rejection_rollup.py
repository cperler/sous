"""Run-level CLOSED_INFEASIBLE semantics (#67).

Two refinements over the task-level #52/#53 work:
  1. a run whose non-completed tasks are ALL closed-infeasible rolls up to the honest
     COMPLETED_WITH_REJECTIONS (nothing broke, but not everything shipped) — NOT FAILED;
     a run with any real execution failure stays FAILED even if it also has rejections;
  2. the retrospective (emitted only for FAILED runs) annotates the deliberately-closed
     tasks with their reason, so a mixed run no longer silently drops them — and a
     rejection-only run emits no "No failures recorded" retrospective at all.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_retrospective
from orchestrator.schemas.enums import ResultStatus, RunState, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _reject(eng: Engine, task: str, reason: str) -> None:
    eng.hold_for_approval("r1", task, what="scope reported infeasible")
    eng.reject("r1", task, rejected_by="craig", reason=reason)


def test_rejection_only_run_rolls_up_to_completed_with_rejections(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    _reject(eng, "t1", "needs an upstream migration first")
    _reject(eng, "t2", "duplicate of an already-shipped change")

    run = eng.store.load_run("r1")
    # No execution failure anywhere -> the honest non-failure terminal (not FAILED, which
    # would read as a broken run; not COMPLETED, which would read as all-shipped).
    assert run.state is RunState.COMPLETED_WITH_REJECTIONS
    assert run.progress().closed_infeasible == 2


def test_mixed_failure_and_rejection_run_stays_failed(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=1, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t_fail")
    eng.add_task("r1", "t_inf")
    # A genuine execution failure on t_fail (fails at its first stage, attempts=1).
    w = eng.next_work("r1", "t_fail")
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom"))
    assert eng.store.load_task("r1", "t_fail").state is TaskState.FAILED
    # ...plus a human rejection on t_inf.
    _reject(eng, "t_inf", "not worth doing")

    run = eng.store.load_run("r1")
    # A real failure DOMINATES the rollup — a mixed run is not softened to a non-failure.
    assert run.state is RunState.FAILED


def test_failed_run_retrospective_annotates_rejections(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=1, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t_fail")
    eng.add_task("r1", "t_inf")
    w = eng.next_work("r1", "t_fail")
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom"))
    _reject(eng, "t_inf", "depends on a deprecated third-party API")

    retro = eng.retrospective("r1")
    assert retro["run_state"] == "failed"
    assert retro["totals"]["failed"] == 1 and retro["totals"]["closed_infeasible"] == 1
    # The deliberately-closed task appears distinctly, with the reason read back from the
    # durable rejection artifact — NOT lumped in with the execution failures.
    rejected = retro["rejected_tasks"]
    assert [r["task_id"] for r in rejected] == ["t_inf"]
    assert rejected[0]["reason"] == "depends on a deprecated third-party API"

    md = render_retrospective(retro)
    assert "Closed as infeasible" in md
    assert "depends on a deprecated third-party API" in md


def test_rejection_only_run_emits_a_retrospective(tmp_path, project) -> None:
    """A rejection-only run finalizes COMPLETED_WITH_REJECTIONS and still retrospects.

    This used to assert the opposite — the retrospective was gated on RunState.FAILED, so
    the one run shape whose whole story is "a human closed this as infeasible" produced no
    document at all. The reason it is safe to emit now is the #67 rejected_tasks section:
    the artifact names the closed task and its reason rather than reading "No failures
    recorded", which was the original objection to emitting here."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    _reject(eng, "t1", "genuinely infeasible")

    assert eng.store.load_run("r1").state is RunState.COMPLETED_WITH_REJECTIONS
    md = (eng.store.root / "retrospective.md").read_text()
    assert "Closed as infeasible" in md and "genuinely infeasible" in md
    # Not a failure — the heading must not claim one.
    assert md.startswith("# Run retrospective — r1")
    events = eng.store.read_events("r1")
    assert [e["run_state"] for e in events if e["type"] == "retrospective_emitted"] == [
        "completed_with_rejections"
    ]
