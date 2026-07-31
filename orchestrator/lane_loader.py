"""Execution-lane bundle loading — the composition root's seam onto ``adapters/`` (#273).

The engine defines the execution PORT (``orchestrator.ports.execution``: ``Runner``,
``CapabilityDescriptor``, ``Registry``) but ships no lane. Something still has to name the
concrete bundle that wires headless×claude, codex, the deterministic ENGINE lane, and the
external interactive shim — and that "something" used to be a hard
``from adapters.execution.runners import build_registry`` at the top of ``cli.py``, which
inverted the documented dependency arrow at the one place it is most load-bearing.

So the lane bundle resolves the same way a project adapter already does: by NAME, through
an entry point. A packaged engine finds the bundle registered under the
``orchestrator.execution_lanes`` group (this repo registers its own reference bundle there,
self-testing the mechanism, exactly as it does for ``orchestrator.project_adapters``); a
source checkout with no distribution metadata falls back to the documented dotted default
below. Either way the reference is a STRING resolved at runtime, so no module under
``orchestrator/`` imports a concrete adapter.

A bundle is any module exposing the two callables the composition root needs:

- ``build_registry(**kwargs) -> Registry`` — the cells a run may dispatch;
- ``registry_runner(registry) -> AnyRunner`` — a scheduler-facing runner over them.

Both are duck-checked at load, so a drifted or half-written bundle yields a named-member
error at startup instead of an ``AttributeError`` mid-dispatch — the same fail-loudly
contract ``project_loader`` gives project adapters.
"""

from __future__ import annotations

import importlib
import importlib.metadata
from types import ModuleType
from typing import TYPE_CHECKING, Any

from orchestrator.ports.execution import Registry

if TYPE_CHECKING:  # the runner union lives in scheduler; import it for typing only
    from orchestrator.scheduler import AnyRunner

# The entry-point group an execution-lane bundle registers itself under.
ENTRY_POINT_GROUP = "orchestrator.execution_lanes"

# The bundle used when nothing is registered under the group — a source checkout run
# straight off a clone, with no installed distribution metadata to read. A dotted string,
# NOT an import: resolving it is what keeps the engine free of adapter imports.
DEFAULT_LANE_BUNDLE = "adapters.execution.runners"

# The callables the composition root calls on a bundle. Duck-checked at load so a drifted
# bundle names what it is missing instead of crashing at first dispatch.
_REQUIRED_MEMBERS = ("build_registry", "registry_runner")


def _lane_entry_points() -> list[importlib.metadata.EntryPoint]:
    """Registered ``orchestrator.execution_lanes`` entry points (indirection for tests)."""
    return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))


def _check_bundle(spec: str, module: ModuleType) -> ModuleType:
    missing = [m for m in _REQUIRED_MEMBERS if not callable(getattr(module, m, None))]
    if missing:
        raise SystemExit(
            f"execution-lane bundle {spec} does not satisfy the lane contract — "
            f"missing callable(s): {', '.join(missing)}"
        )
    return module


def load_lane_bundle(spec: str | None = None) -> ModuleType:
    """Resolve the execution-lane bundle module.

    ``spec`` (a dotted module path) wins when given — the explicit override for a project
    shipping its own lanes. Otherwise: the first registered ``orchestrator.execution_lanes``
    entry point, else ``DEFAULT_LANE_BUNDLE``.
    """
    if spec is not None:
        try:
            return _check_bundle(spec, importlib.import_module(spec))
        except ModuleNotFoundError as exc:
            raise SystemExit(f"execution-lane bundle {spec!r} is not importable: {exc}") from exc

    for ep in _lane_entry_points():
        target = ep.load()
        if not isinstance(target, ModuleType):  # a `pkg.mod:attr` value, not a module
            raise SystemExit(
                f"execution-lane entry point {ep.name!r} must point at a MODULE exposing "
                f"{'/'.join(_REQUIRED_MEMBERS)}, got {target!r}"
            )
        return _check_bundle(ep.name, target)

    try:
        return _check_bundle(DEFAULT_LANE_BUNDLE, importlib.import_module(DEFAULT_LANE_BUNDLE))
    except ModuleNotFoundError as exc:  # pragma: no cover - a broken install, not a code path
        raise SystemExit(
            f"no `{ENTRY_POINT_GROUP}` entry point is registered and the default bundle "
            f"{DEFAULT_LANE_BUNDLE} is not importable: {exc}"
        ) from exc


def build_registry(**kwargs: Any) -> Registry:
    """``build_registry`` from the resolved lane bundle (see module docstring).

    The result is checked against the engine's own ``Registry`` type rather than trusted:
    a bundle that hands back something else would otherwise surface as an obscure
    ``AttributeError`` deep inside ``next_work``'s lane resolution.
    """
    registry = load_lane_bundle().build_registry(**kwargs)
    if not isinstance(registry, Registry):
        raise SystemExit(
            "execution-lane bundle's build_registry did not return an "
            f"orchestrator.ports.execution.Registry (got {type(registry).__name__})"
        )
    return registry


def registry_runner(registry: Registry, **kwargs: Any) -> AnyRunner:
    """``registry_runner`` from the resolved lane bundle (see module docstring)."""
    runner: AnyRunner = load_lane_bundle().registry_runner(registry, **kwargs)
    return runner
