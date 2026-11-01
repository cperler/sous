"""#261/#541: REVIEW owns the tests-meaningful verdict, must prove it, and a pass nobody
proved says so.

The hole this pins (observed on #255, a real 7-test code change on the `lite` lane): the
deterministic ENGINE-lane TEST runner cannot judge whether tests are meaningful; the REVIEW
model omitted `tests_meaningful` and nothing noticed. #541 made TEST on the ENGINE lane the
default everywhere and REVIEW the only judge, so:
  * the GATE — an approval on a change with a test surface must carry a boolean verdict and a
    mutation that failed a test, or a stated reason; otherwise REVIEW re-runs;
  * the SIGNAL — a warning-grade `test_validation_skipped` event whenever a stated reason let
    an unproven pass through, or a reviewer's explicit verdict was suppressed by the #41/#168
    no-test-surface exemption.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage
from orchestrator.schemas.status import Task
from orchestrator.state_machine import (
    no_test_surface,
    proving_mutation_count,
    review_proof_gate,
    unjudged_tests_notice,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# What the deterministic ENGINE-lane TEST runner returns: a real suite run with NO
# meaningfulness claim. `change_class` folds because the lane is ENGINE.
ENGINE_TEST_OUTPUT = {
    "passed": True,
    "failures": [],
    "change_class": "code",
    "validation_notes": "unit: green",
}
PROOF = {"mutation_checks": [{"mutation": "a.py:3 drop the guard", "failed_tests": ["t::x"]}]}


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _skipped_events(eng, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == "test_validation_skipped"]


def _lite_to_review(eng, *, test_output=ENGINE_TEST_OUTPUT, run="r1", task="t1"):
    """Drive a LITE task (intake, implement, TEST-on-ENGINE, deliver) and return REVIEW."""
    for _ in range(2):  # intake, implement
        eng.record(run, make_result(eng.next_work(run, task)))
    wt = eng.next_work(run, task)
    assert wt.stage is Stage.TEST
    t = eng.store.load_task(run, task)
    assert Stage.TEST in t.deterministic_stages  # the lane preset this issue was hit on
    eng.record(run, make_result(wt, structured_output=test_output))
    eng.record(run, make_result(eng.next_work(run, task)))  # deliver
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


# --- the gate ---------------------------------------------------------------------------
def test_an_approval_with_no_verdict_is_sent_back_with_the_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "stage_failed_will_retry"
    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.REVIEW and retry.attempt == 1
    assert "review proof gate" in retry.prompt  # the retry is told what was missing
    assert _skipped_events(eng) == []  # sent back, so it is not a skip


def test_true_with_no_mutation_that_failed_a_test_is_sent_back(tmp_path, project) -> None:
    """A survived mutation is an honest record, but it proves nothing in the tests' favor."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [{"mutation": "a.py:3 drop the guard", "failed_tests": []}],
    }))
    assert out["outcome"] == "stage_failed_will_retry"


def test_a_proven_true_completes_silently(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True, **PROOF,
    }))
    assert out["outcome"] == "task_completed"
    assert _skipped_events(eng) == []
    status = eng.status("r1")["tasks"]["t1"]
    assert status["tests_meaningful"] is True and status["proving_mutations"] == 1


def test_a_stated_reason_passes_and_is_evented(tmp_path, project) -> None:
    """The escape hatch: an unproven pass completes, and never reads like a proven one."""
    for verdict, kind in ((None, "not_judged"), (True, "unproven")):
        eng = _engine(tmp_path / str(verdict), project)
        eng.create_run("r1", ExecutionLane.LITE)
        eng.add_task("r1", "t1")
        w = _lite_to_review(eng)
        out = eng.record("r1", make_result(w, structured_output={
            "approved": True, "issues": [], "tests_meaningful": verdict,
            "tests_not_checked_reason": "workspace origin was inconclusive",
        }))
        assert out["outcome"] == "task_completed"
        events = _skipped_events(eng)
        assert [e["kind"] for e in events] == [kind]
        assert events[0]["level"] == "warning"
        assert events[0]["stated_reason"] == "workspace origin was inconclusive"
        assert events[0]["task_id"] == "t1" and events[0]["stage"] == "review"
        assert eng.status("r1")["tasks"]["t1"]["proving_mutations"] == 0


def test_a_blank_reason_is_no_reason(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "tests_not_checked_reason": "   ",
    }))
    assert out["outcome"] == "stage_failed_will_retry"


def test_reviewer_false_on_engine_test_rejects_with_no_proof_needed(tmp_path, project) -> None:
    """ENGINE-lane TEST no longer exempts the veto (#541): a model wrote those tests, and on
    every lane nothing else judges them."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert _skipped_events(eng) == []


def test_docs_only_change_needs_no_proof_and_emits_nothing(tmp_path, project) -> None:
    """A genuinely absent test surface is the case the exemption exists for (#41) — an
    unjudged docs-only change is not a skipped verification, so it stays quiet."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng, test_output={
        "passed": True, "failures": [],
        "change_class": "docs-only", "skipped": "docs-only",
    })
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"
    assert _skipped_events(eng) == []


def test_micro_lane_without_a_test_stage_needs_no_proof(tmp_path, project) -> None:
    """A MICRO pipeline has no TEST stage at all: the lane declared no test verification at
    add_task time (a recorded lane choice, #168), so nothing was silently skipped here."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.MICRO)
    task = eng.add_task("r1", "t1")
    assert Stage.TEST not in task.pipeline
    for _ in range(3):  # intake, implement, deliver
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.REVIEW
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"
    assert _skipped_events(eng) == []


def test_suppressed_explicit_false_is_evented(tmp_path, project) -> None:
    """The docs-only exemption discards an EXPLICIT reviewer `false`. Behavior is unchanged
    (still no rejection), but the discarded verdict is no longer invisible."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng, test_output={
        "passed": True, "failures": [], "change_class": "docs-only", "skipped": "docs-only",
    })
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "task_completed"  # exemption still suppresses the rejection
    events = _skipped_events(eng)
    assert len(events) == 1
    assert events[0]["kind"] == "verdict_suppressed"
    assert events[0]["review_reported"] is False and events[0]["test_stage"] == "engine"
    assert events[0]["level"] == "warning"


def test_a_substantive_rejection_is_not_sent_back_for_proof(tmp_path, project) -> None:
    """A rejecting review re-runs after the fix anyway: approved=false still drives the fix
    cycle with no proof demanded, and an unproven verdict on it is still recorded as such."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False, "tests_meaningful": True, **PROOF,
        "issues": [{"severity": "critical", "description": "breaks the invariant"}],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert _skipped_events(eng) == []


def test_panel_review_without_a_tests_lens_is_sent_back(tmp_path, project) -> None:
    """The #73 panel folds to a null `tests_meaningful` when find:tests never reported (#261)
    — the gate reads the FOLDED output, so that is caught like a single reviewer's omission."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    panel = {"findings_by_lens": {"find:code": {"findings": []}}, "verdicts": []}
    out = eng.record("r1", make_result(w, sub_results=panel))
    assert out["outcome"] == "stage_failed_will_retry"


def test_panel_tests_lens_proof_reaches_the_gate(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    panel = {"findings_by_lens": {
        "find:code": {"findings": []},
        "find:tests": {"findings": [], "tests_meaningful": True, **PROOF},
    }, "verdicts": []}
    out = eng.record("r1", make_result(w, sub_results=panel))
    assert out["outcome"] == "task_completed"
    assert eng.status("r1")["tasks"]["t1"]["proving_mutations"] == 1


# --- the prompt side -----------------------------------------------------------------------
def test_review_prompt_requires_a_recorded_mutation_and_scopes_the_carve_out() -> None:
    """#261: the no-verdict carve-out is scoped to a genuinely absent test surface, while the
    `false`-is-a-rejection warning that #144/#168 exist for survives. #541: the check moved
    here from TEST, in one wording for the single reviewer and the panel's find:tests."""
    from orchestrator.stages import _LENS_TESTS, _MUTATION_PROOF_RECIPE, render_prompt

    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="",
                           context={"pr_url": "http://x/pr/1"})
    assert "## Reporting tests_meaningful" in prompt
    # #390 replaced the one-direction mutation sentence this used to assert; its own
    # regression coverage lives in tests/test_review_worktree_trust.py.
    assert "resolve an exercised module's `__file__`" in prompt
    assert "JUDGE `tests_meaningful` whenever this change has tests you can read" in prompt
    assert "Answer null ONLY when there is genuinely NO test surface" in prompt
    assert "reads as a rejection" in prompt  # the #144 misfire guard is NOT relaxed
    assert _MUTATION_PROOF_RECIPE in prompt
    assert _MUTATION_PROOF_RECIPE in _LENS_TESTS.body
    assert "single window" in _MUTATION_PROOF_RECIPE  # the #1028 lesson
    assert "does not mention" in _MUTATION_PROOF_RECIPE  # the #392 lesson, moved from TEST
    assert "JUDGE it whenever there are tests to read" in _LENS_TESTS.body
    assert "answer null ONLY when the change has genuinely no test surface" in _LENS_TESTS.body


def test_a_lane_with_no_disposable_workspace_says_not_to_mutate() -> None:
    """Interactive REVIEW runs in the live worktree, so a mutation would edit the judged tree.
    Stated to the single reviewer and find:tests alone; other lanes' prompts omit it."""
    from orchestrator.stages import render_prompt, render_review_plan

    heading = "## No disposable workspace on this lane"
    common = {"task_id": "t1", "title": "x", "body": ""}
    assert heading in render_prompt(Stage.REVIEW, tool_posture_unenforced=True, **common)
    assert heading not in render_prompt(Stage.REVIEW, **common)
    plan = render_review_plan(no_disposable_workspace=True, **common)
    assert [f.lens for f in plan.finders if heading in f.prompt] == ["find:tests"]
    assert not any(heading in f.prompt for f in render_review_plan(**common).finders)


def test_test_prompt_and_persona_no_longer_ask_for_a_meaningfulness_judgment() -> None:
    from pathlib import Path

    from orchestrator.stages import render_prompt

    prompt = render_prompt(Stage.TEST, task_id="t1", title="x", body="",
                           test_verdict_verified=True)
    assert "meaningful" not in prompt and "mutation" not in prompt
    for persona in (".claude/agents/test-validator.md",
                    "templates/project-default/agents/test-validator.md"):
        text = Path(persona).read_text()
        assert "`tests_meaningful`" not in text and "vacuous" not in text


def test_a_null_context_value_renders_as_an_abstention_not_the_word_none() -> None:
    """A folded null renders as an abstention, not the literal string "None" (which a reader
    could take for a value)."""
    from orchestrator.stages import render_prompt

    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="",
                           context={"blocked_reason": None})
    assert "- blocked_reason: (not reported)" in prompt
    assert "blocked_reason: None" not in prompt


def test_an_old_task_context_with_tests_meaningful_still_renders() -> None:
    """A task persisted before #541 carries the key in its context; it is simply not shown."""
    from orchestrator.stages import render_prompt

    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="",
                           context={"tests_meaningful": True, "plan": ["p"]})
    assert "- plan: p" in prompt
    assert "- tests_meaningful:" not in prompt


# --- the pure helper -------------------------------------------------------------------
def _task(**kw) -> Task:
    return Task(task_id="t1", run_id="r1", created_at="x", updated_at="x", **kw)


def test_notice_helper_is_scoped_to_a_completed_review() -> None:
    """PURE and narrow: only a COMPLETED REVIEW result is judged (a failed review is
    re-run, and no other stage carries the verdict)."""
    task = _task(pipeline=[Stage.TEST, Stage.REVIEW], deterministic_stages=[Stage.TEST])
    review = make_result(
        _WorkItemStub(Stage.REVIEW), structured_output={"approved": True, "issues": []}
    )
    assert unjudged_tests_notice(task, review) is not None  # the baseline case
    failed = review.model_copy(update={"status": ResultStatus.FAILURE})
    assert unjudged_tests_notice(task, failed) is None
    test_stage = review.model_copy(update={"stage": Stage.TEST})
    assert unjudged_tests_notice(task, test_stage) is None
    # ... and it is a pure function of its inputs: same task+result, same answer.
    assert unjudged_tests_notice(task, review) == unjudged_tests_notice(task, review)


def test_a_non_boolean_review_report_is_unjudged_and_bounded_in_the_notice() -> None:
    """A model may answer with prose instead of a boolean; every gate ignores that as
    non-boolean, so it counts as UNJUDGED here — and the value is bounded before it reaches
    the event (#201's drop-notice bound), so an oversized answer can't bloat the audit line."""
    task = _task(pipeline=[Stage.TEST, Stage.REVIEW], deterministic_stages=[Stage.TEST])
    result = make_result(_WorkItemStub(Stage.REVIEW), structured_output={
        "approved": True, "issues": [], "tests_meaningful": "yes, " + "x" * 5000,
    })
    notice = unjudged_tests_notice(task, result)
    assert notice is not None and notice["kind"] == "not_judged"
    reported = notice["review_reported"]
    assert isinstance(reported, str) and len(reported) < 300 and reported.endswith("[truncated]")
    assert review_proof_gate(task, result) is not None  # prose is not a verdict either


def test_proof_gate_is_scoped_to_a_completed_approval_on_a_test_surface() -> None:
    task = _task(pipeline=[Stage.TEST, Stage.REVIEW])
    review = make_result(
        _WorkItemStub(Stage.REVIEW), structured_output={"approved": True, "issues": []}
    )
    assert review_proof_gate(task, review) is not None  # the baseline case
    failed = review.model_copy(update={"status": ResultStatus.FAILURE})
    assert review_proof_gate(task, failed) is None
    assert review_proof_gate(task, review.model_copy(update={"stage": Stage.TEST})) is None
    assert review_proof_gate(_task(pipeline=[Stage.REVIEW]), review) is None
    docs = _task(pipeline=[Stage.TEST, Stage.REVIEW], context={"change_class": "docs-only"})
    assert review_proof_gate(docs, review) is None


def test_proving_mutation_count_tolerates_any_shape() -> None:
    assert proving_mutation_count({}) == 0
    assert proving_mutation_count({"mutation_checks": "ran some"}) == 0
    assert proving_mutation_count({"mutation_checks": [
        "not an object",
        {"mutation": "", "failed_tests": ["t::x"]},  # no mutation named
        {"mutation": "a.py:1", "failed_tests": []},  # survived
        {"mutation": "a.py:1", "failed_tests": "t::x"},  # malformed
        {"mutation": "a.py:1", "failed_tests": ["t::x"]},
    ]}) == 1


def test_no_test_surface_reads_only_engine_side_state() -> None:
    """The exemption the engine's #13 gate, the proof gate and the #261 notice SHARE. Both
    signals are engine-side facts (the pipeline / the ENGINE-lane change_class tag), so a
    model cannot self-exempt by anything it returns. ENGINE-lane TEST is not a signal (#541)."""
    assert not no_test_surface(_task(pipeline=[Stage.TEST, Stage.REVIEW]))
    assert not no_test_surface(
        _task(pipeline=[Stage.TEST, Stage.REVIEW], deterministic_stages=[Stage.TEST])
    )
    assert no_test_surface(_task(pipeline=[Stage.REVIEW]))  # micro: no TEST stage
    docs = _task(pipeline=[Stage.TEST, Stage.REVIEW], context={"change_class": "docs-only"})
    assert no_test_surface(docs)


class _WorkItemStub:
    """The two fields ``make_result`` reads off a WorkItem, for the pure-helper tests."""

    def __init__(self, stage: Stage) -> None:
        from orchestrator.schemas.enums import Effort, ExecutionMode, Provider
        from orchestrator.schemas.work import LanePolicy

        self.stage = stage
        self.id = "wi"
        self.content_hash = "h"
        self.run_id = "r1"
        self.task_id = "t1"
        self.attempt = 0
        self.model = "claude-opus-5"
        self.effort = Effort.MEDIUM
        self.lane_policy = LanePolicy(
            execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE
        )
