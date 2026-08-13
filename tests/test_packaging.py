"""Installable-library guarantees (#16).

Two layers:

- Fast unit tests for entry-point adapter discovery — a fake registered via a
  monkeypatched entry-point list, the in-repo reference adapters resolvable by short
  name, and the path/module lanes left unchanged.
- One slow smoke test that actually builds the wheel, installs it into a throwaway
  venv, and runs the console scripts + a resource-load check from OUTSIDE the repo cwd.
  It is the only real guard that packaging (data files, entry points, wheel-safe resource
  paths) works end-to-end. Marked ``slow`` so it can be deselected (``-m 'not slow'``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.project.base import ADAPTER_CONTRACT_VERSION
from adapters.project.selfhost.config import SelfHostConfig
from orchestrator import project_loader
from orchestrator.project_loader import load_engine_task_source, load_project

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------------------
# Fast: entry-point adapter discovery
# ---------------------------------------------------------------------------------------

class _FakeEntryPoint:
    """A stand-in for importlib.metadata.EntryPoint (only .name / .load() are used)."""

    def __init__(self, name: str, target) -> None:
        self.name = name
        self._target = target

    def load(self):
        return self._target


def test_reference_adapter_resolves_by_entry_point_name() -> None:
    # A short name isn't an importable module or a dir, so this exercises the real
    # `orchestrator.project_adapters` entry point registered by this package itself.
    # selfhost's config intentionally names itself after the repo it self-hosts.
    assert load_project("selfhost").name == "sous"


def test_engine_task_source_defaults_to_selfhost_tracker(monkeypatch) -> None:
    monkeypatch.delenv(project_loader.ENGINE_PROJECT_ENV, raising=False)
    monkeypatch.delenv("SELFHOST_TASKS", raising=False)
    monkeypatch.delenv("SELFHOST_REPO", raising=False)

    source = load_engine_task_source()

    assert getattr(source, "repo", None) == "cperler/sous"


def test_engine_task_source_project_is_configurable(monkeypatch) -> None:
    sentinel = object()
    seen: list[str] = []

    class Config:
        task_source = sentinel

    def fake_load_project(spec: str):
        seen.append(spec)
        return Config()

    monkeypatch.setenv(project_loader.ENGINE_PROJECT_ENV, "company-engine")
    monkeypatch.setattr(project_loader, "load_project", fake_load_project)

    assert load_engine_task_source() is sentinel
    assert seen == ["company-engine"]


def test_entry_point_configclass_form(monkeypatch) -> None:
    class MyConfig(SelfHostConfig):
        # A third-party ConfigClass carries its own class-level contract version.
        CONTRACT_VERSION = ADAPTER_CONTRACT_VERSION
        name = "myproj"

    ep = _FakeEntryPoint("myproj", MyConfig)
    monkeypatch.setattr(project_loader, "_project_adapter_entry_points", lambda: [ep])
    cfg = load_project("myproj")
    assert isinstance(cfg, MyConfig)
    assert cfg.name == "myproj"


def test_entry_point_contract_mismatch_fails_loudly(monkeypatch) -> None:
    class StaleConfig(SelfHostConfig):
        CONTRACT_VERSION = ADAPTER_CONTRACT_VERSION + 999
        name = "stale"

    ep = _FakeEntryPoint("stale", StaleConfig)
    monkeypatch.setattr(project_loader, "_project_adapter_entry_points", lambda: [ep])
    with pytest.raises(SystemExit, match="contract mismatch"):
        load_project("stale")


def test_unknown_project_adapter_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(project_loader, "_project_adapter_entry_points", lambda: [])
    with pytest.raises(SystemExit, match="unknown project adapter"):
        load_project("does-not-exist")


def test_path_based_loading_unchanged(monkeypatch) -> None:
    # No entry points at all: the dotted-module lane must still resolve in-repo adapters.
    monkeypatch.setattr(project_loader, "_project_adapter_entry_points", lambda: [])
    assert load_project("adapters.project.selfhost").name == "sous"


# ---------------------------------------------------------------------------------------
# Slow: real wheel build + install smoke test
# ---------------------------------------------------------------------------------------

def _run(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


@pytest.mark.slow
def test_wheel_builds_installs_and_runs(tmp_path) -> None:
    """Build the wheel, install it into a throwaway venv, and drive the console scripts +
    a resource-load check from OUTSIDE the repo — the only guard that the package is
    actually installable and CWD-independent."""
    if shutil.which("uv") is None:
        pytest.skip("uv not available for the wheel build/venv smoke test")

    dist = tmp_path / "dist"
    build = _run(["uv", "build", "--wheel", str(REPO), "-o", str(dist)])
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("*.whl"))
    assert wheels, "no wheel produced"

    venv = tmp_path / "venv"
    assert _run(["uv", "venv", str(venv)]).returncode == 0
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    env = {**os.environ, "VIRTUAL_ENV": str(venv)}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.pop("PYTHONPATH", None)  # don't let the repo tree leak onto the installed env's path
    install = _run(["uv", "pip", "install", "--python", str(bin_dir / "python"), str(wheels[0])], env=env)
    assert install.returncode == 0, install.stderr

    # Console scripts resolve and respond, run from a dir that is NOT the repo.
    for script in ("orchestrator", "orchestrator-scaffold"):
        r = _run([str(bin_dir / script), "--help"], cwd=str(tmp_path), env=env)
        assert r.returncode == 0, f"{script} --help failed: {r.stderr}"

    # Resource loads + entry-point discovery from the installed wheel, outside the repo.
    check = (
        "from orchestrator.schemas.stage_schemas import load_stage_schema;"
        "from orchestrator.scaffold import KIT_DIR, load_kit_manifest;"
        "from orchestrator.project_loader import load_project;"
        "assert load_stage_schema('implement'), 'stage schema not found in wheel';"
        "assert (KIT_DIR / 'manifest.toml').is_file(), ('kit missing: %s' % KIT_DIR);"
        "load_kit_manifest();"
        "assert load_project('selfhost').name, 'entry-point adapter not discovered';"
        "print('SMOKE_OK')"
    )
    r = _run([str(bin_dir / "python"), "-c", check], cwd=str(tmp_path), env=env)
    assert r.returncode == 0, r.stderr
    assert "SMOKE_OK" in r.stdout


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
