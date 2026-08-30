"""The review gate: a completed REVIEW that reports approved=false must never
fall through to task_completed. While fix cycles remain, the engine re-opens
implement→…→review with the blocking issues as learnings (the old system's
quality/fix loop, restored bounded); at max_review_cycles it parks the task
BLOCKED_ON_HUMAN with REVIEW re-opened so approve() leads to a re-review.
Suggestion-only rejections auto-approve (the old severity gate)."""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.render import format_review_issue
from orchestrator.schemas.enums import ResultStatus, Stage, StageStatus, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import finish_after_review, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_review(eng, run="r1", task="t1"):
    """Drive intake→…→deliver green; return the REVIEW WorkItem."""
    for _ in range(5):  # intake, scope, implement, test, deliver
        eng.record(run, make_result(eng.next_work(run, task)))
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


REJECTION = {
    "approved": False,
    "issues": [
        {"severity": "critical", "file": "a.py", "line": 12,
         "description": "breaks the invariant", "suggested_fix": "guard the None case"},
        "second issue as a plain string",
    ],
}


def test_rejection_triggers_fix_cycle(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output=REJECTION))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert out["task_state"] == "retrying"
    assert out["next_stage"] == "implement"  # pipeline re-opened from implement

    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1
    for stage in (Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER, Stage.REVIEW):
        assert task.stages[stage].status is StageStatus.PENDING
    # earlier stages keep their completion
    assert task.stages[Stage.INTAKE].status is StageStatus.COMPLETED
    assert task.stages[Stage.SCOPE].status is StageStatus.COMPLETED
    # the reviewer's session must not leak into the fix work
    assert task.session_ref is None

    # the fix implement's prompt carries the blocking issues as learnings
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.IMPLEMENT
    assert "review rejected" in nxt.prompt
    assert "breaks the invariant" in nxt.prompt
    assert "second issue as a plain string" in nxt.prompt


def test_fix_cycle_then_approval_completes(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    eng.record("r1", make_result(w, structured_output=REJECTION))
    # fix cycle: implement, test, deliver run again, then review approves
    for _ in range(3):
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w2 = eng.next_work("r1", "t1")
    assert w2.stage is Stage.REVIEW
    out = eng.record("r1", make_result(w2, structured_output={"approved": True, "issues": []}))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED


FIXUP = {
    "title": "Keep the completion evidence honest",
    "detail": "Render applied only after the re-review accepts the in-place edit.",
    "disposition": "fixup",
}


def _drive_fixup_tail(eng: Engine):
    """Drive the re-opened IMPLEMENT→TEST→DELIVER tail and return its REVIEW.

    PUBLISH sits behind that REVIEW since #389; callers that need the task to COMPLETE
    drive it with ``finish_after_review``."""
    stages = []
    for _ in range(3):
        work = eng.next_work("r1", "t1")
        stages.append(work.stage)
        eng.record("r1", make_result(work))
    assert stages == [Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER]
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.REVIEW
    return work


def test_approved_fixup_reimplements_redelivers_and_records_application(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    review = _advance_to_review(eng)
    result = make_result(
        review,
        structured_output={"approved": True, "issues": [], "improvement": FIXUP},
    )

    out = eng.record("r1", result)
    assert out["outcome"] == "review_fixup_cycle"
    assert out["next_stage"] == "implement"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1
    assert len(task.review_fixups) == 1 and task.review_fixups[0].applied is False
    assert FIXUP["title"] in task.learnings[-1]
    assert FIXUP["detail"] in task.learnings[-1]
    for stage in (Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER, Stage.REVIEW):
        assert task.stages[stage].status is StageStatus.PENDING

    # A result replay cannot schedule a second pass: the closed lease rejects it before
    # mutation, and the atomic stage-event batch contains one scheduled event.
    with pytest.raises(ContractError):
        eng.record("r1", result)
    assert len([
        event for event in eng.store.read_events("r1")
        if event["type"] == "review_fixup_scheduled"
    ]) == 1

    final_review = _drive_fixup_tail(eng)
    out = eng.record(
        "r1", make_result(final_review, structured_output={"approved": True, "issues": []})
    )
    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    task = eng.store.load_task("r1", "t1")
    assert task.review_fixups[0].applied is True
    events = eng.store.read_events("r1")
    assert len([event for event in events if event["type"] == "review_fixup_applied"]) == 1
    # The original request survived the REVIEW-record reset and is reported from actual
    # application evidence.  It never became a backlog issue.
    note = project.task_source.notes[0]["body"]
    assert FIXUP["title"] in note
    assert "applied in place, not filed" in note
    assert not any(followup["labels"] == ["enhancement"]
                   for followup in project.task_source.followups)


def test_rejected_review_combines_fixup_with_its_existing_cycle(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    rejected = {**REJECTION, "improvement": FIXUP}

    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output=rejected))

    assert out["outcome"] == "review_rejected_fix_cycle"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1  # one combined pass, not two resets/cycles
    assert len(task.review_fixups) == 1
    assert "blocking issues" in task.learnings[-2]
    assert FIXUP["title"] in task.learnings[-1]
    event = next(e for e in eng.store.read_events("r1")
                 if e["type"] == "review_fixup_scheduled")
    assert event["reason"] == "combined with blocking-review fix cycle"


def test_rejected_review_holds_fixup_when_combined_cycle_cannot_redeliver(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.INTAKE, Stage.IMPLEMENT, Stage.REVIEW])
    rejected = {**REJECTION, "improvement": FIXUP}
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    review = eng.next_work("r1", "t1")
    assert review.stage is Stage.REVIEW

    out = eng.record("r1", make_result(review, structured_output=rejected))

    assert out["outcome"] == "review_rejected_fix_cycle"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1
    assert task.review_fixups == []
    held = next(e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held")
    assert "no IMPLEMENT→DELIVER→REVIEW→PUBLISH tail" in held["reason"]

    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    final_review = eng.next_work("r1", "t1")
    assert final_review.stage is Stage.REVIEW
    out = eng.record(
        "r1", make_result(final_review, structured_output={"approved": True, "issues": []})
    )
    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    assert "applied in place, not filed" not in project.task_source.notes[0]["body"]


def test_repeated_fixup_parks_instead_of_looping(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    output = {"approved": True, "issues": [], "improvement": FIXUP}
    eng.record("r1", make_result(_advance_to_review(eng), structured_output=output))

    out = eng.record("r1", make_result(_drive_fixup_tail(eng), structured_output=output))

    assert out["outcome"] == "review_fixup_held"
    assert out["task_state"] == "blocked_on_human"
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.REVIEW].status is StageStatus.FAILED
    assert len(task.review_fixups) == 1 and task.review_fixups[0].applied is False
    held = [e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held"]
    assert len(held) == 1 and "requested again" in held[0]["reason"]


@pytest.mark.parametrize(
    ("pipeline", "reason"),
    [
        ([Stage.INTAKE, Stage.REVIEW], "no IMPLEMENT→DELIVER→REVIEW→PUBLISH tail"),
        (
            [Stage.REVIEW, Stage.IMPLEMENT, Stage.DELIVER, Stage.PUBLISH],
            "does not order IMPLEMENT→DELIVER→REVIEW→PUBLISH",
        ),
    ],
)
def test_fixup_parks_when_pipeline_cannot_reimplement(
    tmp_path, project, pipeline, reason
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=pipeline)
    while (review := eng.next_work("r1", "t1")).stage is not Stage.REVIEW:
        eng.record("r1", make_result(review))

    out = eng.record(
        "r1",
        make_result(
            review,
            structured_output={"approved": True, "issues": [], "improvement": FIXUP},
        ),
    )

    assert out["outcome"] == "review_fixup_held"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.BLOCKED_ON_HUMAN
    assert task.review_fixups == []
    held = next(e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held")
    assert reason in held["reason"]


def test_rejection_parks_when_cycles_exhausted(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_review_cycles=0)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output=REJECTION))
    assert out["outcome"] == "review_rejected_held"
    assert out["task_state"] == "blocked_on_human"

    task = eng.store.load_task("r1", "t1")
    # REVIEW re-opened as FAILED: after approve() the pipeline still has a next stage
    assert task.stages[Stage.REVIEW].status is StageStatus.FAILED
    assert "review rejected" in (task.last_error or "")
    # parked, not dispatchable
    assert eng.next_work("r1", "t1") is None
    # approve() releases to a re-review, never a zombie with no next stage
    eng.approve("r1", "t1", approved_by="craig")
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.REVIEW
    # the run stayed open throughout (BLOCKED_ON_HUMAN is non-terminal)
    assert eng.store.load_run("r1").state.value == "running"


def test_suggestion_only_rejection_auto_approves(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False,
        "issues": [
            {"severity": "suggestion", "description": "could rename the helper"},
            {"severity": "suggestion", "description": "docstring nit"},
        ],
    }))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # severity gate: suggestions never block
    events = eng.store.read_events("r1")
    verdicts = [e for e in events if e["type"] == "review_verdict"]
    assert verdicts and verdicts[-1]["kind"] == "auto_approved"


def test_reviewer_vacuous_tests_verdict_rejects_even_when_approved(tmp_path, project) -> None:
    """The independent test-validate half (#13): the reviewer (a different agent from
    the test writer) reporting tests_meaningful=false rejects an otherwise-approved
    review and drives the fix cycle."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    task = eng.store.load_task("r1", "t1")
    assert "independent test-validate" in task.learnings[-1]
    # fail-open unchanged: omitting the field on an approved review completes
    for _ in range(3):
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w2 = eng.next_work("r1", "t1")
    out2 = eng.record("r1", make_result(w2, structured_output={"approved": True, "issues": []}))
    assert finish_after_review(eng, out2)["outcome"] == "task_completed"


def test_deterministic_test_stage_exempts_vacuous_tests_rejection(tmp_path, project) -> None:
    """#168: a task whose TEST stage runs on the deterministic $0 ENGINE lane has no
    model-written/graded tests for the #13 gate to bite on, so a reviewer's
    tests_meaningful=false must NOT reject an otherwise-approved review (the #144 misfire)."""
    from orchestrator.schemas.enums import Stage as _Stage
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", deterministic_stages=[_Stage.TEST, _Stage.DELIVER])
    assert _Stage.TEST in task.deterministic_stages
    w = _advance_to_review(eng)  # FULL pipeline (6 stages), TEST just runs on ENGINE lane
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # deterministic TEST → no model test surface


def test_micro_no_test_stage_exempts_vacuous_tests_rejection(tmp_path, project) -> None:
    """#168: a MICRO task has no TEST stage at all — no test surface — so a reviewer's
    tests_meaningful=false must NOT reject an otherwise-approved review."""
    from orchestrator.schemas.enums import ExecutionLane
    from orchestrator.schemas.enums import Stage as _Stage
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.MICRO)
    task = eng.add_task("r1", "t1")
    assert _Stage.TEST not in task.pipeline  # micro: intake, implement, deliver, review
    for _ in range(3):  # intake, implement, deliver
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.REVIEW
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"


def _advance_to_review_with_test(eng, *, test_output, test_mode=None, test_provider=None,
                                 run="r1", task="t1"):
    """Drive intake→scope→implement with defaults, record TEST with a custom result, then
    deliver; return the REVIEW WorkItem. Lets a test inject a change_class-tagged TEST."""
    from orchestrator.schemas.enums import ExecutionMode, Provider
    for _ in range(3):  # intake, scope, implement
        eng.record(run, make_result(eng.next_work(run, task)))
    wt = eng.next_work(run, task)
    assert wt.stage is Stage.TEST
    eng.record(run, make_result(
        wt, structured_output=test_output,
        mode=test_mode or ExecutionMode.ENGINE, provider=test_provider or Provider.NONE,
    ))
    eng.record(run, make_result(eng.next_work(run, task)))  # deliver
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


def test_docs_only_tag_exempts_vacuous_tests_rejection(tmp_path, project) -> None:
    """#41: a deterministically-tagged docs-only change has no behavioral surface, so a
    reviewer's tests_meaningful=false must NOT reject it (engine-side, deterministic)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review_with_test(eng, test_output={
        # ENGINE-lane shape (#261): the deterministic runner makes no meaningfulness claim.
        "passed": True, "failures": [], "tests_meaningful": None, "change_class": "docs-only",
    })
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # docs-only exempts the missing-tests rejection
    assert eng.store.load_task("r1", "t1").context["change_class"] == "docs-only"


def test_docs_only_tag_does_not_exempt_an_explicit_rejection(tmp_path, project) -> None:
    """Docs-only relaxes only the tests criterion — a substantive approved=false still rejects."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review_with_test(eng, test_output={
        # ENGINE-lane shape (#261): the deterministic runner makes no meaningfulness claim.
        "passed": True, "failures": [], "tests_meaningful": None, "change_class": "docs-only",
    })
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False, "issues": [{"severity": "critical", "description": "wrong doc claim"}],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"  # docs-only never waves through a real reject


def test_model_claimed_docs_only_is_ignored_still_rejects(tmp_path, project) -> None:
    """The loophole guard: a MODEL-lane TEST claiming docs-only is not folded, so a
    reviewer's tests_meaningful=false STILL rejects (a model can't self-exempt)."""
    from orchestrator.schemas.enums import ExecutionMode, Provider
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review_with_test(
        eng,
        test_output={"passed": True, "failures": [], "tests_meaningful": True,
                     "change_class": "docs-only"},
        test_mode=ExecutionMode.INTERACTIVE, test_provider=Provider.CLAUDE,  # a MODEL claim
    )
    assert "change_class" not in eng.store.load_task("r1", "t1").context  # dropped at the fold
    out = eng.record("r1", make_result(w, structured_output={
        "approved": True, "issues": [], "tests_meaningful": False,
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"  # no exemption from a model's claim


def test_vacuous_tests_never_auto_approve_as_suggestions(tmp_path, project) -> None:
    """tests_meaningful=false must not slip through the suggestion-only severity gate."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False, "tests_meaningful": False,
        "issues": [{"severity": "suggestion", "description": "nit"}],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"  # not auto-approved


def _reject_then_fix(eng, issues):
    """Reject the first review with `issues`, then drive the fix cycle back to review."""
    w = _advance_to_review(eng)
    eng.record("r1", make_result(w, structured_output={"approved": False, "issues": issues}))
    for _ in range(3):  # implement, test, deliver again
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w2 = eng.next_work("r1", "t1")
    assert w2.stage is Stage.REVIEW
    return w2


def test_converged_re_review_auto_approves(tmp_path, project) -> None:
    """#15: a re-review whose issues are a subset of the previous rejection's (no
    net-new findings, none critical) has converged — auto-approve, don't park."""
    eng = _engine(tmp_path, project, max_review_cycles=1)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    issues = [
        {"severity": "important", "file": "a.py", "description": "naming could be tighter"},
        {"severity": "important", "file": "b.py", "description": "duplicated helper"},
    ]
    w2 = _reject_then_fix(eng, issues)
    # the re-review repeats ONE of the same issues (reworded whitespace) — a subset
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": False,
        "issues": [{"severity": "important", "file": "a.py",
                    "description": "naming  could be tighter"}],
    }))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # converged, not parked
    events = [e for e in eng.store.read_events("r1") if e["type"] == "review_verdict"]
    assert events[-1]["kind"] == "converged_auto_approved"


def test_net_new_issue_blocks_convergence(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_review_cycles=1)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w2 = _reject_then_fix(eng, [{"severity": "important", "file": "a.py",
                                 "description": "naming"}])
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": False,
        "issues": [{"severity": "important", "file": "c.py",
                    "description": "brand new problem the fix introduced"}],
    }))
    assert out["outcome"] == "review_rejected_held"  # net-new finding: cycles spent -> park


def test_critical_issue_never_converges(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_review_cycles=1)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    crit = [{"severity": "critical", "file": "a.py", "description": "data loss"}]
    w2 = _reject_then_fix(eng, crit)
    out = eng.record("r1", make_result(w2, structured_output={
        "approved": False, "issues": crit,  # identical, but critical
    }))
    assert out["outcome"] == "review_rejected_held"  # a critical repeat parks, never ships


def test_first_rejection_never_converges(tmp_path, project) -> None:
    """Subset-of-nothing must not auto-approve the FIRST rejection."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={
        "approved": False,
        "issues": [{"severity": "important", "file": "a.py", "description": "x"}],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"


def test_missing_approved_field_fails_open(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"issues": []}))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # fail-open: only explicit false triggers


def test_rejection_event_is_audited(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    eng.record("r1", make_result(w, structured_output=REJECTION))
    events = eng.store.read_events("r1")
    verdicts = [e for e in events if e["type"] == "review_verdict"]
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v["kind"] == "rejected" and v["disposition"] == "fix_cycle"
    assert v["review_cycles"] == 1
    assert any("breaks the invariant" in i for i in v["issues"])


def test_rejection_still_records_cost_and_stage_log(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, status=ResultStatus.SUCCESS, structured_output=REJECTION))
    assert out["cost_usd"] > 0  # the review model call is still priced


def test_format_review_issue_shapes() -> None:
    assert format_review_issue("plain text") == "plain text"
    rich = format_review_issue({
        "severity": "critical", "file": "a.py", "line": 12,
        "description": "breaks it", "suggested_fix": "guard None",
    })
    assert rich == "critical — a.py:12 — breaks it (suggested fix: guard None)"
    assert format_review_issue({"description": "just a description"}) == "just a description"


# --- #414: a non-blocking finding may ask to be fixed in place, and be believed ----------
#
# Before #414 a `non_blocking` finding dispositioned `fix_now` was read by NOTHING but the
# renderer, which reported it "fixed in place (boy-scout)". On run ff-v1-b29 two correct
# findings were reported fixed, shipped unfixed to main, and surfaced only in a hand-audit.
# So: `fixup` is the disposition that acts, `fix_now` says plainly that it does not, and
# the note reports application from the durable record rather than from the ask.

FINDING_FIXUP = {
    "title": "Comment names the wrong confidence tier",
    "detail": "The comment says medium three lines above an assertion checking low.",
    "disposition": "fixup",
}
FINDING_FIXUP_2 = {
    "title": "Recovery note documents one command where two are needed",
    "detail": "`ingest map` resolves an existing institution rather than creating one.",
    "disposition": "fixup",
}


def test_finding_fixup_reimplements_and_reports_application_from_evidence(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    out = eng.record("r1", make_result(
        _advance_to_review(eng),
        structured_output={
            "approved": True, "issues": [], "non_blocking": [FINDING_FIXUP],
        },
    ))

    # the nit earns a real re-implement pass — the thing `fix_now` never did
    assert out["outcome"] == "review_fixup_cycle"
    assert out["next_stage"] == "implement"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1
    assert len(task.review_fixups) == 1
    assert task.review_fixups[0].source == "finding"
    assert task.review_fixups[0].applied is False
    # ...and the implementer is told what to fix
    assert FINDING_FIXUP["title"] in task.learnings[-1]
    assert FINDING_FIXUP["detail"] in task.learnings[-1]
    assert "review finding fixup" in task.learnings[-1]
    scheduled = next(e for e in eng.store.read_events("r1")
                     if e["type"] == "review_fixup_scheduled")
    assert scheduled["source"] == "finding"

    final = _drive_fixup_tail(eng)
    out = eng.record("r1", make_result(
        final, structured_output={"approved": True, "issues": []}
    ))

    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    assert eng.store.load_task("r1", "t1").review_fixups[0].applied is True
    applied = [e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_applied"]
    assert len(applied) == 1 and applied[0]["source"] == "finding"
    # It never became a backlog issue, and the note does not mislabel a nit an improvement.
    assert not project.task_source.followups
    note = project.task_source.notes[0]["body"]
    assert "Improvement fixup" not in note
    # The approving review no longer restates the finding (its record was reset with the
    # REVIEW stage), so the note must report it from the durable record — otherwise the
    # application is invisible in the very note that is supposed to prove it happened.
    assert f"{FINDING_FIXUP['title']} — applied in place, not filed" in note


def test_finding_fixup_note_reports_application_not_the_ask(tmp_path, project) -> None:
    # The evidence half: the FINAL review still carries the finding (a reviewer may restate
    # it), so the note must read the durable record, not the disposition.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [], "non_blocking": [FINDING_FIXUP],
    }))
    finish_after_review(eng, eng.record("r1", make_result(
        _drive_fixup_tail(eng), structured_output={
            "approved": True, "issues": [],
            "non_blocking": [{**FINDING_FIXUP, "disposition": "drop"}],
        },
    )))

    note = project.task_source.notes[0]["body"]
    assert f"{FINDING_FIXUP['title']} — applied in place, not filed" in note
    assert "fixed in place (boy-scout)" not in note


def test_two_finding_fixups_share_one_cycle(tmp_path, project) -> None:
    # A cycle per nit would exhaust the default two-cycle budget and park the task.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [],
        "non_blocking": [FINDING_FIXUP, FINDING_FIXUP_2],
    }))

    assert out["outcome"] == "review_fixup_cycle"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1  # ONE cycle for both
    assert [f.title for f in task.review_fixups] == [
        FINDING_FIXUP["title"], FINDING_FIXUP_2["title"]
    ]
    scheduled = [e for e in eng.store.read_events("r1")
                 if e["type"] == "review_fixup_scheduled"]
    assert len(scheduled) == 2  # both are individually auditable

    out = eng.record("r1", make_result(
        _drive_fixup_tail(eng), structured_output={"approved": True, "issues": []}
    ))
    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    assert all(f.applied for f in eng.store.load_task("r1", "t1").review_fixups)


def test_improvement_and_finding_fixups_share_one_cycle(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [],
        "improvement": FIXUP, "non_blocking": [FINDING_FIXUP],
    }))

    assert out["outcome"] == "review_fixup_cycle"
    task = eng.store.load_task("r1", "t1")
    assert task.review_cycles == 1
    assert [f.source for f in task.review_fixups] == ["improvement", "finding"]

    finish_after_review(eng, eng.record("r1", make_result(
        _drive_fixup_tail(eng), structured_output={"approved": True, "issues": []}
    )))
    note = project.task_source.notes[0]["body"]
    # the improvement is reported in its own section, the finding in the findings section
    assert f"Improvement fixup:** {FIXUP['title']}" in note
    assert f"{FINDING_FIXUP['title']} — applied in place, not filed" in note


def test_unschedulable_finding_fixup_completes_instead_of_parking(tmp_path, project) -> None:
    # A nit the engine cannot schedule must not park an approved task at the human gate —
    # that would stall a headless batch over a stale comment. It degrades, loudly.
    eng = _engine(tmp_path, project, max_review_cycles=0)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [], "non_blocking": [FINDING_FIXUP],
    }))

    assert finish_after_review(eng, out)["outcome"] == "task_completed"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.COMPLETED
    assert task.last_error is None  # a completed task carries no error
    # No durable record: an unapplied one would be swept applied by the next review.
    assert task.review_fixups == []
    held = next(e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held")
    assert held["source"] == "finding" and held["level"] == "warning"
    assert "budget exhausted" in held["reason"]
    # ...and the note says it was asked for and NOT applied.
    note = project.task_source.notes[0]["body"]
    assert f"{FINDING_FIXUP['title']} — requested in-place fixup — not applied" in note


def test_held_finding_fixup_is_not_swept_applied_by_its_own_review(tmp_path, project) -> None:
    # The regression that makes the degrade path safe: a fixup scheduled, re-asked, and
    # held must NOT be marked applied by the same approving transaction.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    output = {"approved": True, "issues": [], "non_blocking": [FINDING_FIXUP]}
    eng.record("r1", make_result(_advance_to_review(eng), structured_output=output))

    out = eng.record("r1", make_result(_drive_fixup_tail(eng), structured_output=output))

    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # degrades, does not park
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.COMPLETED
    assert len(task.review_fixups) == 1
    assert task.review_fixups[0].applied is False  # never applied, never claimed applied
    held = [e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held"]
    assert len(held) == 1 and "requested again" in held[0]["reason"]
    assert not [e for e in eng.store.read_events("r1")
                if e["type"] == "review_fixup_applied"]
    note = project.task_source.notes[0]["body"]
    assert f"{FINDING_FIXUP['title']} — requested in-place fixup — not applied" in note


def test_improvement_fixup_still_parks_when_a_finding_rides_along(tmp_path, project) -> None:
    # The improvement side's human gate is unchanged by #414: when it must be held, the
    # whole batch holds with it rather than half-scheduling.
    eng = _engine(tmp_path, project, max_review_cycles=0)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [],
        "improvement": FIXUP, "non_blocking": [FINDING_FIXUP],
    }))

    assert out["outcome"] == "review_fixup_held"
    assert out["task_state"] == "blocked_on_human"
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.REVIEW].status is StageStatus.FAILED
    held = [e for e in eng.store.read_events("r1") if e["type"] == "review_fixup_held"]
    assert {e["source"] for e in held} == {"improvement", "finding"}


def test_fix_now_finding_is_noted_but_never_claimed_fixed(tmp_path, project) -> None:
    # `fix_now` keeps working (persisted reviews carry it) but claims nothing.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [],
        "non_blocking": [{**FINDING_FIXUP, "disposition": "fix_now"}],
    }))

    assert finish_after_review(eng, out)["outcome"] == "task_completed"  # no cycle: fix_now does not act
    assert eng.store.load_task("r1", "t1").review_fixups == []
    note = project.task_source.notes[0]["body"]
    assert f"{FINDING_FIXUP['title']} — noted for in-place handling, not applied" in note
    assert "fixed in place" not in note


def test_one_nit_stated_twice_earns_one_fixup(tmp_path, project) -> None:
    # A reviewer that puts the same nit in both fields must not get two records.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": True, "issues": [],
        "improvement": FINDING_FIXUP, "non_blocking": [FINDING_FIXUP],
    }))

    task = eng.store.load_task("r1", "t1")
    assert len(task.review_fixups) == 1
    assert task.review_fixups[0].source == "improvement"  # improvement wins the tie
