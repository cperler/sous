"""Validated SCOPE-authored child-task DAGs (#60)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import spec_intake
from .errors import OrchestratorError
from .schemas.enums import ImplementationBudget, QualityTier


class DecompositionError(OrchestratorError):
    """The optional SCOPE child graph is malformed."""


class ChildTaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1)
    agent: str | None = None
    quality_tier: QualityTier = QualityTier.FULL
    implementation_budget: ImplementationBudget = ImplementationBudget.STANDARD
    depends_on: list[str] = Field(default_factory=list)


def parse_subtasks(output: dict | None) -> list[ChildTaskPlan]:
    """Parse and DAG-validate an optional ``subtasks`` payload."""
    raw = (output or {}).get("subtasks")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise DecompositionError("scope subtasks must be a non-empty list when present")
    try:
        tasks = [ChildTaskPlan.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise DecompositionError(f"scope subtasks failed validation: {exc}") from exc
    spec = {
        "tasks": [{"id": task.id, "depends_on": task.depends_on} for task in tasks]
    }
    try:
        spec_intake.validate_dag(spec)
    except spec_intake.SpecError as exc:
        raise DecompositionError(str(exc)) from exc
    return tasks


def topological_order(tasks: list[ChildTaskPlan]) -> list[str]:
    spec = {
        "tasks": [{"id": task.id, "depends_on": task.depends_on} for task in tasks]
    }
    return spec_intake.topological_order(spec)


def leaf_ids(tasks: list[ChildTaskPlan]) -> list[str]:
    depended_on = {dep for task in tasks for dep in task.depends_on}
    return [task.id for task in tasks if task.id not in depended_on]
