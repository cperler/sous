"""Failure learnings carry substance (failing tests + output tail + taxonomy), and an
infra-classified TEST failure doesn't stack the code-failure breaker streak. Restores
the old system's stage-trail/log-tail learning content and gives the failure
classifier its first production caller (the reset loop itself remains issue #14)."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import FailureKind, ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_test(eng, run="r1", task="t1"):
    eng.create_run(run)
    eng.add_task(run, task)
    for _ in range(3):  # intake, scope, implement
        eng.record(run, make_result(eng.next_work(run, task)))
    w = eng.next_work(run, task)
    assert w.stage is Stage.TEST
    return w


class _InfraClassifier:
    def classify(self, test_output: str) -> list[Failure]:
        return [Failure(test="<infra>", kind=FailureKind.INFRA, message="port in use")]

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        return []


class _RaisingClassifier:
    def classify(self, test_output: str) -> list[Failure]:
        raise RuntimeError("classifier exploded")

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        return []


def test_learning_carries_failures_and_output_tail(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, breaker_threshold=9)
    w = _advance_to_test(eng)
    result = make_result(
        w, status=ResultStatus.FAILURE, error="2 tests failed",
        structured_output={"failures": ["tests/test_a.py::t1", "tests/test_b.py::t2"]},
    )
    result = result.model_copy(update={"raw_output": "x" * 600 + "AssertionError: expected 3 got 2"})
    eng.record("r1", result)

    task = eng.store.load_task("r1", "t1")
    learning = task.learnings[-1]
    assert "test (attempt 0): 2 tests failed" in learning
    assert "tests/test_a.py::t1" in learning and "tests/test_b.py::t2" in learning
    assert "AssertionError: expected 3 got 2" in learning  # output tail survives
    assert "output tail: …" in learning  # and is visibly clipped
    # the whole enriched entry reaches the retry prompt
    nxt = eng.next_work("r1", "t1")
    assert "AssertionError: expected 3 got 2" in nxt.prompt


def test_infra_failure_resets_env_and_reruns_same_attempt(tmp_path) -> None:
    """The #14 loop: an infra-classified TEST failure runs the project's infra_reset
    and re-runs the SAME attempt — a broken runner never consumes the code-fix budget
    or stacks the breaker streak."""
    project = FakeProject()
    project._classifier = _InfraClassifier()
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=2)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="ECONNREFUSED 127.0.0.1:5173",
    ))
    assert out["outcome"] == "stage_infra_reset_retry"
    task = eng.store.load_task("r1", "t1")
    assert task.error_signatures == []  # infra doesn't stack the code-failure streak
    assert task.infra_resets == 1
    assert "environment reset (ok)" in task.learnings[-1]  # FakeProject's echo ran
    # the SAME attempt re-dispatches (RUNNING marker, not a burned retry)
    w2 = eng.next_work("r1", "t1")
    assert w2.stage is Stage.TEST and w2.attempt == w.attempt
    events = [e for e in eng.store.read_events("r1") if e["type"] == "infra_reset"]
    assert len(events) == 1 and events[0]["resets_used"] == 1
    # a clean stage refreshes the reset budget
    eng.record("r1", make_result(w2))
    assert eng.store.load_task("r1", "t1").infra_resets == 0


def test_infra_reset_budget_exhaustion_degrades_to_normal_failure(tmp_path) -> None:
    project = FakeProject()
    project._classifier = _InfraClassifier()
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9, max_infra_resets=1)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="ECONNREFUSED",
    ))
    assert out["outcome"] == "stage_infra_reset_retry"  # reset #1
    w2 = eng.next_work("r1", "t1")
    out2 = eng.record("r1", make_result(
        w2, status=ResultStatus.FAILURE, error="ECONNREFUSED",
    ))
    # budget spent: the persistent infra failure now takes the normal failure path
    assert out2["outcome"] == "stage_failed_will_retry"
    task = eng.store.load_task("r1", "t1")
    assert task.error_signatures == []  # still never stacks the code-failure streak
    assert "INFRASTRUCTURE failure" in task.learnings[-1]


def test_raising_classifier_never_breaks_failure_handling(tmp_path) -> None:
    project = FakeProject()
    project._classifier = _RaisingClassifier()
    eng = _engine(tmp_path, project, breaker_threshold=9)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom"))
    assert out["outcome"] == "stage_failed_will_retry"  # classification is best-effort
    task = eng.store.load_task("r1", "t1")
    assert task.error_signatures  # normal (non-infra) signature path taken


def test_baseline_split_separates_inherited_from_regressions(tmp_path, project) -> None:
    """The deterministic regression-vs-inherited diff (ADR-035 parity): a TEST failure's
    learning names which failures were already red at base and which are real."""
    eng = _engine(tmp_path, project, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.INTAKE
    eng.record("r1", make_result(w, structured_output={
        "branch": "task/1", "worktree": "/wt/1", "baseline_captured": True,
        "baseline_failures": ["tests/test_old.py::flaky"],
    }))
    for _ in range(2):  # scope, implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    wt = eng.next_work("r1", "t1")
    assert wt.stage is Stage.TEST
    # the TEST prompt itself carries the inherited list via the context plane
    assert "tests/test_old.py::flaky" in wt.prompt

    eng.record("r1", make_result(
        wt, status=ResultStatus.FAILURE, error="2 failed",
        structured_output={"failures": ["tests/test_old.py::flaky", "tests/test_new.py::broke"]},
    ))
    learning = eng.store.load_task("r1", "t1").learnings[-1]
    assert "inherited from base" in learning and "tests/test_old.py::flaky" in learning
    assert "true regressions to fix: tests/test_new.py::broke" in learning


def test_non_test_stage_failure_is_not_classified(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.IMPLEMENT
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="merge conflict"))
    events = [e for e in eng.store.read_events("r1") if e["type"] == "failure_classified"]
    assert events == []  # classifier is a TEST-stage concern
