"""Pre-merge batch integration gate (#370).

Per-task review can only see one branch. These tests use real git branches to prove the
run-finalize gate sees both kinds of between-task break: a clean git composition whose
combined behavior is red, and a composition conflict that prevents verification entirely.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _branch(repo: Path, name: str, files: dict[str, str]) -> None:
    _git(repo, "checkout", "-q", "-b", name, "main")
    for filename, content in files.items():
        (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", name)
    _git(repo, "checkout", "-q", "main")


class _IntegrationProject(FakeProject):
    def __init__(self, repo: Path, test_command: str = "true") -> None:
        super().__init__()
        self.repo_root = str(repo)
        self.test_command = test_command

    def test_unit_cmd(self, files=None):
        return ["sh", "-c", self.test_command]

    def test_e2e_cmd(self, files=None):
        return ["true"]

    def test_shell_cmd(self, files=None):
        return ["true"]

    def typecheck_cmd(self):
        return ["true"]


def _engine(tmp_path: Path, project: _IntegrationProject) -> Engine:
    root = tmp_path / "run"
    return Engine(StatusStore(root), CostLedger(root / "stage-costs.jsonl"), project)


def _complete(eng: Engine, task_id: str, branch: str | None, *, umbrella=False) -> None:
    def mutate(task) -> None:
        task.state = TaskState.COMPLETED
        if branch is not None:
            task.context["branch"] = branch
        if umbrella:
            task.decomposition_children = ["a", "b"]

    eng.store.update_task("r1", task_id, mutate)
    eng._set_ref_state("r1", task_id, TaskState.COMPLETED)


def _artifact(eng: Engine) -> dict:
    return json.loads(
        (eng.store.root / "batch-integration-gate.json").read_text(encoding="utf-8")
    )


def test_finalize_catches_clean_auto_merge_that_is_red_only_when_combined(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "task/a", {"changed-api": "three values\n"})
    _branch(repo, "task/b", {"old-caller": "two values\n"})
    project = _IntegrationProject(
        repo,
        "test ! -f changed-api || test ! -f old-caller",
    )
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "a")
    eng.add_task("r1", "b")
    _complete(eng, "a", "task/a")
    _complete(eng, "b", "task/b")

    eng._maybe_finalize_run("r1")

    result = _artifact(eng)
    assert result["green"] is False
    assert result["failing"] == ["test_unit"]
    assert [entry["branch"] for entry in result["branches"]] == ["task/a", "task/b"]
    assert len(project.task_source.followups) == 1
    assert project.task_source.followups[0]["labels"] == ["bug"]
    assert eng.store.load_run("r1").state.value == "completed"
    finalized = next(
        event for event in eng.store.read_events("r1") if event["type"] == "run_finalized"
    )
    assert finalized["integration_gate"]["green"] is False
    assert "sous-batch-integration-" not in _git(repo, "worktree", "list").stdout

    repeated = eng.batch_integration_gate("r1")
    assert repeated["deduped"] is True
    assert len(project.task_source.followups) == 1


def test_merge_conflict_is_red_and_verification_does_not_run(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "task/a", {"shared.txt": "from a\n"})
    _branch(repo, "task/b", {"shared.txt": "from b\n"})
    project = _IntegrationProject(repo, "exit 99")
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "a")
    eng.add_task("r1", "b")
    _complete(eng, "a", "task/a")
    _complete(eng, "b", "task/b")

    result = eng.batch_integration_gate("r1")

    assert result["green"] is False
    assert result["failing"] == ["merge:b"]
    assert not [command for command in result["commands"] if command["name"] == "test_unit"]
    assert {"name": "verification", "reason": "composition_red"} in result["skipped"]
    assert result["cleanup"] == {"attempted": True, "rc": 0}
    assert "sous-batch-integration-" not in _git(repo, "worktree", "list").stdout


def test_dependency_order_skips_umbrella_and_green_batch_finalizes(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "task/a", {"a.txt": "a\n"})
    _branch(repo, "task/b", {"b.txt": "b\n"})
    project = _IntegrationProject(repo)
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "a")
    eng.add_task("r1", "b", depends_on=["a"])
    eng.add_task("r1", "umbrella", depends_on=["a", "b"])
    _complete(eng, "a", "task/a")
    _complete(eng, "b", "task/b")
    _complete(eng, "umbrella", None, umbrella=True)

    eng._maybe_finalize_run("r1")

    result = _artifact(eng)
    assert result["green"] is True
    assert [entry["task_id"] for entry in result["branches"]] == ["a", "b"]
    assert {"task_id": "umbrella", "reason": "decomposition_umbrella"} in result[
        "skipped_tasks"
    ]
    merge_names = [
        command["name"] for command in result["commands"] if command["name"].startswith("merge:")
    ]
    assert merge_names == ["merge:a", "merge:b"]
    command_names = [command["name"] for command in result["commands"]]
    assert command_names.index("merge:b") < command_names.index("install")
    assert command_names.index("install") < command_names.index("test_unit")
    assert project.task_source.followups == []


def test_completed_code_task_without_branch_makes_batch_red(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "task/a", {"a.txt": "a\n"})
    project = _IntegrationProject(repo)
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "a")
    eng.add_task("r1", "b")
    _complete(eng, "a", "task/a")
    _complete(eng, "b", None)

    result = eng.batch_integration_gate("r1")

    assert result["green"] is False
    assert result["reason"] == "branch_inputs_invalid"
    assert result["failing"] == ["branch_input:b"]
    assert result["input_errors"] == [{"task_id": "b", "reason": "no_branch"}]
    assert len(project.task_source.followups) == 1


def test_dispatchable_recovers_kill_after_last_task_became_terminal(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch(repo, "task/a", {"a.txt": "a\n"})
    _branch(repo, "task/b", {"b.txt": "b\n"})
    eng = _engine(tmp_path, _IntegrationProject(repo))
    eng.create_run("r1")
    eng.add_task("r1", "a")
    eng.add_task("r1", "b")
    _complete(eng, "a", "task/a")
    _complete(eng, "b", "task/b")
    assert eng.store.load_run("r1").state.value == "running"

    assert eng.dispatchable("r1") == []

    assert eng.store.load_run("r1").state.value == "completed"
    assert _artifact(eng)["green"] is True
