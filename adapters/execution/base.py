"""Back-compat re-export shim — the execution-runner contract moved inward (#273).

The canonical home is ``orchestrator.ports.execution``: the engine OWNS the runner
Protocol, the capability descriptors, and the ``Registry``; an execution adapter
implements them, so the dependency arrow points inward (``adapters/`` → ``orchestrator/``,
never the reverse).

Re-exports the same objects, so the ``SUPPORTED`` / ``EXPLICIT_EMPTY`` sentinels stay
IDENTICAL across both import paths — ``desc.status is EXPLICIT_EMPTY`` is an identity
check and would silently start answering False if this shim rebuilt them.

New code should import from ``orchestrator.ports.execution``.
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

__all__ = [
    "EXPLICIT_EMPTY",
    "SUPPORTED",
    "CapabilityDescriptor",
    "CellStatus",
    "Registry",
    "Runner",
    "default_registry",
]
