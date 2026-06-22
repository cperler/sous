"""Self-host project-config adapter — the orchestration-template repo itself.

A pure Python/uv/pytest/ruff library: no frontend build, no lambda, no E2E/bats.
Demonstrates Phase 5 generality — the engine drives this with ZERO changes; only
this adapter differs from the Hey Soo! one (different commands, taxonomy, task source).
"""

from __future__ import annotations

import os

from orchestrator.schemas.enums import Stage

from .classifier import SelfHostClassifier
from .task_source import LocalFileTaskSource

_NOOP = ["true"]  # this project has no e2e / shell / infra layer

_ROSTER: dict[str, str] = {
    "implement": "python-implementer",
    "test": "python-implementer",
    "review": "code-reviewer",
    "docstring": "docstring-writer",
}


class SelfHostConfig:
    name = "orchestration-template"

    def __init__(self, tasks_path: str = "tasks.json") -> None:
        self._classifier = SelfHostClassifier()
        self._task_source = LocalFileTaskSource(tasks_path)

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
    def task_source(self) -> LocalFileTaskSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        return _ROSTER.get(role) if role else None


def get_config() -> SelfHostConfig:
    return SelfHostConfig(tasks_path=os.environ.get("SELFHOST_TASKS", "tasks.json"))
