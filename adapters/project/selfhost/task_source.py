"""Local-file task source (target.md §5 — a second TaskSource impl, not GitHub).

Tasks live in a JSON file: {"<id>": {"title", "body", "depends_on": [...]}}.
``mark_complete`` appends to a sibling ``completed.log``. This proves the task-source
provider is genuinely pluggable — a project with no GitHub workflow uses a different
source with zero engine changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from adapters.project.base import TaskSpec
from orchestrator.errors import OrchestratorError


class LocalFileTaskSource:
    def __init__(self, tasks_path: str | Path) -> None:
        self.tasks_path = Path(tasks_path)

    def _load(self) -> dict:
        if not self.tasks_path.exists():
            raise OrchestratorError(f"tasks file not found: {self.tasks_path}")
        return json.loads(self.tasks_path.read_text())

    def resolve(self, task_id: str) -> TaskSpec:
        data = self._load()
        if task_id not in data:
            raise OrchestratorError(f"unknown task {task_id!r} in {self.tasks_path}")
        t = data[task_id]
        return TaskSpec(
            task_id=task_id,
            title=t.get("title", ""),
            body=t.get("body", ""),
            depends_on=list(t.get("depends_on", [])),
            provider_tag=t.get("provider_tag"),
        )

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        log = self.tasks_path.with_name("completed.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{task_id}\t{pr_url or ''}\n")
