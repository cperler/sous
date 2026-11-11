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
                    ".codex/agents/test-validator.toml",
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


# --- #614: the stopping rule for surviving mutants -----------------------------------------
BLOCKING_SURVIVOR = {
    "mutation": "orchestrator/a.py:12 drop the refusal guard",
    "failed_tests": [],
    "blocking": True,
    "blocking_reason": "the spec requires the refusal on an unknown maker",
}


def _survivor_events(eng, kind, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == kind]


def test_a_blocking_survivor_with_a_reason_opens_a_fix_cycle_and_names_itself(
    tmp_path, project
) -> None:
    """The engine, not the reviewer, opens the cycle: the reviewer APPROVED with the survivor
    only in mutation_checks, and the rule turns it into the blocking issue."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [PROOF["mutation_checks"][0], BLOCKING_SURVIVOR],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    t = eng.store.load_task("r1", "t1")
    assert t.open_mutants == ["orchestrator/a.py:12 drop the refusal guard"]
    assert t.review_cycles == 1
    # The fix cycle reset the REVIEW record, so the promoted issue is read where the
    # implementer reads it: the rejection learning, rendered by format_review_issue.
    [learning] = [x for x in t.learnings if x.startswith("review rejected (cycle 1)")]
    assert "important — orchestrator/a.py:12 — surviving mutant: orchestrator/a.py:12 drop " \
           "the refusal guard — the spec requires the refusal on an unknown maker" in learning
    [receipt] = _survivor_events(eng, "surviving_mutants_disposed")
    assert receipt["blocking"] == 1 and receipt["noted"] == 0 and receipt["demoted"] == 0
    assert receipt["blocking_mutations"] == t.open_mutants
    assert _survivor_events(eng, "surviving_mutant_demoted") == []
    # The fix cycle reset the REVIEW record, so status carries no verdict until the re-review.
    assert "surviving_mutations" not in eng.status("r1")["tasks"]["t1"]
    # The fix cycle re-opens IMPLEMENT with the survivor as a learning.
    assert eng.next_work("r1", "t1").stage is Stage.IMPLEMENT


def test_a_blocking_flag_without_a_reason_is_demoted_and_opens_no_cycle(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    unexplained = {**BLOCKING_SURVIVOR, "blocking_reason": "  "}
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [PROOF["mutation_checks"][0], unexplained],
    }))
    assert out["outcome"] == "task_completed"
    t = eng.store.load_task("r1", "t1")
    assert t.open_mutants == [] and t.review_cycles == 0
    [demoted] = _survivor_events(eng, "surviving_mutant_demoted")
    assert demoted["level"] == "warning"
    assert demoted["mutation"] == "orchestrator/a.py:12 drop the refusal guard"
    [receipt] = _survivor_events(eng, "surviving_mutants_disposed")
    assert (receipt["blocking"], receipt["noted"], receipt["demoted"]) == (0, 1, 1)
    [gap] = t.stages[Stage.REVIEW].output["non_blocking"]
    assert gap["disposition"] == "drop" and gap["title"].startswith("surviving mutant:")
    status = eng.status("r1")["tasks"]["t1"]
    assert status["surviving_mutations"] == 1 and status["blocking_mutations"] == 0


def test_a_plain_survivor_is_a_noted_gap_and_the_task_completes(tmp_path, project) -> None:
    """The pred-1141-1291 shape: correct code, a wording-only survivor. Noted once, no cycle,
    and a reviewer that had already listed the same gap does not get it doubled."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [
            PROOF["mutation_checks"][0],
            {"mutation": "b.py:4 change the account tag text", "failed_tests": []},
            {"mutation": "c.py:9 reword the log line", "failed_tests": [],
             "blocking": False, "blocking_reason": "label only"},
        ],
        "non_blocking": [{"title": "surviving mutant: b.py:4 change the account tag text",
                          "detail": "already noted by the reviewer", "disposition": "drop"}],
    }))
    assert out["outcome"] == "task_completed"
    t = eng.store.load_task("r1", "t1")
    nb = t.stages[Stage.REVIEW].output["non_blocking"]
    assert [e["title"] for e in nb] == [
        "surviving mutant: b.py:4 change the account tag text",
        "surviving mutant: c.py:9 reword the log line",
    ]
    assert nb[0]["detail"] == "already noted by the reviewer"  # the reviewer's entry won
    assert "Reviewer note: label only" in nb[1]["detail"]
    assert t.stages[Stage.REVIEW].output["approved"] is True
    [receipt] = _survivor_events(eng, "surviving_mutants_disposed")
    assert (receipt["blocking"], receipt["noted"], receipt["demoted"]) == (0, 2, 0)
    assert eng.status("r1")["tasks"]["t1"]["surviving_mutations"] == 2


def test_no_survivors_leaves_the_review_byte_identical_and_silent(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True, **PROOF,
    }))
    assert _survivor_events(eng, "surviving_mutants_disposed") == []
    out = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output
    assert "non_blocking" not in out


def _cycle_back_to_review(eng, run="r1", task="t1"):
    """After a review rejection on the LITE lane: implement, TEST-on-ENGINE, deliver."""
    eng.record(run, make_result(eng.next_work(run, task)))  # implement
    wt = eng.next_work(run, task)
    assert wt.stage is Stage.TEST
    eng.record(run, make_result(wt, structured_output=ENGINE_TEST_OUTPUT))
    eng.record(run, make_result(eng.next_work(run, task)))  # deliver
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


def test_the_re_review_is_told_the_named_survivors_and_clears_them_when_closed(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    assert "## Named survivors to re-check" not in w.prompt  # first review: byte-identical
    eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [PROOF["mutation_checks"][0], BLOCKING_SURVIVOR],
    }))
    w2 = _cycle_back_to_review(eng)
    assert "## Named survivors to re-check" in w2.prompt
    assert "- orchestrator/a.py:12 drop the refusal guard" in w2.prompt
    # The re-review re-applies it (verbatim up to whitespace/case) and it now fails a test.
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [{"mutation": "orchestrator/a.py:12  Drop the refusal guard",
                             "failed_tests": ["tests/test_a.py::test_refuses_unknown_maker"]}],
    }))
    assert out["outcome"] == "task_completed"
    assert eng.store.load_task("r1", "t1").open_mutants == []
    assert _survivor_events(eng, "named_survivor_unchecked") == []


def test_a_re_review_that_ignores_the_named_survivors_is_evented_not_vetoed(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [PROOF["mutation_checks"][0], BLOCKING_SURVIVOR],
    }))
    w2 = _cycle_back_to_review(eng)
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True, **PROOF,  # a fresh sample
    }))
    assert out["outcome"] == "task_completed"
    [missed] = _survivor_events(eng, "named_survivor_unchecked")
    assert missed["level"] == "warning"
    assert missed["mutations"] == ["orchestrator/a.py:12 drop the refusal guard"]
    assert eng.store.load_task("r1", "t1").open_mutants == []  # the approval still clears


def test_a_second_rejection_replaces_the_named_survivors(tmp_path, project) -> None:
    """The list is the LAST rejection's, so a re-review that found a different blocking
    survivor hands the next cycle only that one."""
    eng = _engine(tmp_path, project, max_review_cycles=3)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    w = _lite_to_review(eng)
    eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [PROOF["mutation_checks"][0], BLOCKING_SURVIVOR],
    }))
    w2 = _cycle_back_to_review(eng)
    other = {**BLOCKING_SURVIVOR, "mutation": "orchestrator/b.py:3 skip the halt",
             "blocking_reason": "the halt path is a money-path guard"}
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": True, "issues": [], "tests_meaningful": True,
        "mutation_checks": [{**BLOCKING_SURVIVOR, "failed_tests": ["t::closed"]}, other],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert eng.store.load_task("r1", "t1").open_mutants == ["orchestrator/b.py:3 skip the halt"]


def test_review_prompts_state_the_stopping_rule_and_the_re_check_block_only_when_named(
) -> None:
    from orchestrator.stages import (
        _LENS_TESTS,
        _MUTATION_PROOF_RECIPE,
        _SURVIVOR_STOPPING_RULE,
        render_prompt,
        render_review_plan,
    )

    assert _SURVIVOR_STOPPING_RULE in _MUTATION_PROOF_RECIPE
    assert _SURVIVOR_STOPPING_RULE in _LENS_TESTS.body
    assert "do NOT restate it in `issues`" in _SURVIVOR_STOPPING_RULE
    assert "money-path / safety" in _SURVIVOR_STOPPING_RULE
    common = {"task_id": "t1", "title": "x", "body": ""}
    prompt = render_prompt(Stage.REVIEW, **common)
    assert _SURVIVOR_STOPPING_RULE in prompt
    assert "blocking?, blocking_reason?" in prompt
    heading = "## Named survivors to re-check"
    assert heading not in prompt
    assert heading not in render_prompt(Stage.REVIEW, open_mutants=["  "], **common)
    named = render_prompt(Stage.REVIEW, open_mutants=["a.py:1 drop guard"], **common)
    assert heading in named and "- a.py:1 drop guard" in named and "VERBATIM" in named
    plan = render_review_plan(open_mutants=["a.py:1 drop guard"], **common)
    assert [f.lens for f in plan.finders if heading in f.prompt] == ["find:tests"]
    assert not any(heading in f.prompt for f in render_review_plan(**common).finders)


def test_surviving_mutant_disposition_tolerates_any_shape() -> None:
    from orchestrator.state_machine import (
        surviving_mutant_disposition,
        unchecked_named_survivors,
    )

    assert surviving_mutant_disposition({}) == ([], [], [])
    assert surviving_mutant_disposition({"mutation_checks": "ran some"}) == ([], [], [])
    blocking, noted, demoted = surviving_mutant_disposition({"mutation_checks": [
        "oops", {"failed_tests": []}, {"mutation": "   ", "failed_tests": []},
        {"mutation": "a.py:1 x", "failed_tests": ["t"]},  # proving: untouched
        {"mutation": "b.py:2 y", "failed_tests": "none"},  # malformed proof: a survivor
        {"mutation": "drop the guard", "failed_tests": [], "blocking": "true",
         "blocking_reason": "spec"},  # no file:line prefix → no file, still blocking
        {"mutation": "c.py:3 z", "failed_tests": [], "blocking": True},
    ]})
    assert [n["title"] for n in noted] == ["surviving mutant: b.py:2 y",
                                           "surviving mutant: c.py:3 z"]
    assert [b["mutation"] for b in blocking] == ["drop the guard"]
    assert "file" not in blocking[0] and "line" not in blocking[0]
    assert [d["mutation"] for d in demoted] == ["c.py:3 z"]
    assert unchecked_named_survivors([], {"mutation_checks": []}) == []
    assert unchecked_named_survivors(["A.py:1  x", "b.py:2 y"], {
        "mutation_checks": [{"mutation": "a.py:1 x", "failed_tests": []}, "oops"],
    }) == ["b.py:2 y"]
