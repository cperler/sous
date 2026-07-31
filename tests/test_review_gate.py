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
from tests.conftest import make_result


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
    assert out["outcome"] == "task_completed"
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED


FIXUP = {
    "title": "Keep the completion evidence honest",
    "detail": "Render applied only after the re-review accepts the in-place edit.",
    "disposition": "fixup",
}


def _drive_fixup_tail(eng: Engine):
    """Drive the re-opened IMPLEMENT→TEST→DELIVER tail and return its REVIEW."""
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
    assert out["outcome"] == "task_completed"
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
        ([Stage.INTAKE, Stage.REVIEW], "no IMPLEMENT→DELIVER→REVIEW tail"),
        (
            [Stage.REVIEW, Stage.IMPLEMENT, Stage.DELIVER],
            "does not order IMPLEMENT→DELIVER→REVIEW",
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
    assert out["outcome"] == "task_completed"  # severity gate: suggestions never block
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
    assert out2["outcome"] == "task_completed"


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
    assert out["outcome"] == "task_completed"  # deterministic TEST → no model test surface


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
    assert out["outcome"] == "task_completed"


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
    assert out["outcome"] == "task_completed"  # docs-only exempts the missing-tests rejection
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
    assert out["outcome"] == "task_completed"  # converged, not parked
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
    assert out["outcome"] == "task_completed"  # fail-open: only explicit false triggers


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
