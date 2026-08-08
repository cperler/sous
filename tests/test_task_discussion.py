"""Issue comments are bounded task-spec input, not write-only evidence."""

from __future__ import annotations

import json

from adapters.project.github_issues import GitHubIssuesSource
from adapters.project.selfhost.task_source import LocalFileTaskSource
from adapters.project.task_discussion import append_discussion
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.status_store import StatusStore


def _issue_payload(comments: list[object]) -> str:
    return json.dumps(
        {
            "number": 7,
            "title": "Course correct",
            "body": "Original requirements",
            "labels": [],
            "state": "OPEN",
            "updatedAt": "2026-08-08T12:00:00Z",
            "comments": comments,
        }
    )


def test_github_resolve_includes_human_comments_and_filters_progress() -> None:
    calls: list[list[str]] = []
    comments: list[object] = [
        {
            "author": {"login": "reviewer"},
            "body": "Do not repeat the previous approach.",
            "createdAt": "2026-08-07T10:00:00Z",
            "updatedAt": "2026-08-07T11:00:00Z",
        },
        {
            "author": {"login": "automation"},
            "body": "<!-- orchestrator:progress:#7 -->\n## Run progress\n\nImplement running",
            "createdAt": "2026-08-07T12:00:00Z",
        },
    ]

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return _issue_payload(comments)

    spec = GitHubIssuesSource("o/r", runner=runner).resolve("#7")

    json_fields = calls[0][calls[0].index("--json") + 1]
    assert "comments" in json_fields
    assert spec.body.startswith("Original requirements\n\n## Discussion")
    assert "### Comment by @reviewer — 2026-08-07T10:00:00Z" in spec.body
    assert "(edited 2026-08-07T11:00:00Z)" in spec.body
    assert "Do not repeat the previous approach." in spec.body
    assert "Run progress" not in spec.body


def test_empty_or_progress_only_discussion_leaves_body_unchanged() -> None:
    progress = "<!-- orchestrator:progress:#7 -->\ncurrent stage"

    assert append_discussion("Task body\n", []) == "Task body\n"
    assert append_discussion("Task body\n", ["  ", progress]) == "Task body\n"


def test_discussion_keeps_newest_comments_and_discloses_count_truncation() -> None:
    comments: list[object] = ["oldest comment"] + [f"guidance-{n:02d}" for n in range(1, 21)]

    body = append_discussion("Task", comments)

    assert "oldest comment" not in body
    assert "guidance-01" in body and "guidance-20" in body
    assert "[truncated by orchestrator: kept 20 newest of 21 eligible comments;" in body


def test_discussion_caps_individual_and_aggregate_comment_text() -> None:
    comments: list[object] = [str(n) * 4_001 for n in range(5)]

    body = append_discussion("Task", comments)

    assert "0" * 100 not in body  # aggregate cap drops the oldest comment first
    assert all(str(n) * 4_000 in body for n in range(1, 5))
    assert body.count("… [truncated]") == 4
    assert "kept 4 newest of 5 eligible comments; 4005 comment chars dropped" in body


def test_local_file_resolve_supports_string_and_structured_comments(tmp_path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "t1": {
                    "title": "Local task",
                    "body": "Do the work",
                    "comments": [
                        "Plain correction",
                        {
                            "author": "maintainer",
                            "body": "Structured correction",
                            "created_at": "2026-08-07T10:00:00Z",
                            "updated_at": "2026-08-07T10:30:00Z",
                        },
                    ],
                }
            }
        )
    )

    spec = LocalFileTaskSource(path).resolve("t1")

    assert "Plain correction" in spec.body
    assert "### Comment by @maintainer — 2026-08-07T10:00:00Z" in spec.body
    assert "(edited 2026-08-07T10:30:00Z)" in spec.body
    assert "Structured correction" in spec.body


def test_comment_edit_is_stale_and_refreshes_the_next_prompt(tmp_path, project) -> None:
    comments: list[object] = [{"author": {"login": "reviewer"}, "body": "First guidance"}]
    source = GitHubIssuesSource("o/r", runner=lambda _argv: _issue_payload(comments))
    project._task_source = source
    engine = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)
    engine.create_run("r1")
    captured = engine.add_task("r1", "#7")
    assert "First guidance" in captured.body

    comments[0] = {"author": {"login": "reviewer"}, "body": "Corrected guidance"}
    stale = engine.spec_staleness("r1", "#7")
    assert stale["spec_stale"] is True

    report = engine.refresh_spec("r1", "#7")
    assert report["changed"] is True
    work = engine.next_work("r1", "#7")
    assert work is not None
    assert "Corrected guidance" in work.prompt
    assert "First guidance" not in work.prompt
