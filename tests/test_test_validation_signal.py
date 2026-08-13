"""#261: when NOBODY judged test-meaningfulness, the run says so.

The hole this pins (observed on #255, a real 7-test code change on the `lite` lane): the
deterministic ENGINE-lane TEST runner cannot judge whether tests are meaningful and defers
to REVIEW; the REVIEW model omitted `tests_meaningful` and deferred to TEST. Each side
deferred to the other, neither did it, the fail-OPEN gate passed — and the stage record
carried an affirmative `tests_meaningful: true` nobody had earned.

Two halves are pinned here:
  * the honest encoding — the ENGINE lane makes NO claim (null), covered in
    tests/test_deterministic_test_deliver.py and the fold's null in test_review_workflow.py;
  * the SIGNAL — a warning-grade `test_validation_skipped` event whenever a plausible test
    surface went unjudged, or a reviewer's explicit verdict was suppressed by the
    #41/#168 no-model-test-surface exemption.

Fail-OPEN is deliberate and load-bearing, so every case below must still COMPLETE the task:
the fix is to stop claiming a verification that did not happen, not to start blocking.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage
from orchestrator.schemas.status import Task
from orchestrator.state_machine import no_model_test_surface, unjudged_tests_notice
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# What the deterministic ENGINE-lane TEST runner now returns (#261): a real suite run with
# NO meaningfulness claim. `change_class` folds because the lane is ENGINE.
ENGINE_TEST_OUTPUT = {
    "passed": True,
    "failures": [],
    "tests_meaningful": None,
    "change_class": "code",
    "validation_notes": "unit: green; tests_meaningful not judged by this runner (null)",
}


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


# --- the headline case ------------------------------------------------------------------
def test_lite_lane_nobody_judged_emits_a_warning_and_still_completes(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))

    assert out["outcome"] == "task_completed"  # fail-OPEN is unchanged: never blocking
    events = _skipped_events(eng)
    assert len(events) == 1
    ev = events[0]
    assert ev["level"] == "warning"  # warning-grade: a skipped verification, not a debug note
    assert ev["kind"] == "not_judged"
    assert ev["test_stage"] == "engine"  # the runner that cannot judge meaningfulness
    assert ev["test_reported"] is None and ev["review_reported"] is None
    assert "did not happen" in ev["reason"]
    assert ev["task_id"] == "t1" and ev["stage"] == "review"
    # ... and the record itself no longer claims otherwise.
    assert eng.store.load_task("r1", "t1").context["tests_meaningful"] is None


def test_review_judging_it_emits_nothing(tmp_path, project) -> None:
    """The whole point: a REVIEW that answers the question needs no warning — either way."""
    for verdict, expected in ((True, "task_completed"), (False, "task_completed")):
        eng = _engine(tmp_path / str(verdict), project)
        eng.create_run("r1", ExecutionLane.LITE)
        eng.add_task("r1", "t1")
        w = _lite_to_review(eng)
        out = eng.record("r1", make_result(w, structured_output={
            "approved": True, "issues": [], "tests_meaningful": verdict,
        }))
        assert out["outcome"] == expected
        # `false` on the lite lane is suppressed by the #168 exemption — that suppression IS
        # evented (below); a `true` is a real judgment and is silent.
        assert _skipped_events(eng) if verdict is False else not _skipped_events(eng)


def test_model_test_stage_judging_it_emits_nothing(tmp_path, project) -> None:
    """A FULL-lane model TEST stage really does judge meaningfulness, so a REVIEW that omits
    the field is not the #261 hole — the guarantee was met once, by the other side."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    for _ in range(5):  # intake, scope, implement, test (model lane, reports true), deliver
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.REVIEW
    assert eng.store.load_task("r1", "t1").context["tests_meaningful"] is True
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"
    assert _skipped_events(eng) == []


def test_docs_only_change_emits_nothing(tmp_path, project) -> None:
    """A genuinely absent test surface is the case fail-OPEN-on-absent exists for (#41) —
    an unjudged docs-only change is not a skipped verification, so it stays quiet."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng, test_output={
        "passed": True, "failures": [], "tests_meaningful": None,
        "change_class": "docs-only", "skipped": "docs-only",
    })
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"
    assert _skipped_events(eng) == []


def test_micro_lane_without_a_test_stage_emits_nothing(tmp_path, project) -> None:
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


# --- the other half of the mutual deference ---------------------------------------------
def test_suppressed_explicit_false_is_evented(tmp_path, project) -> None:
    """The #168 exemption discards an EXPLICIT reviewer `false` on the lite lane — even
    though a model (IMPLEMENT) wrote those tests. Behavior is unchanged (still no rejection),
    but the discarded verdict is no longer invisible."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "task_completed"  # exemption still suppresses the rejection
    events = _skipped_events(eng)
    assert len(events) == 1
    assert events[0]["kind"] == "verdict_suppressed"
    assert events[0]["review_reported"] is False and events[0]["test_stage"] == "engine"
    assert events[0]["level"] == "warning"


def test_a_substantive_rejection_is_unaffected(tmp_path, project) -> None:
    """The signal is observability only: an explicit approved=false still drives the fix
    cycle, and a rejection whose reviewer judged the tests emits no skip warning."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False, "tests_meaningful": True,
        "issues": [{"severity": "critical", "description": "breaks the invariant"}],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert _skipped_events(eng) == []


def test_panel_review_without_a_tests_lens_emits_the_warning(tmp_path, project) -> None:
    """The #73 panel path folds to a null `tests_meaningful` when find:tests never reported
    (#261) — which the same signal must catch, exactly as a single reviewer's omission."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    panel = {"findings_by_lens": {"find:code": {"findings": []}}, "verdicts": []}
    out = eng.record("r1", make_result(w, sub_results=panel))
    assert out["outcome"] == "task_completed"
    assert [e["kind"] for e in _skipped_events(eng)] == ["not_judged"]


# --- the prompt side (what invited the omission) -----------------------------------------
def test_review_prompt_scopes_the_omission_carve_out_to_an_absent_test_surface() -> None:
    """#261: the old wording ("Only set it when there ARE tests to judge … OMIT the field
    entirely") read as broad permission to omit and #255's reviewer took it on a change with
    7 new tests. The carve-out is now explicitly scoped to a genuinely absent test surface,
    and an omission is stated to be recorded/evented rather than passing quietly — while the
    `false`-is-a-rejection warning that #144/#168 exist for survives."""
    from orchestrator.stages import _LENS_TESTS, render_prompt

    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="",
                           context={"pr_url": "http://x/pr/1"})
    assert "## Reporting tests_meaningful" in prompt
    assert "resolve the exercised module's `__file__`" in prompt
    assert "belongs to this review worktree" in prompt
    assert "JUDGE `tests_meaningful` whenever this change has tests you can read" in prompt
    assert "OMIT the field ONLY when there is genuinely NO test surface" in prompt
    assert "not judged" in prompt and "NOT a pass" in prompt
    assert "reads as a rejection" in prompt  # the #144 misfire guard is NOT relaxed
    # The panel's find:tests finder (#73) must say the same thing — same question, same rules.
    assert "JUDGE it whenever there are tests to read" in _LENS_TESTS.body
    assert "OMIT it ONLY when the change has genuinely no test surface" in _LENS_TESTS.body
    assert "not a pass" in _LENS_TESTS.body


def test_a_null_context_value_renders_as_an_abstention_not_the_word_none() -> None:
    """The deterministic runner's `tests_meaningful: null` folds into the context plane and
    is rendered into the REVIEW prompt: it must read as an abstention, not the literal
    string "None" (which a reader could take for a value)."""
    from orchestrator.stages import render_prompt

    prompt = render_prompt(Stage.REVIEW, task_id="t1", title="x", body="",
                           context={"tests_meaningful": None})
    assert "- tests_meaningful: (not reported)" in prompt
    assert "tests_meaningful: None" not in prompt


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


def test_no_model_test_surface_reads_only_add_task_state() -> None:
    """The exemption the engine's #13 gate and the #261 notice SHARE. All three signals are
    engine-side facts (pipeline / deterministic_stages / the ENGINE-lane change_class tag),
    so a model cannot self-exempt by anything it returns."""
    full = _task(pipeline=[Stage.TEST, Stage.REVIEW])
    assert not no_model_test_surface(full)
    assert no_model_test_surface(_task(pipeline=[Stage.REVIEW]))  # micro: no TEST stage
    assert no_model_test_surface(
        _task(pipeline=[Stage.TEST, Stage.REVIEW], deterministic_stages=[Stage.TEST])
    )
    docs = _task(pipeline=[Stage.TEST, Stage.REVIEW], context={"change_class": "docs-only"})
    assert no_model_test_surface(docs)


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
