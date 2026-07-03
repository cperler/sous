"""The review gate: a completed REVIEW that reports approved=false must never
fall through to task_completed. While fix cycles remain, the engine re-opens
implement→…→review with the blocking issues as learnings (the old system's
quality/fix loop, restored bounded); at max_review_cycles it parks the task
BLOCKED_ON_HUMAN with REVIEW re-opened so approve() leads to a re-review.
Suggestion-only rejections auto-approve (the old severity gate)."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
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
