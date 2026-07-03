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

    def __init__(self, repo: str, *, runner: Runner = _gh, allow_closed: bool = False) -> None:
        self.repo = repo
        self._run = runner
        self._allow_closed = allow_closed

    def resolve(self, task_id: str) -> TaskSpec:
        num = _issue_number(task_id)
        raw = self._run(
            ["gh", "issue", "view", num, "--repo", self.repo,
             "--json", "number,title,body,labels,state"]
        )
        data = json.loads(raw)
        # Already-closed early exit (ports implement-orchestrator.sh:519): a batch over
        # a stale issue list must not burn a full pipeline and open a PR against a
        # closed issue. Loud refusal, opt-out via allow_closed for deliberate re-runs.
        state = str(data.get("state", "")).upper()
        if state == "CLOSED" and not self._allow_closed:
            raise ValueError(
                f"issue #{num} in {self.repo} is CLOSED — refusing to run "
                f"(pass allow_closed=True to the task source to override)"
            )
        labels = [lbl["name"] for lbl in data.get("labels", [])]
        return TaskSpec(
            task_id=task_id,
            title=data.get("title", ""),
            body=data.get("body") or "",
            issue_number=data.get("number"),
            depends_on=[],  # no analysis step yet; add-task --depends-on supplies edges
            labels=labels,
        )

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        num = _issue_number(task_id)
        body = f"Implemented via {pr_url}" if pr_url else "Implemented."
        self._run(["gh", "issue", "comment", num, "--repo", self.repo, "--body", body])

    # --- optional evidence-out hooks (duck-typed by the engine; not part of the v1
    # TaskSource contract, so an older external adapter without them still runs) ------

    def publish_note(self, task_id: str, body: str, *, pr_url: str | None = None) -> None:
        """Post a run's completion evidence. Prefers the PR thread when a ``pr_url`` is
        known (a full URL locates the repo+PR by itself, so no ``--repo``); otherwise
        comments on the issue."""
        if pr_url:
            self._run(["gh", "pr", "comment", pr_url, "--body", body])
        else:
            self._run(
                ["gh", "issue", "comment", _issue_number(task_id), "--repo", self.repo,
                 "--body", body]
            )

    def file_followup(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> str | None:
        """Open a follow-up issue (e.g. a review's non-blocking finding) and return the
        new issue URL (`gh issue create` prints it on stdout)."""
        argv = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels or []:
            argv += ["--label", label]
        return self._run(argv).strip() or None
