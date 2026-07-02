"""Shared GitHub-Issues task source (target.md §5, build-fresh D8).

Repo-agnostic: any project adapter whose tasks are GitHub issues instantiates this
with its repo slug (heysoo and selfhost both do). ``resolve`` reads an issue via
``gh``; ``mark_complete`` posts a PR link comment. The subprocess runner is
injectable so unit tests never hit the network.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

from adapters.project.base import TaskSpec

Runner = Callable[[list[str]], str]


def _gh(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _issue_number(task_id: str) -> str:
    return task_id.lstrip("#")


class GitHubIssuesSource:
    """Task source backed by GitHub issues via the ``gh`` CLI."""

    def __init__(self, repo: str, *, runner: Runner = _gh) -> None:
        self.repo = repo
        self._run = runner

    def resolve(self, task_id: str) -> TaskSpec:
        num = _issue_number(task_id)
        raw = self._run(
            ["gh", "issue", "view", num, "--repo", self.repo, "--json", "number,title,body,labels"]
        )
        data = json.loads(raw)
        labels = [lbl["name"] for lbl in data.get("labels", [])]
        return TaskSpec(
            task_id=task_id,
            title=data.get("title", ""),
            body=data.get("body") or "",
            issue_number=data.get("number"),
            depends_on=[],  # dependency analysis is the 3b scheduler's job
            labels=labels,
        )

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        num = _issue_number(task_id)
        body = f"Implemented via {pr_url}" if pr_url else "Implemented."
        self._run(["gh", "issue", "comment", num, "--repo", self.repo, "--body", body])
