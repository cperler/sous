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
        self.progress: list[dict] = []  # publish_progress calls (mid-run, #64)
        self.candidates: list[TaskSpec] = []  # list_tasks output (batch-plan #57)

    def list_tasks(self, label: str | None = None, limit: int = 50) -> list[TaskSpec]:
        pool = self.candidates
        if label is not None:
            pool = [t for t in pool if label in t.labels]
        return pool[:limit]

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

    def publish_progress(
        self, task_id: str, body: str, *, marker: str, pr_url: str | None = None
    ) -> None:
        self.progress.append(
            {"task_id": task_id, "body": body, "marker": marker, "pr_url": pr_url}
        )

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

    def setup_task(self, task_id: str) -> dict:
        # No-git fake for the deterministic ENGINE-lane intake runner: tests must never
        # create real worktrees. Returns the intake contract with a synthetic worktree.
        safe = task_id.lstrip("#")
        return {"branch": f"task/{safe}", "worktree": f"/wt/{safe}", "baseline_captured": True}


@pytest.fixture
def project() -> FakeProject:
    return FakeProject()


@pytest.fixture(autouse=True)
def _isolate_learnings_kb(tmp_path, monkeypatch):
    """Isolate the cross-run learnings KB (#72) per test. The production default lives at
    the store-root's parent (``<runs-root>/learnings-kb.jsonl``), which for a single-run
    ``StatusStore(tmp_path)`` test would resolve to pytest's SHARED basetemp — leaking
    harvested learnings across tests. Pin it to each test's own tmp_path so harvest/fold
    stay hermetic; tests that want a shared KB (the two-run flow) just use one tmp_path."""
    monkeypatch.setenv("ORCHESTRATOR_LEARNINGS_KB_PATH", str(tmp_path / "learnings-kb.jsonl"))


def make_result(
    work: WorkItem,
    *,
    status: ResultStatus = ResultStatus.SUCCESS,
    structured_output: dict | None = None,
    error: str | None = None,
    tokens: TokenUsage | None = None,
    mode: ExecutionMode | None = None,
    provider: Provider | None = None,
    session_ref: str | None = None,
    checkpoint: dict | None = None,
    salvage: dict | None = None,
) -> StageResult:
    """Simulate a runner's StageResult answering a WorkItem. The lane defaults to the
    WorkItem's own policy (so a deterministic engine-lane stage records as engine:none),
    honoring an explicit mode/provider override when a test needs one."""
    mode = mode if mode is not None else work.lane_policy.execution_mode
    provider = provider if provider is not None else work.lane_policy.provider
    if structured_output is None and status is ResultStatus.SUCCESS:
        structured_output = _default_output(work.stage)
    return StageResult(
        session_ref=session_ref,
        checkpoint=checkpoint,
        salvage=salvage,
        work_item_id=work.id,
        content_hash=work.content_hash,
        run_id=work.run_id,
        task_id=work.task_id,
        stage=work.stage,
        attempt=work.attempt,
        model=work.model,
        effort=work.effort,  # echoed like a real runner (#96 audit thread)
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
