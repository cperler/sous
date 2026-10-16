"""#546: a fix cycle must not drop the superseded review's ``file`` requests.

A fix cycle (an approved review's fixup, or a blocking rejection) resets the REVIEW record
and re-runs implement→…→review, and finalize files only from the review that survives. A
``file`` finding or improvement the first pass raised was therefore lost unless the second
reviewer happened to repeat it, with no event and no note line. The engine now carries those
requests onto the task doc at the reset and files them at completion, deduped against the
final review. Each pass's stream/prompt files also get their own name instead of the second
pass overwriting ``review-attempt0.*``.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_review(eng: Engine):
    for _ in range(5):  # intake, scope, implement, test, deliver
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.REVIEW
    return work


def _drive_tail(eng: Engine):
    """Drive the re-opened IMPLEMENT→TEST→DELIVER tail and return its REVIEW WorkItem."""
    for _ in range(3):
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.REVIEW
    return work


FIXUP_NIT = {"title": "Rename the helper", "detail": "It reads as a getter.",
             "disposition": "fixup"}
FILE_FINDING = {"title": "Retries block the exec loop", "detail": "Handed-off RFQ retries.",
                "disposition": "file"}
FILE_IDEA = {"title": "Move retries onto a worker", "detail": "So EXEC never waits.",
             "disposition": "file"}
APPROVED = {"approved": True, "issues": []}


def _events(eng: Engine, kind: str) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == kind]


def _start(tmp_path, project, **kw) -> Engine:
    eng = _engine(tmp_path, project, **kw)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    return eng


def test_fixup_cycle_files_the_first_reviews_requests(tmp_path, project) -> None:
    eng = _start(tmp_path, project)
    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        **APPROVED, "non_blocking": [FIXUP_NIT, FILE_FINDING], "improvement": FILE_IDEA,
    }))
    assert out["outcome"] == "review_fixup_cycle"
    task = eng.store.load_task("r1", "t1")
    assert [(c.kind, c.title, c.review_cycle) for c in task.carried_review_filings] == [
        ("finding", FILE_FINDING["title"], 0), ("improvement", FILE_IDEA["title"], 0),
    ]
    carried = _events(eng, "review_filing_carried_forward")
    assert [e["kind"] for e in carried] == ["finding", "improvement"]

    # The second review says nothing about either — the #237 case.
    out = eng.record("r1", make_result(_drive_tail(eng), structured_output=APPROVED))
    assert out["outcome"] == "task_completed"

    filed = {f["title"]: f for f in project.task_source.followups}
    assert set(filed) == {FILE_FINDING["title"], FILE_IDEA["title"]}
    assert filed[FILE_FINDING["title"]]["labels"] is None
    assert filed[FILE_IDEA["title"]]["labels"] == ["enhancement"]
    assert "review pass 1" in filed[FILE_IDEA["title"]]["body"]
    assert FILE_IDEA["detail"] in filed[FILE_IDEA["title"]]["body"]
    assert [e["title"] for e in _events(eng, "improvement_filed")] == [FILE_IDEA["title"]]
    note = project.task_source.notes[0]["body"]
    assert f"Improvement idea (earlier review pass):** {FILE_IDEA['title']}" in note
    completed = _events(eng, "task_completed")[0]
    assert completed["followups_filed"] == 1
    assert completed["carried_improvements_filed"] == 1


def test_final_review_restatement_wins_and_is_evented(tmp_path, project) -> None:
    eng = _start(tmp_path, project)
    eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        **APPROVED, "non_blocking": [FIXUP_NIT, FILE_FINDING], "improvement": FILE_IDEA,
    }))
    # The final reviewer restates the finding as `file` (reworded casing) and drops the idea.
    eng.record("r1", make_result(_drive_tail(eng), structured_output={
        **APPROVED,
        "non_blocking": [{**FILE_FINDING, "title": FILE_FINDING["title"].upper()}],
        "improvement": {**FILE_IDEA, "disposition": "drop"},
    }))

    titles = [f["title"] for f in project.task_source.followups]
    assert titles == [FILE_FINDING["title"].upper()]  # filed once, from the final review
    deduped = _events(eng, "review_filing_carried_deduped")
    assert sorted(e["kind"] for e in deduped) == ["finding", "improvement"]


def test_rejection_fix_cycle_carries_its_file_findings(tmp_path, project) -> None:
    eng = _start(tmp_path, project)
    out = eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        "approved": False,
        "issues": [{"severity": "important", "file": "a.py", "description": "broken"}],
        "non_blocking": [FILE_FINDING],
    }))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert eng.store.load_task("r1", "t1").carried_review_filings[0].title == FILE_FINDING["title"]

    eng.record("r1", make_result(_drive_tail(eng), structured_output=APPROVED))
    assert [f["title"] for f in project.task_source.followups] == [FILE_FINDING["title"]]


def test_carried_findings_share_the_cap_and_overflow_is_noted(tmp_path, project) -> None:
    eng = _start(tmp_path, project, max_filed_followups=1)
    eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        **APPROVED, "non_blocking": [FIXUP_NIT, FILE_FINDING],
    }))
    final_finding = {"title": "Log the retry count", "detail": "", "disposition": "file"}
    eng.record("r1", make_result(_drive_tail(eng), structured_output={
        **APPROVED, "non_blocking": [final_finding],
    }))

    assert [f["title"] for f in project.task_source.followups] == [final_finding["title"]]
    note = project.task_source.notes[0]["body"]
    assert f"{FILE_FINDING['title']} — over per-task cap (raised by review pass 1)" in note
    assert _events(eng, "completion_note_published")[0]["unfiled_count"] == 2  # nit + overflow


def test_a_request_repeated_across_cycles_is_carried_and_filed_once(tmp_path, project) -> None:
    eng = _start(tmp_path, project, max_review_cycles=3)
    second_nit = {**FIXUP_NIT, "title": "Tighten the docstring"}
    eng.record("r1", make_result(_advance_to_review(eng), structured_output={
        **APPROVED, "non_blocking": [FIXUP_NIT, FILE_FINDING],
    }))
    eng.record("r1", make_result(_drive_tail(eng), structured_output={
        **APPROVED, "non_blocking": [second_nit, FILE_FINDING],
    }))
    assert len(eng.store.load_task("r1", "t1").carried_review_filings) == 1
    assert len(_events(eng, "review_filing_carried_forward")) == 1

    eng.record("r1", make_result(_drive_tail(eng), structured_output=APPROVED))
    assert [f["title"] for f in project.task_source.followups] == [FILE_FINDING["title"]]


def test_fix_cycle_redispatch_gets_its_own_log_file_names(tmp_path, project) -> None:
    eng = _start(tmp_path, project)
    first = _advance_to_review(eng)
    assert first.review_cycle == 0
    eng.record("r1", make_result(first, structured_output={
        **APPROVED, "non_blocking": [FIXUP_NIT],
    }))
    second = _drive_tail(eng)
    assert second.review_cycle == 1
    assert second.attempt == first.attempt == 0  # the reset restarts the attempt counter
    prompt_files = [e["prompt_file"] for e in _events(eng, "stage_dispatched")
                    if e.get("stage") == "review"]
    assert prompt_files == [
        "stages/t1/review-attempt0.prompt.txt", "stages/t1/review-attempt0.cycle1.prompt.txt",
    ]
    for rel in prompt_files:  # both passes' prompts survive on disk
        assert (eng.store.root / rel).exists()
