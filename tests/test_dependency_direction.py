"""The dependency arrow points INWARD — enforced, not just documented (#273).

README/ARCHITECTURE state the rule: ``adapters/`` depends on ``orchestrator/``, and the
engine imports no adapter. Before #273 that was false — ``engine.py`` imported
``Registry``/``ProjectConfig`` out of ``adapters/*/base.py`` and the CLI imported the
concrete lane bundle — so the docs described an architecture the code did not have.

These tests are the guard that keeps the two in agreement. They walk the AST rather than
importing, so they see lazy/function-local imports (the CLI's several
``from adapters... import`` lines inside subcommand branches were exactly that shape) and
``importlib.import_module("adapters...")`` string literals, which a runtime import check
would miss.

The allowlist is deliberately EMPTY. If a future change needs an exception, that is an
architecture decision to argue in review, not a line to quietly append here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO / "orchestrator"
PORTS_DIR = ENGINE_DIR / "ports"

# Modules under orchestrator/ permitted to depend on `adapters.*`. Empty by design.
ALLOWED: frozenset[str] = frozenset()


def _engine_modules() -> list[Path]:
    return sorted(p for p in ENGINE_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _adapter_refs(path: Path) -> list[str]:
    """Every way ``path`` names an ``adapters.*`` module: static import, function-local
    import, and ``importlib.import_module``/``__import__`` on a literal string."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    refs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs += [a.name for a in node.names if a.name == "adapters" or a.name.startswith("adapters.")]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == "adapters" or mod.startswith("adapters.")):
                refs.append(mod)
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else ""
            )
            if name in {"import_module", "__import__"} and node.args:
                arg = node.args[0]
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and (arg.value == "adapters" or arg.value.startswith("adapters."))
                ):
                    refs.append(arg.value)
    return refs


def test_engine_modules_exist_to_check() -> None:
    """Guard the guard: a walk that silently found nothing would pass vacuously."""
    modules = _engine_modules()
    assert len(modules) > 20, f"expected the engine package, found {len(modules)} modules"
    assert ENGINE_DIR / "engine.py" in modules
    assert ENGINE_DIR / "cli.py" in modules


@pytest.mark.parametrize("path", _engine_modules(), ids=lambda p: p.name)
def test_no_engine_module_imports_an_adapter(path: Path) -> None:
    rel = path.relative_to(REPO).as_posix()
    if rel in ALLOWED:
        return
    refs = _adapter_refs(path)
    assert not refs, (
        f"{rel} depends on {sorted(set(refs))} — the arrow must point inward. Depend on a "
        f"contract in orchestrator/ports/, or resolve the concrete adapter by NAME "
        f"(orchestrator.lane_loader / orchestrator.project_loader)."
    )


def test_ports_package_is_self_contained() -> None:
    """The ports ARE the inward contracts, so they may not reach outward either."""
    for path in sorted(PORTS_DIR.glob("*.py")):
        assert not _adapter_refs(path), f"{path.name} (a port) depends on an adapter"


def test_contract_types_live_in_ports_not_adapters() -> None:
    """The contracts moved; the old modules are re-export shims, not second definitions.

    A shim that re-DEFINED the types would give two distinct ``ProjectConfig`` classes and
    two distinct ``EXPLICIT_EMPTY`` sentinels — and ``desc.status is EXPLICIT_EMPTY`` is an
    identity check, so a run would silently start treating explicit-empty cells as served.
    """
    from adapters.execution import base as exec_shim
    from adapters.project import base as project_shim
    from orchestrator.ports import execution as exec_port
    from orchestrator.ports import project as project_port

    assert project_shim.ProjectConfig is project_port.ProjectConfig
    assert project_shim.TaskSpec is project_port.TaskSpec
    assert project_shim.TaskSource is project_port.TaskSource
    assert project_shim.ADAPTER_CONTRACT_VERSION == project_port.ADAPTER_CONTRACT_VERSION
    assert exec_shim.Registry is exec_port.Registry
    assert exec_shim.EXPLICIT_EMPTY is exec_port.EXPLICIT_EMPTY
    assert exec_shim.SUPPORTED is exec_port.SUPPORTED

    # The definitions really are in the ports (a shim re-exporting a shim would pass the
    # identity checks above while leaving the contract outward).
    assert project_port.ProjectConfig.__module__ == "orchestrator.ports.project"
    assert exec_port.Registry.__module__ == "orchestrator.ports.execution"


def test_scaffolded_adapter_template_imports_inward() -> None:
    """A NEWLY scaffolded project-owned adapter must depend on the port, not the shim."""
    from orchestrator import scaffold

    assert "from orchestrator.ports.project import TaskSpec" in scaffold._TASK_SOURCE
    assert "adapters.project.base" not in scaffold._TASK_SOURCE
