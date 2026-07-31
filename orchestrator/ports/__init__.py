"""Engine-owned adapter PORTS — the contracts an adapter implements (#273).

The documented dependency rule is INWARD: ``adapters/`` depends on ``orchestrator/``,
never the reverse. These modules are the seam that makes that true — the Protocols,
value types, and registry abstractions the engine defines and a concrete project /
execution adapter implements:

- ``orchestrator.ports.project`` — ``ProjectConfig`` / ``TaskSource`` / ``TaskSpec`` and
  ``ADAPTER_CONTRACT_VERSION`` (the versioned surface an external adapter targets);
- ``orchestrator.ports.execution`` — ``Runner`` / ``CapabilityDescriptor`` / ``Registry``
  and the ``default_registry`` cell map.

Concrete implementations stay under ``adapters/``: the reference project adapters
(``adapters/project/{heysoo,selfhost}``) and the execution lanes
(``adapters/execution/{headless_claude,codex,interactive,...}``). The engine reaches a
concrete lane bundle only through ``orchestrator.lane_loader`` (an entry point), never
by importing it.

Re-exported here so ``from orchestrator.ports import ProjectConfig`` works; the
submodules remain the canonical homes.
"""

from __future__ import annotations

from orchestrator.ports.execution import (
    EXPLICIT_EMPTY,
    SUPPORTED,
    CapabilityDescriptor,
    CellStatus,
    Registry,
    Runner,
    default_registry,
)
from orchestrator.ports.project import (
    ADAPTER_CONTRACT_VERSION,
    ProjectConfig,
    TaskSource,
    TaskSpec,
)

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "EXPLICIT_EMPTY",
    "SUPPORTED",
    "CapabilityDescriptor",
    "CellStatus",
    "ProjectConfig",
    "Registry",
    "Runner",
    "TaskSource",
    "TaskSpec",
    "default_registry",
]
