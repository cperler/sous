"""Mid-run progress commentary (#64).

The engine upserts a living progress comment/PR-body section on the driving issue/PR at
each stage boundary when a run opts in (``progress_comments``) — throttled and best-effort,
so a missing/raising hook never breaks record(). GitHubIssuesSource implements the upsert
(find-by-marker → edit, else create); routing goes to the PR body once a pr_url is known.
"""

from __future__ import annotations

import json

from adapters.project.github_issues import GitHubIssuesSource
from adapters.project.selfhost.task_source import LocalFileTaskSource
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_progress
from orchestrator.schemas.enums import ExecutionLane, Stage, StageStatus
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _drive(eng: Engine, *, run="r1", task="t1") -> list:
    outcomes = []
    while (work := eng.next_work(run, task)) is not None:
        outcomes.append(eng.record(run, make_result(work)))
    return outcomes


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- engine emit points ------------------------------------------------------------


def test_emits_at_every_stage_boundary_when_opted_in(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, progress_throttle_s=0)
    eng.create_run("r1", ExecutionLane.FULL, progress_comments=True)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng)
    assert outcomes[-1]["outcome"] == "task_completed"

    calls = project.task_source.progress
    # one publish per stage in the full pipeline (throttle disabled)
    assert len(calls) == len(outcomes)
    assert all(c["marker"] == "orchestrator:progress:t1" for c in calls)
    # body carries the stage table + cost line
    assert "| Stage |" in calls[0]["body"]
    assert "Cost to date" in calls[0]["body"]
    assert "Run progress" in calls[0]["body"]

    events = _events(tmp_path)
    assert sum(e["type"] == "progress_published" for e in events) == len(calls)


def test_default_off_emits_nothing(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, progress_throttle_s=0)
    eng.create_run("r1", ExecutionLane.FULL)  # progress_comments defaults OFF
    eng.add_task("r1", "t1")

    _drive(eng)

    assert project.task_source.progress == []
    assert not any(e["type"] == "progress_published" for e in _events(tmp_path))


def test_throttle_skips_rapid_successive_publishes(tmp_path, project) -> None:
    # A high throttle collapses the mid-run boundaries to the first publish; the terminal
    # transition ALWAYS bypasses the throttle so the final state still lands.
    eng = _engine(tmp_path, project, progress_throttle_s=10_000)
    eng.create_run("r1", ExecutionLane.FULL, progress_comments=True)
    eng.add_task("r1", "t1")

    _drive(eng)

    calls = project.task_source.progress
    assert len(calls) == 2  # first boundary + the terminal one
    assert "completed" in calls[-1]["body"].lower()


def test_pr_body_routing_once_pr_url_present(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, progress_throttle_s=0)
    eng.create_run("r1", ExecutionLane.FULL, progress_comments=True)
    eng.add_task("r1", "t1")

    _drive(eng)

    calls = project.task_source.progress
    # DELIVER sets pr_url; every publish AT/AFTER deliver routes to the PR, before it to the issue
    routed = [c["pr_url"] for c in calls]
    assert routed[0] is None and routed[1] is None
    assert any(u and u.endswith("/1234") for u in routed)
    # once a PR exists, it never reverts to an issue-only publish
    first_pr = next(i for i, u in enumerate(routed) if u)
    assert all(u for u in routed[first_pr:])


class _RaisingSource:
    """publish_progress raises; record() must survive and event the failure."""

    def __init__(self) -> None:
        from adapters.project.base import TaskSpec

        self._spec = TaskSpec(task_id="t1", title="x", body="y", issue_number=1)
        self.completed: list = []

    def resolve(self, task_id: str):
        return self._spec.model_copy(update={"task_id": task_id})

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        self.completed.append((task_id, pr_url))

    def publish_progress(self, task_id, body, *, marker, pr_url=None) -> None:
        raise RuntimeError("gh exploded")


def test_raising_publish_progress_never_breaks_record(tmp_path, project) -> None:
    project._task_source = _RaisingSource()
    eng = _engine(tmp_path, project, progress_throttle_s=0)
    eng.create_run("r1", ExecutionLane.FULL, progress_comments=True)
    eng.add_task("r1", "t1")

    outcomes = _drive(eng)

    assert outcomes[-1]["outcome"] == "task_completed"  # run finished despite the flaky hook
    events = _events(tmp_path)
    assert any(e["type"] == "progress_publish_failed" for e in events)
    assert not any(e["type"] == "progress_published" for e in events)


# --- render_progress ---------------------------------------------------------------


def test_render_progress_content() -> None:
    task = Task(
        task_id="#12", run_id="r1", created_at="2026-07-01T00:00:00+00:00",
        updated_at="t0", title="A long task", pr_url="https://github.com/o/r/pull/9",
    )
    task.stages[Stage.INTAKE].status = StageStatus.COMPLETED
    task.stages[Stage.INTAKE].attempt = 1
    task.stages[Stage.IMPLEMENT].status = StageStatus.RUNNING
    task.review_cycles = 1

    body = render_progress(task, now="2026-07-01T00:05:00+00:00")

    assert "Run progress — #12" in body
    assert "A long task" in body
    assert "https://github.com/o/r/pull/9" in body
    assert "5.0 min" in body  # elapsed from created_at → now
    assert "1 review cycle(s)" in body
    assert "✅" in body and "▶️" in body  # done + running glyphs
    assert "(next)" in body  # first pending stage flagged


# --- GitHubIssuesSource upsert (injectable runner, never hits the network) ----------


class _FakeGh:
    """A tiny in-memory GitHub that records the gh argv it was asked to run."""

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self._next_id = 100
        self.pr_body = ""
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> str:
        self.calls.append(argv)
        # issue comments listing
        if argv[:2] == ["gh", "api"] and "PATCH" not in argv and argv[2].endswith("/comments"):
            return json.dumps(self.comments)
        # edit an existing issue comment (PATCH)
        if argv[:2] == ["gh", "api"] and "PATCH" in argv:
            cid = int(argv[argv.index("PATCH") + 1].rsplit("/", 1)[-1])
            body = next(a for a in argv if a.startswith("body="))[len("body="):]
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = body
            return ""
        # create an issue comment
        if argv[:3] == ["gh", "issue", "comment"]:
            body = argv[argv.index("--body") + 1]
            self.comments.append({"id": self._next_id, "body": body})
            self._next_id += 1
            return ""
        # PR body read / edit
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps({"body": self.pr_body})
        if argv[:3] == ["gh", "pr", "edit"]:
            self.pr_body = argv[argv.index("--body") + 1]
            return ""
        raise AssertionError(f"unexpected gh argv: {argv}")


def test_issue_comment_upsert_creates_then_edits() -> None:
    gh = _FakeGh()
    src = GitHubIssuesSource("o/r", runner=gh.run)

    src.publish_progress("#10", "first body", marker="orchestrator:progress:#10")
    assert len(gh.comments) == 1
    assert gh.comments[0]["body"] == "<!-- orchestrator:progress:#10 -->\nfirst body"

    src.publish_progress("#10", "second body", marker="orchestrator:progress:#10")
    # still ONE comment — edited in place, not a new one (no comment spam)
    assert len(gh.comments) == 1
    assert gh.comments[0]["body"] == "<!-- orchestrator:progress:#10 -->\nsecond body"
    # the second publish issued a PATCH edit
    assert any("PATCH" in c for c in gh.calls)


def test_pr_body_section_upserted_idempotently() -> None:
    gh = _FakeGh()
    gh.pr_body = "Original PR description."
    src = GitHubIssuesSource("o/r", runner=gh.run)

    src.publish_progress("#10", "stage 1", marker="orchestrator:progress:#10",
                         pr_url="https://github.com/o/r/pull/7")
    assert "Original PR description." in gh.pr_body  # existing body preserved
    assert gh.pr_body.count("## Run progress") == 1
    assert "stage 1" in gh.pr_body

    src.publish_progress("#10", "stage 2", marker="orchestrator:progress:#10",
                         pr_url="https://github.com/o/r/pull/7")
    # the section is replaced in place — exactly one section, updated content
    assert gh.pr_body.count("## Run progress") == 1
    assert gh.pr_body.count("<!-- orchestrator:progress:#10:start -->") == 1
    assert "stage 2" in gh.pr_body and "stage 1" not in gh.pr_body
    assert "Original PR description." in gh.pr_body


def test_localfile_source_publish_progress_upserts(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"t1": {"title": "x", "body": "y"}}))
    src = LocalFileTaskSource(path)

    src.publish_progress("t1", "first", marker="orchestrator:progress:t1")
    src.publish_progress("t1", "second", marker="orchestrator:progress:t1")

    data = json.loads((tmp_path / "progress.json").read_text())
    assert list(data.keys()) == ["orchestrator:progress:t1"]  # single upserted entry
    assert data["orchestrator:progress:t1"]["body"] == "second"
