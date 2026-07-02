"""Shared test fakes — a minimal in-memory ProjectConfig + StageResult builder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from adapters.project.base import TaskSpec
from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage, WorkItem


class FakeClassifier:
    def classify(self, test_output: str) -> list[Failure]:
        return [Failure(test=line, kind="unit") for line in test_output.splitlines() if line]

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        return [f"test_{f}" for f in changed_files]


class FakeTaskSource:
    def __init__(self) -> None:
        self.completed: list[tuple[str, str | None]] = []
        self.deps: dict[str, list[str]] = {}  # task_id -> depends_on (test-configurable)
        self.notes: list[dict] = []  # publish_note calls
        self.followups: list[dict] = []  # file_followup calls

    def resolve(self, task_id: str) -> TaskSpec:
        return TaskSpec(
            task_id=task_id,
            title=f"Fake {task_id}",
            body="do it",
            issue_number=42,
            depends_on=self.deps.get(task_id, []),
        )

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        self.completed.append((task_id, pr_url))

    # Optional evidence-out hooks (duck-typed by the engine at finalize).
    def publish_note(self, task_id: str, body: str, *, pr_url: str | None = None) -> None:
        self.notes.append({"task_id": task_id, "body": body, "pr_url": pr_url})

    def file_followup(self, title: str, body: str, labels: list[str] | None = None) -> str:
        ref = f"https://example.test/issues/{len(self.followups) + 1}"
        self.followups.append({"title": title, "body": body, "labels": labels, "ref": ref})
        return ref


class FakeProject:
    name = "fake"

    def __init__(self) -> None:
        self._classifier = FakeClassifier()
        self._task_source = FakeTaskSource()

    def install_cmd(self):
        return ["echo", "install"]

    def test_unit_cmd(self, files=None):
        return ["echo", "unit"]

    def test_e2e_cmd(self, files=None):
        return ["echo", "e2e"]

    def test_shell_cmd(self, files=None):
        return ["echo", "shell"]

    def typecheck_cmd(self):
        return ["echo", "typecheck"]

    def infra_reset(self):
        return ["echo", "reset"]

    @property
    def classifier(self):
        return self._classifier

    @property
    def task_source(self):
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None):
        return {"implement": "impl-agent", "review": "code-reviewer", "docstring": "docstring-agent"}.get(role)


@pytest.fixture
def project() -> FakeProject:
    return FakeProject()


def make_result(
    work: WorkItem,
    *,
    status: ResultStatus = ResultStatus.SUCCESS,
    structured_output: dict | None = None,
    error: str | None = None,
    tokens: TokenUsage | None = None,
    mode: ExecutionMode = ExecutionMode.INTERACTIVE,
    provider: Provider = Provider.CLAUDE,
    session_ref: str | None = None,
    checkpoint: dict | None = None,
) -> StageResult:
    """Simulate a runner's StageResult answering a WorkItem."""
    if structured_output is None and status is ResultStatus.SUCCESS:
        structured_output = _default_output(work.stage)
    return StageResult(
        session_ref=session_ref,
        checkpoint=checkpoint,
        work_item_id=work.id,
        content_hash=work.content_hash,
        run_id=work.run_id,
        task_id=work.task_id,
        stage=work.stage,
        attempt=work.attempt,
        model=work.model,
        status=status,
        structured_output=structured_output,
        error=error,
        lane_used=LaneUsed(
            execution_mode=mode, provider=provider, invocation=f"agent(model={work.model})"
        ),
        token_usage=tokens or TokenUsage(input=1000, output=200),
        completed_at=datetime.now(UTC).isoformat(),
    )


def _default_output(stage: Stage) -> dict:
    return {
        Stage.INTAKE: {"branch": "issue-42", "worktree": "/wt/42", "baseline_captured": True},
        Stage.SCOPE: {"feasible": True, "plan": ["subtask-1"]},
        Stage.IMPLEMENT: {"files_changed": ["a.py"], "summary": "done", "committed": True},
        Stage.TEST: {"passed": True, "failures": [], "tests_meaningful": True,
                     "validation_notes": "asserts the changed behavior"},
        Stage.DELIVER: {"pr_number": 1234, "pr_url": "https://github.com/x/y/pull/1234"},
        Stage.REVIEW: {"approved": True, "issues": []},
    }[stage]
