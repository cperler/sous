"""Evidence-out at task finalize (the 'publish the reasoning' half of the seam).

When a task completes the engine files each non-blocking review finding as a
deferred-scope follow-up and posts a completion note — both via OPTIONAL duck-typed
task-source hooks, so an adapter without them is a graceful no-op and a flaky hook can
never un-complete a finished task.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from adapters.project.base import TaskSpec
from adapters.project.github_issues import GitHubIssuesSource
from adapters.project.selfhost.task_source import LocalFileTaskSource
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_completion_note
from orchestrator.schemas.enums import ExecutionLane, Stage, StageStatus
from orchestrator.schemas.stage_schemas import resolve_stage_schema
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _drive(eng: Engine, *, review_output: dict, run="r1", task="t1") -> list:
    """Supervisor loop to completion, injecting a custom review structured_output."""
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        so = review_output if work.stage is Stage.REVIEW else None
        outcomes.append(eng.record(run, make_result(work, structured_output=so)))
    return outcomes


_REVIEW_WITH_FINDINGS = {
    "approved": True,
    "issues": [],
    "non_blocking": [
        {"title": "Handle a nonexistent --repo path", "detail": "raises FileNotFoundError"},
        {"title": "Guard negative --keep-latest", "detail": "a negative N is nonsensical"},
    ],
}


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- engine finalize ---------------------------------------------------------------


def test_finalize_files_followups_and_publishes_note(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)
    assert outcomes[-1]["outcome"] == "task_completed"

    ts = project.task_source
    # one follow-up per non-blocking finding, all labeled deferred-scope
    assert [f["title"] for f in ts.followups] == [
        "Handle a nonexistent --repo path",
        "Guard negative --keep-latest",
    ]
    assert all(f["labels"] == ["deferred-scope"] for f in ts.followups)
    assert "#10" not in ts.followups[0]["body"]  # (uses the real task id t1, not a literal)
    assert "t1" in ts.followups[0]["body"]  # provenance back-reference

    # exactly one completion note, on the PR, reflecting the verdict + follow-ups
    assert len(ts.notes) == 1
    note = ts.notes[0]
    assert note["pr_url"].endswith("/1234")
    assert "✅ approved" in note["body"]
    assert "Follow-ups filed" in note["body"]
    assert note["body"].count("https://example.test/issues/") == 2

    events = _events(tmp_path)
    assert sum(e["type"] == "followup_filed" for e in events) == 2
    completed = next(e for e in events if e["type"] == "task_completed")
    assert completed["followups_filed"] == 2


def test_finalize_no_findings_files_nothing_but_still_notes(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    _drive(eng, review_output={"approved": True, "issues": []})

    ts = project.task_source
    assert ts.followups == []
    assert len(ts.notes) == 1  # a clean run still publishes its completion evidence


class _BareSource:
    """A v1-era task source: resolve + mark_complete only, no evidence-out hooks."""

    def __init__(self) -> None:
        self.completed: list = []

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(task_id=task_id, title="x", body="y", issue_number=1)

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        self.completed.append((task_id, pr_url))


def test_finalize_graceful_when_adapter_lacks_hooks(tmp_path, project) -> None:
    project._task_source = _BareSource()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)

    # No hooks -> silent no-op, task still completes cleanly.
    assert outcomes[-1]["outcome"] == "task_completed"
    assert project.task_source.completed == [("t1", "https://github.com/x/y/pull/1234")]


class _FlakySource:
    """file_followup raises; publish_note works. Finalize must survive."""

    def __init__(self) -> None:
        self.notes: list = []

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(task_id=task_id, title="x", body="y", issue_number=1)

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        pass

    def file_followup(self, title: str, body: str, labels=None) -> str:
        raise RuntimeError("gh exploded")

    def publish_note(self, task_id: str, body: str, *, pr_url=None) -> None:
        self.notes.append(body)


def test_finalize_survives_a_flaky_followup_hook(tmp_path, project) -> None:
    project._task_source = _FlakySource()
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng, review_output=_REVIEW_WITH_FINDINGS)

    assert outcomes[-1]["outcome"] == "task_completed"  # never crashes finalize
    events = _events(tmp_path)
    assert sum(e["type"] == "followup_failed" for e in events) == 2
    # the note still publishes, marking the failed filings
    assert project.task_source.notes and "(filing failed)" in project.task_source.notes[0]


# --- render_completion_note --------------------------------------------------------


def test_render_completion_note_content() -> None:
    task = Task(
        task_id="#10", run_id="r1", created_at="t0", updated_at="t0",
        title="Checkpoint-tag GC", pr_url="https://github.com/o/r/pull/23",
    )
    task.stages[Stage.IMPLEMENT].model = "claude-opus-4-8"
    task.stages[Stage.IMPLEMENT].status = StageStatus.COMPLETED
    task.stages[Stage.REVIEW].output = {
        "approved": True, "issues": ["a lingering blocker"],
        "non_blocking": [{"title": "polish", "detail": "d"}],
    }

    note = render_completion_note(
        task, [{"title": "polish", "ref": "url-1"}, {"title": "other", "ref": None}]
    )

    assert "Checkpoint-tag GC" in note
    assert "https://github.com/o/r/pull/23" in note
    assert "✅ approved" in note
    assert "`claude-opus-4-8`" in note  # stage table renders per-stage model
    assert "a lingering blocker" in note  # blocking issues surfaced
    assert "polish → url-1" in note
    assert "other → (filing failed)" in note


def test_render_completion_note_denied_verdict() -> None:
    task = Task(task_id="#10", run_id="r1", created_at="t0", updated_at="t0")
    task.stages[Stage.REVIEW].output = {"approved": False, "issues": ["broken"]}
    note = render_completion_note(task, [])
    assert "❌ changes requested" in note


# --- adapter hooks -----------------------------------------------------------------


def test_github_source_publish_note_and_file_followup() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "https://github.com/o/r/issues/99\n"

    src = GitHubIssuesSource("o/r", runner=runner)

    src.publish_note("#10", "hello", pr_url="https://github.com/o/r/pull/23")
    assert calls[-1] == ["gh", "pr", "comment", "https://github.com/o/r/pull/23",
                         "--body", "hello"]

    src.publish_note("#10", "hi")  # no PR -> comment on the issue
    assert calls[-1] == ["gh", "issue", "comment", "10", "--repo", "o/r", "--body", "hi"]

    ref = src.file_followup("Title", "Body", labels=["deferred-scope", "dx"])
    assert calls[-1] == ["gh", "issue", "create", "--repo", "o/r", "--title", "Title",
                         "--body", "Body", "--label", "deferred-scope", "--label", "dx"]
    assert ref == "https://github.com/o/r/issues/99"


def test_localfile_source_publish_note_and_file_followup(tmp_path) -> None:
    src = LocalFileTaskSource(tmp_path / "tasks.json")

    src.publish_note("id-1", "the note body", pr_url=None)
    assert "the note body" in (tmp_path / "notes.log").read_text()

    ref = src.file_followup("A title", "A body", labels=["deferred-scope"])
    assert ref == "local:A title"
    log = (tmp_path / "followups.log").read_text()
    assert "deferred-scope" in log and "A title" in log


# --- review schema -----------------------------------------------------------------


def test_review_schema_accepts_non_blocking() -> None:
    schema = resolve_stage_schema("review")
    assert "non_blocking" in schema["properties"]
    validator = jsonschema.Draft202012Validator(schema)

    validator.validate({
        "approved": True, "issues": [],
        "non_blocking": [{"title": "x", "detail": "y"}, {"title": "z"}],
    })
    # a finding missing the required title is a schema violation
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({
            "approved": True, "issues": [], "non_blocking": [{"detail": "no title"}],
        })
