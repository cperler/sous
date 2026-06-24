"""The test-validate gate: a green test run that doesn't affirm meaningful tests
is vetoed by the engine (the 'verify' half of the collapsed test stage, §6.1)."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, Stage, StageStatus
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_test(eng):
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(3):  # intake, scope, implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST
    return w


def test_gate_vetoes_passing_but_vacuous_tests(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_to_test(eng)
    # Runner reports SUCCESS, tests green — but does NOT affirm meaningfulness.
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"passed": True, "failures": [], "tests_meaningful": False},
    ))
    assert out["outcome"] == "stage_failed_will_retry"  # vetoed -> retry, not shipped
    assert out["task_state"] == "retrying"
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.TEST].status is StageStatus.FAILED
    assert "test-validate gate" in (task.last_error or "")
    # the cost of the (real) model call is still recorded
    assert out["cost_usd"] > 0
    # re-dispatch returns the SAME test stage (retry with the gate learning appended)
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.TEST and "test-validate gate" in nxt.prompt


def test_gate_passes_when_tests_affirmed_meaningful(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"passed": True, "failures": [], "tests_meaningful": True,
                           "validation_notes": "asserts the new behavior"},
    ))
    assert out["outcome"] == "stage_completed"
    assert out["next_stage"] == "deliver"


def test_gate_fails_closed_when_field_missing(tmp_path, project) -> None:
    """A test result omitting tests_meaningful is treated as unverified (vetoed)."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"passed": True, "failures": []},  # no tests_meaningful
    ))
    assert out["outcome"] == "stage_failed_will_retry"


def test_gate_only_applies_to_test_stage(tmp_path, project) -> None:
    """Other stages reporting SUCCESS without the field are unaffected."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # implement success carries no tests_meaningful and must still advance
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    out = eng.record("r1", make_result(
        eng.next_work("r1", "t1"),
        structured_output={"files_changed": ["a.py"], "summary": "x", "committed": True},
    ))
    assert out["outcome"] == "stage_completed" and out["next_stage"] == "test"
