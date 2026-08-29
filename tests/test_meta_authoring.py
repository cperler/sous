"""Meta-authoring loop: REVIEW process evidence -> recurring task -> gated delivery."""

from __future__ import annotations

import json
import threading

import pytest

import orchestrator.engine as engine_module
from orchestrator import learnings_kb as kb
from orchestrator import meta_authoring as meta
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage, TaskState
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import FakeTaskSource, make_result

TARGET = {"kind": "stage-template", "ref": "REVIEW"}


def _engine(tmp_path, project, *, meta_task_source=None) -> Engine:
    # Most tests in this module model a self-hosted run, where both explicitly resolved
    # sources point at the same tracker. The external-routing case passes a distinct source.
    meta_task_source = meta_task_source or project.task_source
    return Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "cost.jsonl"),
        project,
        meta_task_source=meta_task_source,
    )


def _review_run(eng: Engine, run_id: str, task_id: str, *, detail: str) -> None:
    eng.create_run(run_id)
    eng.add_task(run_id, task_id, pipeline=[Stage.REVIEW])
    work = eng.next_work(run_id, task_id)
    assert work is not None
    eng.record(
        run_id,
        make_result(
            work,
            structured_output={
                "approved": True,
                "issues": [],
                "retrospective": {
                    "title": "Review prompt hides the baseline",
                    "detail": detail,
                    "target": TARGET,
                },
            },
        ),
    )


def test_engine_never_falls_back_to_product_task_source(tmp_path, project) -> None:
    class ProductOnlyConfig:
        task_source = project.task_source

    with pytest.raises(TypeError, match="requires meta_task_source"):
        Engine(
            StatusStore(tmp_path),
            CostLedger(tmp_path / "cost.jsonl"),
            ProductOnlyConfig(),  # type: ignore[arg-type]
        )


def test_process_entries_dedupe_per_run_and_never_reach_recall(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    row = {"kind": "process", "text": "Review prompt hides the baseline", "target": TARGET}
    assert len(kb.append_learnings(path, [{**row, "run_id": "r1"}])) == 1
    assert kb.append_learnings(path, [{**row, "run_id": "r1"}]) == []
    other_target = {"kind": "skill", "ref": "review-evidence"}
    assert len(kb.append_learnings(
        path, [{**row, "run_id": "r1", "target": other_target}]
    )) == 1
    assert kb.append_learnings(
        path,
        [{**row, "run_id": "r1", "target": {"kind": " SKILL ", "ref": " REVIEW-EVIDENCE "}}],
    ) == []
    assert len(kb.append_learnings(path, [{**row, "run_id": "r2"}])) == 1
    assert kb.relevant_learnings(path, {"title_tokens": ["review", "baseline"]}) == []


def test_harvest_process_retrospective_keeps_valid_target(tmp_path) -> None:
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    task.stages[Stage.REVIEW].output = {
        "retrospective": {
            "title": "Review prompt hides the baseline",
            "detail": "Ask for the baseline explicitly",
            "target": TARGET,
        }
    }
    written = kb.harvest_process_retrospective(tmp_path / "kb.jsonl", task, "r1", now="now")
    assert written[0]["kind"] == "process"
    assert written[0]["target"] == TARGET
    assert written[0]["text"] == (
        "Review prompt hides the baseline: Ask for the baseline explicitly"
    )
    assert kb.harvest_process_retrospective(tmp_path / "kb.jsonl", task, "r1") == []


def test_harvest_ignores_malformed_retrospective_and_drops_only_bad_target(tmp_path) -> None:
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    task.stages[Stage.REVIEW].output = {"retrospective": "not an object"}
    assert kb.harvest_process_retrospective(tmp_path / "kb.jsonl", task, "r1") == []

    task.stages[Stage.REVIEW].output = {
        "retrospective": {
            "title": "Useful process lesson",
            "target": {"kind": "engine-logic", "ref": "record"},
        }
    }
    written = kb.harvest_process_retrospective(tmp_path / "kb.jsonl", task, "r1")
    assert len(written) == 1
    assert "target" not in written[0]


def test_recurring_proposals_require_distinct_runs_and_are_deterministic() -> None:
    same_run = [
        {"kind": "process", "run_id": "r1", "task_id": "a", "text": "one", "target": TARGET},
        {"kind": "process", "run_id": "r1", "task_id": "b", "text": "two", "target": TARGET},
    ]
    assert meta.recurring_proposals(same_run) == []
    entries = [*same_run, {"kind": "process", "run_id": "r2", "text": "three", "target": TARGET}]
    first = meta.recurring_proposals(entries)
    assert first == meta.recurring_proposals(list(reversed(entries)))
    assert first[0]["key"] == "stage-template:review"
    assert {row["run_id"] for row in first[0]["evidence"]} == {"r1", "r2"}


def test_targetless_process_entries_cluster_by_normalized_text() -> None:
    proposals = meta.recurring_proposals([
        {"kind": "process", "run_id": "r1", "text": "Prompt repeats context"},
        {"kind": "process", "run_id": "r2", "text": "  PROMPT repeats   context "},
    ])
    assert len(proposals) == 1
    assert proposals[0]["key"].startswith("text:")
    assert proposals[0]["target"] is None


def test_second_run_files_one_meta_task_and_a_repeat_leaves_a_skip_receipt(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="First observation")
    assert project.task_source.followups == []

    _review_run(eng, "r2", "t2", detail="Independent observation")
    filed = project.task_source.followups
    assert len(filed) == 1
    assert filed[0]["labels"] == ["meta-authoring", "enhancement"]
    assert "run `r1`" in filed[0]["body"] and "run `r2`" in filed[0]["body"]
    ledger = meta.read_filing_ledger(eng._meta_proposals_path())
    assert len(ledger) == 1 and ledger[0]["ref"] == filed[0]["ref"]

    eng._file_meta_proposals("r2")
    assert len(project.task_source.followups) == 1
    assert project.task_source.comments == []
    skipped = [e for e in eng.store.read_events("r2") if e["type"] == "meta_proposal_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["ref"] == filed[0]["ref"]
    assert skipped[0]["evidence"] == 2 and skipped[0]["evidence_reported"] == 2
    assert eng.status("r2")["meta_proposals"]["skipped"] == 1


def test_new_evidence_after_filing_is_commented_onto_the_open_issue(tmp_path, project) -> None:
    """#406: a filed cluster keeps reporting instead of going quiet forever."""
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="First observation")
    _review_run(eng, "r2", "t2", detail="Independent observation")
    ref = project.task_source.followups[0]["ref"]

    _review_run(eng, "r3", "t3", detail="A newer lesson about the same template")

    assert len(project.task_source.followups) == 1  # no duplicate issue
    assert len(project.task_source.comments) == 1
    comment = project.task_source.comments[0]
    assert comment["ref"] == ref
    assert "A newer lesson about the same template" in comment["body"]
    # Only the NEW rows travel; the issue body already carries the earlier two.
    assert "First observation" not in comment["body"]
    updated = [e for e in eng.store.read_events("r3") if e["type"] == "meta_proposal_updated"]
    assert len(updated) == 1
    assert updated[0]["ref"] == ref and updated[0]["new_evidence"] == 1
    assert updated[0]["evidence"] == 3
    assert eng.status("r3")["meta_proposals"]["updated"] == 1

    # The watermark advanced, so a repeat pass reports nothing rather than re-commenting.
    eng._file_meta_proposals("r3")
    assert len(project.task_source.comments) == 1
    assert [e["type"] for e in eng.store.read_events("r3")].count("meta_proposal_skipped") == 1
    ledger = meta.read_filing_ledger(eng._meta_proposals_path())
    assert [row["action"] for row in ledger] == ["filed", "updated"]
    assert ledger[-1]["evidence_count"] == 3


def test_recurrence_after_the_tracked_issue_closed_files_a_new_one(tmp_path, project) -> None:
    """A closed issue means the lesson was addressed; a recurrence is genuinely new work."""
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="First observation")
    _review_run(eng, "r2", "t2", detail="Independent observation")
    first_ref = project.task_source.followups[0]["ref"]
    project.task_source.describe_issue = lambda ref: {
        "ref": ref, "state": "CLOSED", "body": "", "pr": None
    }

    _review_run(eng, "r3", "t3", detail="It came back after the fix shipped")

    assert len(project.task_source.followups) == 2
    assert project.task_source.comments == []
    assert first_ref in project.task_source.followups[1]["body"]
    refiled = [e for e in eng.store.read_events("r3") if e["type"] == "meta_proposal_refiled"]
    assert len(refiled) == 1
    assert refiled[0]["prior_ref"] == first_ref
    assert refiled[0]["ref"] == project.task_source.followups[1]["ref"]
    ledger = meta.read_filing_ledger(eng._meta_proposals_path())
    assert ledger[-1]["ref"] == project.task_source.followups[1]["ref"]
    assert ledger[-1]["prior_ref"] == first_ref


def test_cluster_held_under_the_recurrence_floor_leaves_a_receipt(tmp_path, project) -> None:
    """The min_runs floor is a suppression too — it must not be invisible (#406)."""
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="Seen exactly once so far")

    assert project.task_source.followups == []
    withheld = [e for e in eng.store.read_events("r1") if e["type"] == "meta_proposal_withheld"]
    assert len(withheld) == 1
    assert withheld[0]["key"] == "stage-template:review"
    assert withheld[0]["runs"] == 1 and withheld[0]["evidence"] == 1
    assert eng.status("r1")["meta_proposals"]["withheld"] == 1


def test_legacy_ledger_row_reports_its_whole_backlog_once(tmp_path, project) -> None:
    """A pre-#406 row carries no watermark, so the accumulated backlog reports once."""
    eng = _engine(tmp_path, project)
    kb.append_learnings(eng._learnings_kb_path(), [
        {"kind": "process", "run_id": f"r{n}", "text": f"lesson {n}", "target": TARGET,
         "ts": f"2026-08-1{n}T00:00:00+00:00"}
        for n in (1, 2, 3)
    ])
    eng._meta_proposals_path().write_text(
        json.dumps({"key": "stage-template:review", "ref": "#390",
                    "filed_at": "2026-08-13T00:00:00+00:00", "run_id": "b15"}) + "\n",
        encoding="utf-8",
    )

    eng.create_run("r4")
    eng._file_meta_proposals("r4")

    assert project.task_source.followups == []  # the tracked issue is still the home
    assert len(project.task_source.comments) == 1
    body = project.task_source.comments[0]["body"]
    assert project.task_source.comments[0]["ref"] == "#390"
    assert all(f"lesson {n}" in body for n in (1, 2, 3))

    eng._file_meta_proposals("r4")  # backlog now watermarked — no second comment
    assert len(project.task_source.comments) == 1


def test_missing_comment_hook_is_evented_and_stays_retryable(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="First observation")
    _review_run(eng, "r2", "t2", detail="Independent observation")
    project.task_source.comment_on_ref = None  # an adapter without the optional hook

    _review_run(eng, "r3", "t3", detail="A newer lesson the seam cannot deliver")

    failures = [e for e in eng.store.read_events("r3") if e["type"] == "meta_proposal_failed"]
    assert len(failures) == 1
    assert failures[0]["source"] == "model"
    assert "comment_on_ref" in failures[0]["error"]
    assert "1 new evidence row(s)" in failures[0]["error"]
    assert eng.status("r3")["meta_proposals"]["clean"] is False
    # The MODEL cluster's watermark held. (The engine-authored input files its own row for
    # the `meta_proposal_failed` signal — #400 — so the ledger is scoped by key here.)
    model_rows = [
        row for row in meta.read_filing_ledger(eng._meta_proposals_path())
        if not str(row["key"]).startswith("signal:")
    ]
    assert len(model_rows) == 1

    del project.task_source.comment_on_ref  # hook restored -> the run retries cleanly
    eng._file_meta_proposals("r3")
    assert len(project.task_source.comments) == 1
    assert eng.status("r3")["meta_proposals"]["updated"] == 1


def test_withheld_clusters_mirror_recurring_proposals() -> None:
    entries = [
        {"kind": "process", "run_id": "r1", "text": "one", "target": TARGET},
        {"kind": "process", "run_id": "r2", "text": "two", "target": TARGET},
        {"kind": "process", "run_id": "r1", "text": "only seen once"},
    ]
    held = meta.withheld_clusters(entries)
    assert [cluster["key"] for cluster in meta.recurring_proposals(entries)] == [
        "stage-template:review"
    ]
    assert len(held) == 1 and held[0]["runs"] == 1
    assert held[0]["key"].startswith("text:")
    assert held[0] == meta.withheld_clusters(list(reversed(entries)))[0]


def test_append_filing_refuses_stale_rows_and_accepts_advancing_evidence(tmp_path) -> None:
    ledger = tmp_path / "meta-proposals.jsonl"
    proposal = meta.recurring_proposals([
        {"kind": "process", "run_id": "r1", "text": "one", "target": TARGET, "ts": "2026-01-01"},
        {"kind": "process", "run_id": "r2", "text": "two", "target": TARGET, "ts": "2026-01-02"},
    ])[0]
    first = {"key": proposal["key"], "ref": "#1", "filed_at": "t0", "run_id": "r2",
             **meta.evidence_watermark(proposal)}
    assert meta.append_filing(ledger, first) is True
    assert meta.append_filing(ledger, dict(first, ref="#2")) is False  # same evidence
    assert meta.latest_filing(ledger, proposal["key"])["ref"] == "#1"
    assert meta.new_evidence(proposal, meta.latest_filing(ledger, proposal["key"])) == []

    grown = meta.recurring_proposals([
        {"kind": "process", "run_id": "r1", "text": "one", "target": TARGET, "ts": "2026-01-01"},
        {"kind": "process", "run_id": "r2", "text": "two", "target": TARGET, "ts": "2026-01-02"},
        {"kind": "process", "run_id": "r3", "text": "three", "target": TARGET, "ts": "2026-01-03"},
    ])[0]
    fresh = meta.new_evidence(grown, meta.latest_filing(ledger, grown["key"]))
    assert [row["text"] for row in fresh] == ["three"]
    assert meta.append_filing(
        ledger, {"key": grown["key"], "ref": "#1", "filed_at": "t1", "run_id": "r3",
                 "action": "updated", **meta.evidence_watermark(grown)}
    ) is True
    assert meta.latest_filing(ledger, grown["key"])["evidence_count"] == 3


def test_external_run_files_meta_task_only_in_engine_tracker(tmp_path, project) -> None:
    engine_tracker = FakeTaskSource()
    eng = _engine(tmp_path, project, meta_task_source=engine_tracker)

    _review_run(eng, "r1", "t1", detail="First observation")
    _review_run(eng, "r2", "t2", detail="Independent observation")

    assert project.task_source.followups == []
    assert len(engine_tracker.followups) == 1
    assert engine_tracker.followups[0]["labels"] == ["meta-authoring", "enhancement"]

    # The shared ledger still owns dedupe even though the filing target is now distinct
    # from the run's product task source.
    eng._file_meta_proposals("r2")
    assert len(engine_tracker.followups) == 1
    assert len(meta.read_filing_ledger(eng._meta_proposals_path())) == 1


def test_concurrent_finalizers_file_one_meta_task(tmp_path, project, monkeypatch) -> None:
    path = tmp_path / "learnings-kb.jsonl"
    kb.append_learnings(path, [
        {"kind": "process", "run_id": "r1", "text": "first", "target": TARGET},
        {"kind": "process", "run_id": "r2", "text": "second", "target": TARGET},
    ])
    engines = [_engine(tmp_path, project), _engine(tmp_path, project)]

    # Force both finalizers to finish detection before either can enter the per-cluster
    # guard. Without the locked ledger recheck, both would create an external issue.
    barrier = threading.Barrier(2)
    original_detector = engine_module.recurring_proposals

    def synchronized_detector(entries):
        proposals = original_detector(entries)
        barrier.wait(timeout=10)
        return proposals

    monkeypatch.setattr(engine_module, "recurring_proposals", synchronized_detector)
    threads = [
        threading.Thread(target=engine._file_meta_proposals, args=(f"r{index}",))
        for index, engine in enumerate(engines, start=1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert len(project.task_source.followups) == 1
    assert len(meta.read_filing_ledger(engines[0]._meta_proposals_path())) == 1


def test_filing_failure_is_evented_without_ledger_and_can_retry(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _review_run(eng, "r1", "t1", detail="First observation")
    real_file = project.task_source.file_followup

    def fail_file(*_args, **_kwargs):
        raise RuntimeError("tracker unavailable")

    project.task_source.file_followup = fail_file
    _review_run(eng, "r2", "t2", detail="Independent observation")
    assert eng.store.load_run("r2").state.value == "completed"
    assert meta.read_filing_ledger(eng._meta_proposals_path()) == []
    assert any(e["type"] == "meta_proposal_failed" for e in eng.store.read_events("r2"))
    audit = eng.status("r2")["meta_proposals"]
    assert audit["clean"] is False
    # Both inputs are blocked by the same dead tracker: the model cluster, and the
    # engine-authored `meta_proposal_failed` signal that noticed it fail (#400).
    model_failures = [f for f in audit["failures"] if f["source"] == "model"]
    assert len(model_failures) == 1
    assert model_failures[0]["error"] == "tracker unavailable"

    project.task_source.file_followup = real_file
    eng._file_meta_proposals("r2")
    model_rows = [
        row for row in meta.read_filing_ledger(eng._meta_proposals_path())
        if not str(row["key"]).startswith("signal:")
    ]
    assert len(model_rows) == 1
    retried = eng.status("r2")["meta_proposals"]
    assert [f for f in retried["failures"] if f["source"] == "model"] == []
    assert retried["by_source"]["model"] == 1


def test_meta_authoring_label_holds_before_delivery_until_exact_gate_approved(
    tmp_path, project
) -> None:
    project.task_source.spec_overrides["t1"] = {"labels": ["meta-authoring"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", pipeline=[Stage.REVIEW, Stage.DELIVER])
    assert task.hold_before is Stage.DELIVER

    review = eng.next_work("r1", "t1")
    assert review is not None and review.stage is Stage.REVIEW
    eng.record("r1", make_result(review))
    assert eng.next_work("r1", "t1") is None
    held = eng.store.load_task("r1", "t1")
    assert held.state is TaskState.BLOCKED_ON_HUMAN
    assert held.pending_approval_what == "before:deliver"
    assert eng.dispatchable("r1") == []

    eng.approve("r1", "t1", approved_by="maintainer")
    deliver = eng.next_work("r1", "t1")
    assert deliver is not None and deliver.stage is Stage.DELIVER
    artifact = eng.store.load_approval("r1", "t1")
    assert artifact is not None and artifact["what"] == "before:deliver"


def test_earlier_approval_does_not_preapprove_delivery_gate(tmp_path, project) -> None:
    project.task_source.spec_overrides["t1"] = {"labels": ["meta-authoring"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW, Stage.DELIVER])
    eng.hold_for_approval("r1", "t1", what="approve scope")
    eng.approve("r1", "t1", approved_by="maintainer")

    review = eng.next_work("r1", "t1")
    assert review is not None
    eng.record("r1", make_result(review))
    assert eng.next_work("r1", "t1") is None
    held = eng.store.load_task("r1", "t1")
    assert held.pending_approval_what == "before:deliver"
    assert held.approved_holds == ["approve scope"]


def test_delivery_gate_reholds_if_approval_artifact_write_crashes(tmp_path, project) -> None:
    project.task_source.spec_overrides["t1"] = {"labels": ["meta-authoring"]}
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.DELIVER])
    assert eng.next_work("r1", "t1") is None

    real_write = eng.store.write_approval

    def fail_write(*_args, **_kwargs):
        raise RuntimeError("disk full")

    eng.store.write_approval = fail_write
    try:
        try:
            eng.approve("r1", "t1", approved_by="maintainer")
        except RuntimeError as exc:
            assert str(exc) == "disk full"
        else:  # pragma: no cover - assertion spelling keeps the injected failure explicit
            raise AssertionError("approval write should fail")
    finally:
        eng.store.write_approval = real_write

    # The task-doc transition landed first, but without the gate artifact DELIVER still
    # cannot dispatch; next_work restores the human hold.
    assert eng.next_work("r1", "t1") is None
    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN
