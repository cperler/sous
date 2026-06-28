"""Hey Soo! reference project-config adapter (target.md §5).

Ports the Hey Soo! commands + roster + taxonomy from the bash system into the
project-config surface the engine consumes. The fix-forwards land here: a generic
docstring agent (no ``phpdoc-writer``), and the test-taxonomy living in config.
"""

from __future__ import annotations

import os

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import HeysooClassifier
from .task_source import GitHubIssuesSource

# Roster (ports the agent names; review/spec stay generic, frontend/backend are
# product-specific drop-ins). docstring -> a GENERIC docstring agent (fix D13).
_ROSTER: dict[str, str] = {
    "implement": "python-backend-developer",
    "implement:frontend": "bulletproof-frontend-developer",
    "test": "python-backend-developer",
    "review": "code-reviewer",
    "review:spec": "spec-reviewer",
    "docstring": "docstring-writer",  # generic; NOT phpdoc-writer
}


class HeysooConfig:
    name = "heysoo"

    def __init__(self, repo: str = "cperler/heysoo") -> None:
        self.repo = repo
        self._classifier = HeysooClassifier()
        self._task_source = GitHubIssuesSource(repo)

    # --- commands (ported; runners shell these inside a worktree) -------------
    def install_cmd(self) -> list[str]:
        return ["bash", "-lc", "npm install && uv sync"]

    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["uv", "run", "pytest", *files, "-v"]
        return ["bash", ".claude/scripts/test-unit.sh", "--skip-infra"]

    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["bash", ".claude/scripts/e2e-smoke.sh", *files]
        return ["bash", ".claude/scripts/e2e-smoke.sh"]

    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["bash", ".claude/scripts/test-shell.sh", *files]
        return ["bash", ".claude/scripts/test-shell.sh"]

    def typecheck_cmd(self) -> list[str]:
        return ["npx", "tsc", "--noEmit"]

    def infra_reset(self) -> list[str]:
        return ["bash", ".claude/scripts/reset-infra.sh"]

    # --- pluggable behavior ---------------------------------------------------
    @property
    def classifier(self) -> HeysooClassifier:
        return self._classifier

    @property
    def task_source(self) -> GitHubIssuesSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        if role and role in _ROSTER:
            return _ROSTER[role]
        return None

    def schema_for(self, ref: str) -> dict | None:
        # Inherit the engine's canonical stage-output contracts (gives codex full-validation).
        return resolve_stage_schema(ref)


def get_config() -> HeysooConfig:
    """Factory the CLI loads via ``--project adapters.project.heysoo``."""
    return HeysooConfig(repo=os.environ.get("HEYSOO_REPO", "cperler/heysoo"))
