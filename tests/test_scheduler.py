"""Phase 3b scheduler — done-criteria: 3-task batch, induced failure →
retry-with-learnings + transitive cascade, and clean resume after a kill."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


class SimRunner:
    """Simulated execution lane. fail_plan: {(task_id, Stage): n_times_to_fail}."""

    def __init__(self, fail_plan: dict | None = None) -> None:
        self.fail_plan = dict(fail_plan or {})
        self.seen: list[tuple[str, str, int, str]] = []  # (task, stage, attempt, prompt)

    def __call__(self, workitems):
        out = []
        for w in workitems:
            self.seen.append((w.task_id, w.stage.value, w.attempt, w.prompt))
            key = (w.task_id, w.stage)
            if self.fail_plan.get(key, 0) > 0:
                self.fail_plan[key] -= 1
                out.append(
                    make_result(w, status=ResultStatus.FAILURE, error="induced boom", structured_output={})
                )
            else:
                out.append(make_result(w))
        return out


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _three_task_run(eng: Engine, *, b_deps_a: bool = True) -> None:
    eng.create_run("r1", ExecutionLane.FULL)
    eng.project.task_source.deps = {"B": ["A"]} if b_deps_a else {}
    eng.add_task("r1", "A")
    eng.add_task("r1", "B")  # depends on A
    eng.add_task("r1", "C")  # independent


def test_three_task_batch_completes_in_dependency_order(tmp_path) -> None:
    eng = _engine(tmp_path, FakeProject())
    _three_task_run(eng)
    runner = SimRunner()
    status = Scheduler(eng, max_concurrent=3).run("r1", runner)

    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("A", "B", "C"))
    # B's first stage must not start until A is fully done (dependency gating).
    a_done = max(i for i, (t, *_rest) in enumerate(runner.seen) if t == "A")
    b_start = min(i for i, (t, *_rest) in enumerate(runner.seen) if t == "B")
    assert b_start > a_done


def test_induced_failure_retries_with_learnings_then_cascades(tmp_path) -> None:
    # A's implement fails every time; with max_attempts=2 it retries once (learnings
    # fire) then permanently fails -> B (deps A) is cascade-blocked; C is unaffected.
    eng = _engine(tmp_path, FakeProject(), max_attempts=2, breaker_threshold=9)
    _three_task_run(eng)
    runner = SimRunner(fail_plan={("A", Stage.IMPLEMENT): 99})
    status = Scheduler(eng, max_concurrent=3).run("r1", runner)

    # retry-with-learnings: the attempt-1 implement WorkItem carried the prior failure.
    retry_prompts = [p for (t, s, attempt, p) in runner.seen if t == "A" and s == "implement" and attempt == 1]
    assert retry_prompts and "induced boom" in retry_prompts[0]

    # cascade-blocking the dependent; independent task still finishes; run fails.
    assert status["tasks"]["A"]["state"] == "failed"
    assert status["tasks"]["B"]["state"] == "cascade_blocked"
    assert status["tasks"]["C"]["state"] == "completed"
    assert status["run_state"] == "failed"


def test_clean_resume_after_kill_no_double_execution(tmp_path) -> None:
    project = FakeProject()
    eng = _engine(tmp_path, project)
    _three_task_run(eng, b_deps_a=False)  # all independent for max parallelism
    runner = SimRunner()
    sched = Scheduler(eng, max_concurrent=3)

    # Run a few ticks, then "kill": drop engine/scheduler, rebuild on the SAME dir.
    for _ in range(5):
        sched.tick("r1", runner)
    assert eng.status("r1")["run_state"] != "completed"  # genuinely partial

    eng2 = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject())
    status = Scheduler(eng2, max_concurrent=3).run("r1", SimRunner())

    assert status["run_state"] == "completed"
    assert all(status["tasks"][t]["state"] == "completed" for t in ("A", "B", "C"))
    # Exactly one ledger row per stage execution (3 tasks x 6 stages) — no stage
    # was re-run after resume.
    assert status["lane_audit"]["total_calls"] == 18
    assert status["lane_audit"]["clean"] is True
