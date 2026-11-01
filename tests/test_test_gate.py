"""TEST carries no meaningfulness veto any more (#541), and a red TEST's output reaches the
stage that has to act on it. REVIEW's proof gate is pinned in test_test_validation_signal.py."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_model_test(eng):
    eng.create_run("r1")
    eng.add_task("r1", "t1", deterministic_stages=())
    for _ in range(3):  # intake, scope, implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST
    return w


def test_a_model_test_saying_tests_are_vacuous_no_longer_vetoes(tmp_path, project) -> None:
    """An opted-in model TEST may still say it; REVIEW alone acts on the question."""
    eng = _engine(tmp_path, project)
    w = _advance_to_model_test(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "passed": True, "failures": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "stage_completed"
    assert "tests_meaningful" not in eng.store.load_task("r1", "t1").context  # not folded


def test_a_red_tests_notes_reach_the_next_prompt_keeping_their_tail(tmp_path, project) -> None:
    """A failed result is never absorbed into the context, so the learning is the only way
    the output tail reaches whoever fixes it. Bounded from the END: the failure is there."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_to_model_test(eng)
    eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="red",
        structured_output={"passed": False, "failures": ["t::x"],
                           "validation_notes": "head " + "x" * 5000 + " assert 1 == 2"},
    ))
    learning = eng.store.load_task("r1", "t1").learnings[-1]
    assert "test run notes:" in learning and "assert 1 == 2" in learning
    assert "head " not in learning and len(learning) < 2000


def test_other_stages_failures_gain_no_notes_line(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, error="boom",
        structured_output={"validation_notes": "not a test stage"},
    ))
    assert "test run notes:" not in eng.store.load_task("r1", "t1").learnings[-1]
