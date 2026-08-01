"""Local-file task source (target.md §5 — a second TaskSource impl, not GitHub).

Tasks live in a JSON file: {"<id>": {"title", "body", "depends_on": [...]}}.
``mark_complete`` appends to a sibling ``completed.log``; the optional evidence-out
hooks append to ``notes.log`` / ``followups.log``. This proves the task-source
provider is genuinely pluggable — a project with no GitHub workflow uses a different
source with zero engine changes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orchestrator.errors import OrchestratorError
from orchestrator.ports.project import TaskSpec


class LocalFileTaskSource:
    def __init__(self, tasks_path: str | Path) -> None:
        self.tasks_path = Path(tasks_path)

    def _load(self) -> dict:
        if not self.tasks_path.exists():
            raise OrchestratorError(f"tasks file not found: {self.tasks_path}")
        return json.loads(self.tasks_path.read_text())

    def resolve(self, task_id: str) -> TaskSpec:
        """Resolve a local task snapshot, including labels that drive engine policies.

        Raises ``OrchestratorError`` when the task file or requested task is absent.
        """
        data = self._load()
        if task_id not in data:
            raise OrchestratorError(f"unknown task {task_id!r} in {self.tasks_path}")
        t = data[task_id]
        return TaskSpec(
            task_id=task_id,
            title=t.get("title", ""),
            body=t.get("body", ""),
            depends_on=list(t.get("depends_on", [])),
            labels=list(t.get("labels", [])),
            provider_tag=t.get("provider_tag"),
            # #271: optional per-task last-modified stamp, mirroring the GitHub source's
            # ``updatedAt``. Absent from the file → None ("unknown"); the engine's staleness
            # verdict compares content fingerprints, so nothing depends on it being present.
            updated_at=t.get("updated_at"),
        )

    def list_tasks(self, label: str | None = None, limit: int = 50) -> list[TaskSpec]:
        """Return at most ``limit`` local tasks, optionally matching an exact label.

        The source order is preserved.  A missing task file is treated as an empty
        source so decomposition recovery can probe safely before the first local task
        is created.
        """
        data = self._load() if self.tasks_path.exists() else {}
        tasks: list[TaskSpec] = []
        for task_id, raw in data.items():
            labels = list(raw.get("labels", []))
            if label is not None and label not in labels:
                continue
            tasks.append(
                TaskSpec(
                    task_id=task_id,
                    title=raw.get("title", ""),
                    body=raw.get("body", ""),
                    depends_on=list(raw.get("depends_on", [])),
                    labels=labels,
                    provider_tag=raw.get("provider_tag"),
                    updated_at=raw.get("updated_at"),
                )
            )
            if len(tasks) >= limit:
                break
        return tasks

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        log = self.tasks_path.with_name("completed.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{task_id}\t{pr_url or ''}\n")

    def describe_issue(self, ref: str) -> dict:
        """Look up a task's state + PR for the conformance gate (#18 bullet 2) — the offline
        mirror of the GitHub source's ``describe_issue``. A task is ``closed`` once it has a
        line in the sibling ``completed.log`` (what ``mark_complete`` writes), else ``open``;
        the recorded PR url, if any, comes from that same line."""
        data = self._load() if self.tasks_path.exists() else {}
        entry = data.get(ref)
        body = entry.get("body", "") if isinstance(entry, dict) else ""
        state, pr = "open", None
        log = self.tasks_path.with_name("completed.log")
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                cols = line.split("\t")
                if cols and cols[0] == ref:
                    state = "closed"
                    pr = cols[1] if len(cols) > 1 and cols[1] else None
                    break
        return {"ref": ref, "state": state, "body": body, "pr": pr}

    # --- optional evidence-out hooks (the offline mirror of the GitHub source's; the
    # engine duck-types these, so implementing them keeps the local lane at parity) ---

    def publish_note(self, task_id: str, body: str, *, pr_url: str | None = None) -> None:
        """Append a run's completion note to a sibling ``notes.log`` (the local stand-in
        for a GitHub PR/issue comment)."""
        log = self.tasks_path.with_name("notes.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"# {task_id} {pr_url or ''}\n{body}\n\n")

    def publish_progress(
        self, task_id: str, body: str, *, marker: str, pr_url: str | None = None
    ) -> None:
        """Upsert mid-run progress (#64) into a sibling ``progress.json`` keyed by the
        ``marker`` — the offline stand-in for the GitHub source's one-living-comment upsert.
        Overwriting the same key each stage keeps a single current entry, never a pile."""
        path = self.tasks_path.with_name("progress.json")
        data = json.loads(path.read_text()) if path.exists() else {}
        data[marker] = {"task_id": task_id, "pr_url": pr_url, "body": body}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def file_followup(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> str | None:
        """Append a follow-up to a sibling ``followups.log`` and return a local ref (the
        offline stand-in for an opened GitHub issue)."""
        log = self.tasks_path.with_name("followups.log")
        ref = f"local:{title}"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{ref}\t{','.join(labels or [])}\t{title}\n{body}\n\n")
        return ref

    def create_task(self, title: str, body: str, labels: list[str] | None = None) -> str:
        """Create a new task in the tasks file and return its id (the offline mirror of the
        GitHub source's spec-filing hook, #18). Assigns the next free ``t<N>`` id so the
        spec front door's Depends-on translation works against the local lane too. The
        first case-insensitive ``Depends-on:`` body line is stored as structured dependency
        metadata so a decomposed child resolves with the same DAG edges after restart."""
        data = self._load() if self.tasks_path.exists() else {}
        n = len(data) + 1
        while f"t{n}" in data:
            n += 1
        task_id = f"t{n}"
        entry = {"title": title, "body": body, "labels": list(labels or [])}
        for line in body.splitlines():
            match = re.match(r"\s*depends[- ]on\s*:\s*(.+)", line, re.IGNORECASE)
            if match:
                deps = [part.strip() for part in match.group(1).split(",") if part.strip()]
                if deps:
                    entry["depends_on"] = deps
                break
        data[task_id] = entry
        self.tasks_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return task_id
