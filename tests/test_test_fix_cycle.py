"""#541: a red ENGINE-lane TEST goes back to IMPLEMENT.

A model TEST fixed its own regressions. The ENGINE-lane runner cannot, and retrying it re-runs
identical code, so before this cycle existed every regression on a FULL task would have failed
the task on the breaker's second identical failure.
"""

from __future__ import annotations

from adapters.execution.deterministic_test import DeterministicTestRunner
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    FailureKind,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
    TaskState,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

RED = {
    "passed": False, "failures": ["tests/a.py::test_x"], "failure_kinds": ["unit"],
    "validation_notes": "unit: FAILED rc=1; unit output tail: E assert 1 == 2",
}


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _to_test(eng, run="r1", task="t1"):
    while True:
        w = eng.next_work(run, task)
        assert w is not None
        if w.stage is Stage.TEST:
            return w
        eng.record(run, make_result(w))


def _red(eng, w, output=RED, run="r1"):
    return eng.record(run, make_result(
        w, status=ResultStatus.FAILURE, error="test stage red", structured_output=output,
        mode=ExecutionMode.ENGINE, provider=Provider.NONE,
    ))


def _events(eng, kind="test_failed_fix_cycle", run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == kind]


def test_red_engine_test_reopens_implement_with_the_failure_in_its_prompt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    out = _red(eng, _to_test(eng))
    assert out["outcome"] == "test_failed_fix_cycle"

    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.RETRYING and task.test_fix_cycles == 1
    assert task.session_ref is None
    assert task.resume_cursor.hint == "test fix cycle: re-running from implement"
    assert task.stages[Stage.TEST].status is StageStatus.PENDING

    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.IMPLEMENT
    assert w.attempt == 0 and w.reset_to is None  # builds on the prior commits, keeps them
    assert w.review_cycle == 1  # the re-run's log files do not overwrite the first pass's
    assert "tests/a.py::test_x" in w.prompt and "assert 1 == 2" in w.prompt
    assert [e["failures"] for e in _events(eng)] == [["tests/a.py::test_x"]]


def test_a_fixed_regression_runs_on_to_completion(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _red(eng, _to_test(eng))
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED


def test_the_budget_runs_out_and_fails_the_task(tmp_path, project) -> None:
    """Bounded at max_attempts - 1, even though IMPLEMENT's success clears the breaker's streak
    between trips."""
    eng = _engine(tmp_path, project, max_attempts=3)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _red(eng, _to_test(eng))["outcome"] == "test_failed_fix_cycle"
    assert _red(eng, _to_test(eng))["outcome"] == "test_failed_fix_cycle"
    assert _red(eng, _to_test(eng))["outcome"] == "task_failed_max_attempts"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.FAILED and task.test_fix_cycles == 2
    assert len(_events(eng)) == 2


def test_lite_gets_the_same_cycle(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    assert _red(eng, _to_test(eng))["outcome"] == "test_failed_fix_cycle"
    assert eng.next_work("r1", "t1").stage is Stage.IMPLEMENT


def test_a_run_that_only_timed_out_retries_test_instead(tmp_path, project) -> None:
    """The code was never shown to be wrong, so nothing goes back to the implementer."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    out = _red(eng, _to_test(eng), {
        "passed": False, "failures": ["<unit timeout>"], "failure_kinds": ["infra"],
    })
    assert out["outcome"] == "stage_failed_will_retry"
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST and w.attempt == 1


def test_a_timeout_alongside_a_real_failure_still_goes_to_the_implementer(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    out = _red(eng, _to_test(eng), {
        "passed": False, "failures": ["a::x", "<e2e timeout>"], "failure_kinds": ["infra", "unit"],
    })
    assert out["outcome"] == "test_failed_fix_cycle"


def test_a_refused_run_retries_test_instead(tmp_path, project) -> None:
    """A run that never started reports no output at all."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _red(eng, _to_test(eng), None)["outcome"] == "stage_failed_will_retry"


def test_an_infra_classified_failure_takes_the_reset_loop(tmp_path, project) -> None:
    class Classifier:
        def classify(self, text):
            return [Failure(test="<infra>", kind=FailureKind.INFRA)]

    project._classifier = Classifier()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _red(eng, _to_test(eng))["outcome"] == "stage_infra_reset_retry"
    assert eng.store.load_task("r1", "t1").test_fix_cycles == 0


def test_a_pipeline_with_no_implement_retries_test(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", pipeline=[Stage.INTAKE, Stage.TEST, Stage.DELIVER],
                 deterministic_stages=[Stage.TEST])
    assert _red(eng, _to_test(eng))["outcome"] == "stage_failed_will_retry"


def test_a_model_test_keeps_its_own_retry(tmp_path, project) -> None:
    """An opted-in model TEST can fix its own regression, so it still retries in place."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", deterministic_stages=())
    w = _to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="red", structured_output=RED,
    ))
    assert out["outcome"] == "stage_failed_will_retry"
    assert eng.next_work("r1", "t1").stage is Stage.TEST


def test_the_real_runner_drives_the_cycle_from_red_to_green(tmp_path, project) -> None:
    """The runner's output and the engine's routing have to agree on what a verdict is, so
    this one runs the real ENGINE-lane runner: red until IMPLEMENT makes the test pass."""
    project.test_unit_cmd = lambda files=None: [
        "sh", "-c", "test -f fixed || { echo 'FAILED a::x'; exit 1; }",
    ]
    worktree = tmp_path / "wt"
    worktree.mkdir()
    runner = DeterministicTestRunner(project)
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    outcomes = []
    implements = 0
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.TEST:
            outcomes.append(eng.record("r1", runner.dispatch(w.model_copy(
                update={"cwd": str(worktree)}
            )))["outcome"])
            continue
        if w.stage is Stage.IMPLEMENT:
            implements += 1
            if implements == 2:
                assert "a::x" in w.prompt
                (worktree / "fixed").write_text("")
        eng.record("r1", make_result(w))
    assert outcomes == ["test_failed_fix_cycle", "stage_completed"]
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED
