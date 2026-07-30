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


def test_gate_fails_open_when_field_missing(tmp_path, project) -> None:
    """Fail-OPEN on a missing field: nothing enforces tests_meaningful on the
    interactive/headless lanes, so a runner that omits it must not dead-end green
    work — only an explicit `false` is a veto."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"passed": True, "failures": []},  # no tests_meaningful
    ))
    assert out["outcome"] == "stage_completed"  # not vetoed
    assert out["next_stage"] == "deliver"


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


# --- #298: a vacuity veto carries its reasoning into the retry's prompt -------------
# A REVIEW rejection already feeds its blocking issues forward as learnings; a TEST
# `tests_meaningful: false` veto used to drop `validation_notes` — the only channel
# naming *why* — making the retry re-derive the diagnosis or pass on a re-roll.

_NOTES = (
    "claim 6 (durability of the torn-tail repair) is asserted only by a happy-path "
    "read; mutating the repair to a no-op leaves the test green. Claims 1-5 were "
    "mutation-tested and are genuinely covered."
)


def _veto_test_stage(eng, *, structured_output: dict) -> None:
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS, structured_output=structured_output,
    ))
    assert out["outcome"] == "stage_failed_will_retry"


def test_vacuity_veto_carries_validation_notes_into_retry_prompt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    _veto_test_stage(eng, structured_output={
        "passed": True, "failures": [], "tests_meaningful": False,
        "validation_notes": _NOTES,
    })
    # the load-bearing veto string is unchanged (error_signature / breaker / last_error)
    assert "test-validate gate" in (eng.store.load_task("r1", "t1").last_error or "")
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.TEST and nxt.attempt == 1
    # the reason the prior attempt rejected, framed as such, verbatim
    assert "why the previous attempt rejected" in nxt.prompt
    assert _NOTES in nxt.prompt
    # ...plus the convergence directive the supervisor used to hand-write
    assert "CLOSE the specific gap" in nxt.prompt
    assert "ADEQUATE is settled" in nxt.prompt
    assert "test-validate gate" in nxt.prompt


def test_vacuity_veto_without_notes_emits_no_empty_header(tmp_path, project) -> None:
    """Blank/absent notes still yield the generic gate line — and no stray header."""
    for i, blank in enumerate((
        {}, {"validation_notes": ""}, {"validation_notes": "   "},
        {"validation_notes": None}, {"validation_notes": ["not", "a", "string"]},
    )):
        eng = _engine(tmp_path / f"b{i}", project, max_attempts=3, breaker_threshold=9)
        _veto_test_stage(eng, structured_output={
            "passed": True, "failures": [], "tests_meaningful": False, **blank,
        })
        nxt = eng.next_work("r1", "t1")
        assert "test-validate gate" in nxt.prompt
        assert "why the previous attempt rejected" not in nxt.prompt
        assert "prior validation_notes" not in nxt.prompt


def test_tri_state_and_non_test_failures_gain_no_notes_block(tmp_path, project) -> None:
    """Only an explicit tests_meaningful=false carries notes: a tri-state null (#261) and
    an ordinary (non-vetoed) TEST failure that happens to carry notes must not."""
    # (a) tests_meaningful=None -> fails open, no veto, nothing to carry
    eng = _engine(tmp_path / "a", project, max_attempts=3, breaker_threshold=9)
    w = _advance_to_test(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.SUCCESS,
        structured_output={"passed": True, "failures": [], "tests_meaningful": None,
                           "validation_notes": _NOTES},
    ))
    assert out["outcome"] == "stage_completed"  # fail-open, unchanged

    # (b) a genuine TEST failure (red tests, no vacuity self-report) carries the failing
    # ids as before but no vacuity header
    eng2 = _engine(tmp_path / "b", project, max_attempts=3, breaker_threshold=9)
    w2 = _advance_to_test(eng2)
    eng2.record("r1", make_result(
        w2, status=ResultStatus.FAILURE, error="2 failed",
        structured_output={"passed": False, "failures": ["tests/test_x.py::test_y"],
                           "validation_notes": _NOTES},
    ))
    nxt2 = eng2.next_work("r1", "t1")
    assert "tests/test_x.py::test_y" in nxt2.prompt
    assert "why the previous attempt rejected" not in nxt2.prompt

    # (c) a non-TEST stage failure is untouched
    eng3 = _engine(tmp_path / "c", project, max_attempts=3, breaker_threshold=9)
    eng3.create_run("r1")
    eng3.add_task("r1", "t1")
    eng3.record("r1", make_result(eng3.next_work("r1", "t1")))  # intake
    eng3.record("r1", make_result(eng3.next_work("r1", "t1")))  # scope
    eng3.record("r1", make_result(
        eng3.next_work("r1", "t1"), status=ResultStatus.FAILURE, error="boom",
        structured_output={"tests_meaningful": False, "validation_notes": _NOTES},
    ))
    nxt3 = eng3.next_work("r1", "t1")
    assert nxt3.stage is Stage.IMPLEMENT
    assert "why the previous attempt rejected" not in nxt3.prompt


def test_vacuity_notes_are_bounded(tmp_path, project) -> None:
    """A pathologically long note is clipped (ellipsis-marked), like the output tail."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    _veto_test_stage(eng, structured_output={
        "passed": True, "failures": [], "tests_meaningful": False,
        "validation_notes": "N" * 5000,
    })
    task = eng.store.load_task("r1", "t1")
    entry = next(le for le in task.learnings if "prior validation_notes" in le)
    assert "N" * 1000 in entry and "N" * 1001 not in entry
    assert "…" in entry
