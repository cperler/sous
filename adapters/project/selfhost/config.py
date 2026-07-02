"""Self-host project-config adapter — the orchestration-template repo itself.

A pure Python/uv/pytest/ruff library: no frontend build, no lambda, no E2E/bats.
Demonstrates Phase 5 generality — the engine drives this with ZERO changes; only
this adapter differs from the Hey Soo! one (different commands, taxonomy, task source).

Tasks default to THIS repo's GitHub issues (the harness's own issue log is directly
orchestratable — the dogfood loop); set ``SELFHOST_TASKS`` to a JSON file for the
local/offline mode instead.
"""

from __future__ import annotations

import os

from adapters.project.github_issues import GitHubIssuesSource
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import SelfHostClassifier
from .task_source import LocalFileTaskSource

_SELF_REPO = "cperler/orchestration-template"

_NOOP = ["true"]  # this project has no e2e / shell / infra layer

# Stage sub-role -> agent name. These resolve to the starter-kit personas seeded into
# this repo's own ``.claude/agents/`` (bootstrapped from templates/project-default), so
# both lanes can name them: the interactive lane as a subagent type, the headless lane as
# ``claude -p --agent <name>``.
_ROSTER: dict[str, str] = {
    "implement": "python-backend-developer",
    "test": "test-validator",
    "review": "code-reviewer",
    "docstring": "docstring-writer",
}


class SelfHostConfig:
    name = "orchestration-template"

    def __init__(self, tasks_path: str | None = None, repo: str = _SELF_REPO) -> None:
        self._classifier = SelfHostClassifier()
        self._task_source = (
            LocalFileTaskSource(tasks_path) if tasks_path else GitHubIssuesSource(repo)
        )

    def install_cmd(self) -> list[str]:
        return ["uv", "sync"]

    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]:
        return ["uv", "run", "pytest", *files, "-q"] if files else ["uv", "run", "pytest", "-q"]

    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP  # no E2E layer

    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP

    def typecheck_cmd(self) -> list[str]:
        return ["uv", "run", "ruff", "check", "."]  # ruff is this project's gate

    def infra_reset(self) -> list[str]:
        return _NOOP

    @property
    def classifier(self) -> SelfHostClassifier:
        return self._classifier

    @property
    def task_source(self) -> LocalFileTaskSource | GitHubIssuesSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        return _ROSTER.get(role) if role else None

    def schema_for(self, ref: str) -> dict | None:
        return resolve_stage_schema(ref)


def get_config() -> SelfHostConfig:
    return SelfHostConfig(
        tasks_path=os.environ.get("SELFHOST_TASKS"),
        repo=os.environ.get("SELFHOST_REPO", _SELF_REPO),
    )
