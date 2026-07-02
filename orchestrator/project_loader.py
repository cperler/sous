"""Project-adapter loading — module path or project-owned directory (target.md §5).

Two ways a project plugs its adapter in:

- **module path** (``adapters.project.heysoo``) — the in-repo reference adapters,
  imported off ``sys.path`` and kept honest by this repo's test suite;
- **directory path** (``../my-project/.orchestration``) — the adapter lives in the
  *project's* repo and the engine loads it by path. The adapter executes inside the
  engine's process, so its ``orchestrator.*`` / ``adapters.project.base`` imports
  resolve against the engine — the project repo needs no Python packaging.

Because an external adapter is not updated atomically with the engine, it must declare
the contract it was generated against (module-level ``CONTRACT_VERSION``, written by
``orchestrator-scaffold``); a mismatch with ``ADAPTER_CONTRACT_VERSION`` fails loudly
at load, and the loaded config is duck-checked against the full ``ProjectConfig``
surface so a drifted adapter yields a named-member error instead of a mid-run crash.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

from adapters.project.base import ADAPTER_CONTRACT_VERSION, ProjectConfig

# Mirrors the ProjectConfig Protocol (adapters/project/base.py). ``schema_for`` is
# deliberately absent: it is optional, duck-typed via ``getattr`` by the CLI.
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


def _import_adapter_dir(path: Path):
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
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # register BEFORE exec so `from .x import y` resolves
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def load_project(spec: str) -> ProjectConfig:
    """Load a project adapter from a module path or a project-owned directory."""
    path = Path(spec)
    external = "/" in spec or path.is_dir()
    if external:
        if not path.is_dir():
            raise SystemExit(f"project adapter directory not found: {spec}")
        module = _import_adapter_dir(path)
    else:
        module = importlib.import_module(spec)

    declared = getattr(module, "CONTRACT_VERSION", None)
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

    factory = getattr(module, "get_config", None)
    if factory is None:
        raise SystemExit(f"project module {spec!r} has no get_config()")
    config = factory()
    if external:  # in-repo module adapters are covered by the test suite instead
        missing = validate_config(config)
        if missing:
            raise SystemExit(
                f"external adapter {spec} does not satisfy ProjectConfig — "
                f"missing: {', '.join(missing)}"
            )
    return config
