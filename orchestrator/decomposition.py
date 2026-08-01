"""Parse SCOPE-authored child plans for registration on the run-level task DAG.

Decomposition deliberately reuses the durable task graph instead of introducing an
intra-task execution loop.  This module validates the model-authored boundary before
the engine performs any external child-task creation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import spec_intake
from .errors import OrchestratorError
from .schemas.enums import ImplementationBudget, QualityTier


class DecompositionError(OrchestratorError):
    """The optional SCOPE child graph is malformed."""


class ChildTaskPlan(BaseModel):
    """One child task and its execution controls, keyed by a plan-local id.

    ``depends_on`` contains other local ids; the engine translates them to durable
    task-source references only after the complete child graph has been validated.
    """

    model_config = ConfigDict(extra="forbid")

    # Single-line, no whitespace: the id goes into the ``Decomposition-key: <parent>/<id>``
    # body marker that crash recovery matches as a whole line. A newline in the id would
    # split the marker across lines and make every lookup miss, filing a duplicate child.
    id: str = Field(min_length=1, max_length=80, pattern=r"^\S+$")
    description: str = Field(min_length=1)
    agent: str | None = None
    quality_tier: QualityTier = QualityTier.FULL
    implementation_budget: ImplementationBudget = ImplementationBudget.STANDARD
    depends_on: list[str] = Field(default_factory=list)


def parse_subtasks(output: dict | None) -> list[ChildTaskPlan]:
    """Parse and DAG-validate an optional SCOPE ``subtasks`` payload.

    Returns an empty list when decomposition was not requested.  A present payload
    must be a non-empty, closed acyclic graph; malformed controls, duplicate/unknown
    ids, and cycles raise :class:`DecompositionError` before any filing can begin.
    """
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
    """Return local child ids in dependency-first filing order.

    Callers must pass plans previously accepted by :func:`parse_subtasks`.
    """

    spec = {
        "tasks": [{"id": task.id, "depends_on": task.depends_on} for task in tasks]
    }
    return spec_intake.topological_order(spec)


def leaf_ids(tasks: list[ChildTaskPlan]) -> list[str]:
    """Return children that no sibling depends on, preserving plan order.

    These leaves become the umbrella parent's dependencies: completing every leaf
    implies that every transitive prerequisite in the validated DAG also completed.
    """

    depended_on = {dep for task in tasks for dep in task.depends_on}
    return [task.id for task in tasks if task.id not in depended_on]
