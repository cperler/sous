"""Project-config adapter interface (target.md §5).

What a repo plugs in so the same engine drives any project. The Hey Soo! adapter
(``adapters/project/heysoo``) is the reference implementation. The engine depends
only on these Protocols, never on a concrete repo.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from orchestrator.failure_classifier import FailureClassifier
from orchestrator.schemas.enums import Stage


class TaskSpec(BaseModel):
    """A task resolved from a task source (e.g. a GitHub issue)."""

    task_id: str
    title: str = ""
    body: str = ""
    issue_number: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


@runtime_checkable
class TaskSource(Protocol):
    """Pluggable task provider (build-fresh D8; GitHub-Issues is the reference impl)."""

    def resolve(self, task_id: str) -> TaskSpec: ...

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None: ...


@runtime_checkable
class ProjectConfig(Protocol):
    """The full per-repo adapter surface."""

    name: str

    # --- commands (shelled by runners / test-support, never by the engine itself) ---
    def install_cmd(self) -> list[str]: ...
    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def typecheck_cmd(self) -> list[str]: ...
    def infra_reset(self) -> list[str]: ...

    # --- pluggable behavior ---
    @property
    def classifier(self) -> FailureClassifier: ...

    @property
    def task_source(self) -> TaskSource: ...

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        """Map a (stage, optional sub-role) to an agent name. Returns None for default.

        Includes a generic docstring agent for the deliver stage (fix-forward D13:
        no phantom ``phpdoc-writer``).
        """
        ...
