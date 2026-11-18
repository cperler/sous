"""REVIEW starts a fresh provider session (#542).

Provider sessions chain across successful stages via ``--resume``, which made REVIEW a
continuation of the implementer's conversation: the reviewer carried the author's
reasoning, so the independence the review prompt claims did not hold. The engine now never
hands REVIEW a session another stage minted. The single exception is a warm retry of
REVIEW's own failed attempt, which resumes REVIEW's session, not the implementer's.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage
from orchestrator.schemas.work import WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

REJECTION = {
    "approved": False,
    "issues": [{"severity": "critical", "file": "a.py", "line": 1,
                "description": "breaks the invariant"}],
}


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def _dispatches(eng: Engine, stage: Stage) -> list[dict]:
    return [e for e in eng.store.read_events("r1")
            if e["type"] == "stage_dispatched" and e.get("stage") == stage.value]


def _advance_to_review(eng: Engine, *, warm: bool = False) -> WorkItem:
    """Drive a FULL task to REVIEW over one chained session, as a headless claude run does:
    SCOPE mints it and every later model stage resumes it and reports the same id back."""
    eng.create_run("r1", ExecutionLane.FULL, warm_retry=warm)
    eng.add_task("r1", "t1")
    for _ in range(5):  # intake, scope, implement, test, deliver
        work = eng.next_work("r1", "t1")
        ref = "sess-impl" if work.stage in (Stage.SCOPE, Stage.IMPLEMENT, Stage.DELIVER) else None
        eng.record("r1", make_result(work, session_ref=ref))
    review = eng.next_work("r1", "t1")
    assert review.stage is Stage.REVIEW
    return review


def test_review_never_resumes_the_implementers_session(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    review = _advance_to_review(eng)
    task = eng.store.load_task("r1", "t1")
    assert task.session_ref == "sess-impl"  # the task still carries IMPLEMENT's session…
    assert review.session_ref is None  # …but REVIEW is dispatched without it
    impl_ref = _dispatches(eng, Stage.IMPLEMENT)[-1]["session_ref"]
    review_ref = _dispatches(eng, Stage.REVIEW)[-1]["session_ref"]
    assert impl_ref == "sess-impl"  # IMPLEMENT ran on the chained session
    assert review_ref != impl_ref and review_ref is None


def test_stages_before_review_still_chain(tmp_path, project) -> None:
    """Only REVIEW is cut off: DELIVER still resumes the session IMPLEMENT created."""
    eng = _engine(tmp_path, project)
    _advance_to_review(eng)
    deliver = _dispatches(eng, Stage.DELIVER)
    assert deliver and deliver[-1]["session_ref"] == "sess-impl"


def test_warm_retry_of_review_resumes_reviews_own_session(tmp_path, project) -> None:
    """A mechanical REVIEW failure on a warm-retry run keeps REVIEW's OWN session."""
    eng = _engine(tmp_path, project)
    review = _advance_to_review(eng, warm=True)
    eng.record("r1", make_result(review, status=ResultStatus.TIMEOUT, error="timed out",
                                 session_ref="sess-review"))
    task = eng.store.load_task("r1", "t1")
    assert task.session_ref == "sess-review" and task.session_stage is Stage.REVIEW
    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.REVIEW and retry.session_ref == "sess-review"


def test_warm_retry_without_reviews_own_ref_stays_cold(tmp_path, project) -> None:
    """A REVIEW timeout that reported no session must not 'keep' IMPLEMENT's session warm:
    next_work would drop it, so a warm_retry_used event would claim warmth that never happens."""
    eng = _engine(tmp_path, project)
    review = _advance_to_review(eng, warm=True)
    eng.record("r1", make_result(review, status=ResultStatus.TIMEOUT, error="timed out"))
    assert not [e for e in eng.store.read_events("r1") if e["type"] == "warm_retry_used"]
    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.REVIEW and retry.session_ref is None


def test_rereview_after_a_fix_cycle_is_fresh(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    review = _advance_to_review(eng)
    out = eng.record("r1", make_result(review, structured_output=REJECTION,
                                       session_ref="sess-review"))
    assert out["outcome"] == "review_rejected_fix_cycle"
    for _ in range(3):  # implement, test, deliver again; IMPLEMENT mints a new session
        work = eng.next_work("r1", "t1")
        ref = "sess-impl-2" if work.stage is Stage.IMPLEMENT else None
        eng.record("r1", make_result(work, session_ref=ref))
    rereview = eng.next_work("r1", "t1")
    assert rereview.stage is Stage.REVIEW
    assert rereview.session_ref is None


def test_a_legacy_untagged_session_is_not_handed_to_review(tmp_path, project) -> None:
    """A pre-#542 task doc has a session_ref but no session_stage: REVIEW treats it as not its own."""
    eng = _engine(tmp_path, project)
    _advance_to_review(eng)
    task = eng.store.load_task("r1", "t1")
    assert task.session_ref == "sess-impl"
    # Re-dispatching the same REVIEW (e.g. after a crash reclaim) still goes out fresh.
    task.session_stage = None
    task.pending_work_item_id = None
    eng.store.save_task(task)
    again = eng.next_work("r1", "t1")
    assert again.stage is Stage.REVIEW and again.session_ref is None
