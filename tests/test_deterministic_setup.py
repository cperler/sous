"""The deterministic ENGINE-lane intake runner (heysoo #227): worktree/baseline in
shell, no model call. Real-git-in-tmp coverage (mirrors tests/test_checkpoint.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adapters.execution.deterministic_setup import DeterministicSetupRunner
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


class _Proj:
    """A project WITHOUT setup_task -> exercises the built-in git logic. Noop install."""

    def install_cmd(self) -> list[str]:
        return ["true"]


def _wi(task: str = "#7") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task, stage=Stage.INTAKE, prompt="p",
        schema_ref="intake", model="engine", lane_policy=_ENGINE, created_at="now",
        checkpoint_tag="task/r1/-7/intake/0",
    )


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=path, check=True)


def test_real_git_setup_creates_worktree_and_tags(tmp_path, monkeypatch) -> None:
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.model == "engine"  # the $0 sentinel, not a real model
    assert (res.lane_used.execution_mode, res.lane_used.provider) == (
        ExecutionMode.ENGINE, Provider.NONE)
    out = res.structured_output
    assert out["branch"] == "task/7" and out["baseline_captured"] is True
    wt = Path(out["worktree"])
    assert wt.exists() and (wt / ".git").exists()  # a real linked worktree
    branches = subprocess.run(["git", "branch"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "task/7" in branches
    assert res.checkpoint and res.checkpoint["tag"] == "task/r1/-7/intake/0"  # baseline anchor


def test_reuse_existing_worktree_is_idempotent(tmp_path, monkeypatch) -> None:
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_Proj())

    first = runner.dispatch(_wi())
    second = runner.dispatch(_wi())  # retry: reuse the same worktree, no error

    assert first.status is second.status is ResultStatus.SUCCESS
    assert first.structured_output["worktree"] == second.structured_output["worktree"]


def test_non_git_dir_fails_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # a bare tmp dir, not a git repo

    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE  # a classifiable failure, not a schema_violation
    assert "git" in (res.error or "").lower()


def test_dispatch_yields_failure_when_setup_task_raises(tmp_path, monkeypatch) -> None:
    # Every dispatch MUST yield a StageResult — a raising setup_task (or a _git timeout)
    # must become FAILURE, never an escaped exception that leaves the lease held.
    monkeypatch.chdir(tmp_path)

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]

        def setup_task(self, task_id: str) -> dict:
            raise RuntimeError("boom")

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE
    assert "boom" in (res.error or "")


def test_project_setup_task_override_skips_git(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # NOT a git repo — the override must not touch git

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]

        def setup_task(self, task_id: str) -> dict:
            return {"branch": f"b/{task_id.lstrip('#')}", "worktree": "/wt/x",
                    "baseline_captured": True}

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output["branch"] == "b/7"
