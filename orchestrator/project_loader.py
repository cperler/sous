"""Project-adapter loading — module path or project-owned directory (target.md §5).

Three ways a project plugs its adapter in:

- **module path** (``adapters.project.heysoo``) — the in-repo reference adapters,
  imported off ``sys.path`` and kept honest by this repo's test suite;
- **directory path** (``../my-project/.orchestration``) — the adapter lives in the
  *project's* repo and the engine loads it by path. The adapter executes inside the
  engine's process, so its ``orchestrator.*`` / ``adapters.project.base`` imports
  resolve against the engine — the project repo needs no Python packaging. This is the
  zero-packaging option and stays first-class;
- **entry-point name** (``heysoo``) — when the engine is ``pip``/``uv tool`` installed as
  a library, a project (or third-party) package registers its adapter under the
  ``orchestrator.project_adapters`` group. ``--project <name>`` then resolves by that name
  once a path/importable-module lookup misses. The entry-point value is either a package
  module (module-level ``CONTRACT_VERSION`` + ``get_config``) or ``pkg.module:ConfigClass``
  (a no-arg-constructible ``ProjectConfig`` carrying a class-level ``CONTRACT_VERSION``).
  The same contract check as directory-loaded adapters applies.

Because an external adapter is not updated atomically with the engine, it must declare
the contract it was generated against (module-level ``CONTRACT_VERSION``, written by
``orchestrator-scaffold``); a mismatch with ``ADAPTER_CONTRACT_VERSION`` fails loudly
at load, and the loaded config is duck-checked against the full ``ProjectConfig``
surface so a drifted adapter yields a named-member error instead of a mid-run crash.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path
from types import ModuleType

from adapters.project.base import ADAPTER_CONTRACT_VERSION, ProjectConfig

# The entry-point group a packaged project (or third-party) registers its adapter under.
ENTRY_POINT_GROUP = "orchestrator.project_adapters"

# Mirrors the ProjectConfig Protocol (adapters/project/base.py). ``schema_for`` and
# ``types_cmd`` are deliberately absent: both are optional, duck-typed via ``getattr``
# (the CLI for schema_for, the trunk gate for types_cmd) — so an older external adapter
# that predates them still satisfies the contract.
_REQUIRED_MEMBERS = [
    "name",
    "install_cmd",
    "test_unit_cmd",
    "test_e2e_cmd",
    "test_shell_cmd",
    "typecheck_cmd",
    "infra_reset",
    "classifier",
    "task_source",
    "agent_for",
]


def validate_config(config: object) -> list[str]:
    """The ProjectConfig members ``config`` is missing (empty list = satisfies the contract)."""
    return [m for m in _REQUIRED_MEMBERS if not hasattr(config, m)]


def _import_adapter_dir(path: Path) -> ModuleType:
    """Import ``<path>/__init__.py`` as a standalone package (relative imports work)."""
    init = path / "__init__.py"
    if not init.is_file():
        raise SystemExit(
            f"project adapter dir {path} has no __init__.py — generate one with orchestrator-scaffold"
        )
    mod_name = "_project_adapter_" + re.sub(r"\W+", "_", str(path.resolve()))
    if mod_name in sys.modules:  # already loaded this process (e.g. validate then run)
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(
        mod_name, init, submodule_search_locations=[str(path)]
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"project adapter dir {path} could not be imported (no loader for {init})")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # register BEFORE exec so `from .x import y` resolves
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _check_contract(spec: str, declared: int | None, *, external: bool) -> None:
    """Fail loudly on a missing (external) or mismatched adapter contract version."""
    if external and declared is None:
        raise SystemExit(
            f"external adapter {spec} declares no CONTRACT_VERSION — add "
            f"`CONTRACT_VERSION = {ADAPTER_CONTRACT_VERSION}` to its __init__.py "
            "(or regenerate with orchestrator-scaffold)"
        )
    if declared is not None and declared != ADAPTER_CONTRACT_VERSION:
        raise SystemExit(
            f"adapter contract mismatch: {spec} was generated against contract "
            f"{declared}, the engine speaks {ADAPTER_CONTRACT_VERSION} — re-run "
            "orchestrator-scaffold to regenerate the adapter"
        )


def _build_config(
    spec: str, factory: Callable[[], ProjectConfig] | None, *, external: bool
) -> ProjectConfig:
    """Instantiate a config from its factory (``get_config``/``ConfigClass``) and, for an
    externally-owned adapter, duck-check it against the full ProjectConfig surface."""
    if factory is None:
        raise SystemExit(f"project adapter {spec!r} exposes no get_config()/config factory")
    config = factory()
    if external:  # in-repo module adapters are covered by the test suite instead
        missing = validate_config(config)
        if missing:
            raise SystemExit(
                f"external adapter {spec} does not satisfy ProjectConfig — "
                f"missing: {', '.join(missing)}"
            )
    return config


def _config_from_module(spec: str, module: ModuleType, *, external: bool) -> ProjectConfig:
    """Load a config from a module exposing module-level ``CONTRACT_VERSION`` + ``get_config``."""
    _check_contract(spec, getattr(module, "CONTRACT_VERSION", None), external=external)
    return _build_config(spec, getattr(module, "get_config", None), external=external)


def _project_adapter_entry_points() -> EntryPoints:
    """Registered ``orchestrator.project_adapters`` entry points (indirection for tests)."""
    return importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)


def _load_entry_point(ep: EntryPoint) -> ProjectConfig:
    """Resolve a registered adapter entry point (always treated as external/contract-checked).

    The entry-point value is either a package module (module protocol) or a
    ``pkg.module:ConfigClass`` callable factory carrying its own ``CONTRACT_VERSION``."""
    target = ep.load()
    if inspect.ismodule(target):
        return _config_from_module(ep.name, target, external=True)
    # A ConfigClass (or get_config-style callable): contract from the target, else its module.
    declared = getattr(target, "CONTRACT_VERSION", None)
    if declared is None:
        declared = getattr(sys.modules.get(getattr(target, "__module__", "")), "CONTRACT_VERSION", None)
    _check_contract(ep.name, declared, external=True)
    factory = target if callable(target) else None
    return _build_config(ep.name, factory, external=True)


def load_project(spec: str) -> ProjectConfig:
    """Load a project adapter by directory path, importable module, or entry-point name."""
    path = Path(spec)
    if "/" in spec or path.is_dir():
        if not path.is_dir():
            raise SystemExit(f"project adapter directory not found: {spec}")
        return _config_from_module(spec, _import_adapter_dir(path), external=True)

    # A dotted, importable in-repo module (``adapters.project.heysoo``) — kept honest by
    # this repo's suite, so it isn't re-validated as external.
    try:
        module = importlib.import_module(spec)
    except ModuleNotFoundError:
        module = None
    if module is not None:
        return _config_from_module(spec, module, external=False)

    # Neither a path nor importable: resolve a registered entry-point adapter by name.
    for ep in _project_adapter_entry_points():
        if ep.name == spec:
            return _load_entry_point(ep)

    raise SystemExit(
        f"unknown project adapter {spec!r}: not a directory, an importable module, or a "
        f"registered `{ENTRY_POINT_GROUP}` entry point"
    )
