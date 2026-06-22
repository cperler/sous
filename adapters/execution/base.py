"""Execution-runner interface + registry (target.md §4).

A runner serves one or more (execution_mode, provider) cells: it consumes a
WorkItem and produces a StageResult. The registry resolves a ``LanePolicy`` to a
runner and fails fast (``assert_cells_covered``) if a required cell is unserved.

Phase 3a ships only the **interactive x claude** cell, which is served *externally*
by the in-session Workflow shim (it has no filesystem and is not a Python object the
engine can call). So its descriptor is marked ``in_process=False``: the supervisor
skill drives it and persists results via ``orchestrator record``. Headless and codex
runners (in-process) land in Phase 4.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from orchestrator.errors import NoRunnerError
from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import LanePolicy, StageResult, WorkItem


class CellStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str  # "supported" | "explicit_empty" | "unsupported"


SUPPORTED = CellStatus(value="supported")
EXPLICIT_EMPTY = CellStatus(value="explicit_empty")  # e.g. codex x interactive


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: ExecutionMode
    provider: Provider
    in_process: bool  # False = served externally (the interactive Workflow shim)
    supports_streaming: bool = False
    schema_enforced: bool = False
    cost_metered: bool = True
    status: CellStatus = SUPPORTED

    @property
    def cell(self) -> tuple[ExecutionMode, Provider]:
        return (self.execution_mode, self.provider)


@runtime_checkable
class Runner(Protocol):
    """In-process runner (headless/codex, Phase 4). Interactive is external."""

    def capabilities(self) -> list[CapabilityDescriptor]: ...

    def dispatch(self, work_item: WorkItem) -> StageResult: ...


class Registry:
    """Maps a LanePolicy -> the descriptor (and in-process runner) serving its cell."""

    def __init__(self) -> None:
        self._descriptors: dict[tuple[ExecutionMode, Provider], CapabilityDescriptor] = {}
        self._runners: dict[tuple[ExecutionMode, Provider], Runner] = {}

    def register_external(self, descriptor: CapabilityDescriptor) -> None:
        """Register a cell served out-of-process (the Workflow shim)."""
        self._descriptors[descriptor.cell] = descriptor

    def register_runner(self, runner: Runner) -> None:
        for desc in runner.capabilities():
            self._descriptors[desc.cell] = desc
            if desc.in_process:
                self._runners[desc.cell] = runner

    def describe(self, policy: LanePolicy) -> CapabilityDescriptor:
        cell = (policy.execution_mode, policy.provider)
        desc = self._descriptors.get(cell)
        if desc is None or desc.status is EXPLICIT_EMPTY:
            raise NoRunnerError(f"no runner for cell {cell[0].value} x {cell[1].value}")
        return desc

    def resolve(self, policy: LanePolicy) -> Runner:
        """Return the in-process runner for a cell (Phase 4). Raises for external cells."""
        desc = self.describe(policy)
        if not desc.in_process:
            raise NoRunnerError(
                f"cell {desc.execution_mode.value} x {desc.provider.value} is served "
                "externally (Workflow shim); the supervisor dispatches it, not the engine"
            )
        return self._runners[desc.cell]

    def sanctioned(self) -> set[tuple[ExecutionMode, Provider]]:
        """The (mode, provider) cells that are actually served (not explicit-empty).

        A model call is 'attributed/clean' iff its lane is one of these — this is
        how the lane audit generalizes beyond the 3a hardcoded interactive:claude.
        """
        return {
            cell for cell, desc in self._descriptors.items() if desc.status is not EXPLICIT_EMPTY
        }

    def assert_cells_covered(self, required: list[LanePolicy]) -> None:
        missing = [
            (p.execution_mode.value, p.provider.value)
            for p in required
            if (p.execution_mode, p.provider) not in self._descriptors
        ]
        if missing:
            raise NoRunnerError(f"required cells not covered: {missing}")


def default_registry() -> Registry:
    """Phase 3a registry: interactive x claude (external) + explicit-empty codex x interactive."""

    reg = Registry()
    reg.register_external(
        CapabilityDescriptor(
            execution_mode=ExecutionMode.INTERACTIVE,
            provider=Provider.CLAUDE,
            in_process=False,
            supports_streaming=True,
            schema_enforced=True,
            cost_metered=True,
            status=SUPPORTED,
        )
    )
    reg.register_external(
        CapabilityDescriptor(
            execution_mode=ExecutionMode.INTERACTIVE,
            provider=Provider.CODEX,
            in_process=False,
            status=EXPLICIT_EMPTY,
        )
    )
    return reg
