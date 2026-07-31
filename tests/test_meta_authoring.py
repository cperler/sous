"""Meta-authoring loop: REVIEW process evidence -> recurring task -> gated delivery."""

from __future__ import annotations

from orchestrator import learnings_kb as kb
from orchestrator import meta_authoring as meta
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage, TaskState
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

TARGET = {"kind": "stage-template", "ref": "REVIEW"}


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "cost.jsonl"), project)


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


def test_process_entries_dedupe_per_run_and_never_reach_recall(tmp_path) -> None:
    path = tmp_path / "kb.jsonl"
    row = {"kind": "process", "text": "Review prompt hides the baseline", "target": TARGET}
    assert len(kb.append_learnings(path, [{**row, "run_id": "r1"}])) == 1
    assert kb.append_learnings(path, [{**row, "run_id": "r1"}]) == []
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


def test_second_run_files_one_meta_task_and_ledger_prevents_refiling(tmp_path, project) -> None:
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

    project.task_source.file_followup = real_file
    eng._file_meta_proposals("r2")
    assert len(meta.read_filing_ledger(eng._meta_proposals_path())) == 1


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
