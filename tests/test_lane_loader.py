"""The execution-lane bundle seam (#273).

The composition root (`cli.py`, `dashboard.py`) needs a CONCRETE lane bundle but may not
import one, so it resolves the bundle by name — entry point first, documented dotted
default second — exactly as `project_loader` resolves a project adapter. These tests cover
the resolution order and the fail-loudly contract check, since a bundle that silently
half-loads would surface as an AttributeError mid-dispatch instead of at startup.
"""

from __future__ import annotations

import types

import pytest

from orchestrator import lane_loader
from orchestrator.ports.execution import Registry


class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint (only .name / .load() are used)."""

    def __init__(self, name: str, target: object) -> None:
        self.name = name
        self._target = target

    def load(self) -> object:
        return self._target


def _bundle(name: str = "fake_lanes", **members: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in members.items():
        setattr(mod, key, value)
    return mod


def _ok_bundle() -> types.ModuleType:
    return _bundle(
        build_registry=lambda **kw: Registry(),
        registry_runner=lambda registry, **kw: ("runner", registry),
    )


# --- resolution order -------------------------------------------------------------------

def test_default_bundle_is_the_reference_lanes() -> None:
    """With no override, the resolved bundle really serves the shipped lanes."""
    bundle = lane_loader.load_lane_bundle()
    registry = bundle.build_registry(include_interactive=False)
    assert isinstance(registry, Registry)
    # It covers real cells — a bundle that returned an empty registry would still be a
    # Registry, so assert the lanes the engine actually dispatches are present.
    assert registry.sanctioned(), "resolved bundle registered no cells"


def test_this_repo_registers_its_bundle_under_the_entry_point_group() -> None:
    """The group is self-tested here the way project_adapters is (#273)."""
    names = {ep.name for ep in lane_loader._lane_entry_points()}
    assert names, f"no `{lane_loader.ENTRY_POINT_GROUP}` entry point registered"


def test_entry_point_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _ok_bundle()
    monkeypatch.setattr(lane_loader, "_lane_entry_points", lambda: [_FakeEntryPoint("mine", mine)])
    assert lane_loader.load_lane_bundle() is mine


def test_falls_back_to_the_default_when_nothing_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source checkout with no distribution metadata still finds the shipped lanes."""
    monkeypatch.setattr(lane_loader, "_lane_entry_points", list)
    bundle = lane_loader.load_lane_bundle()
    assert bundle.__name__ == lane_loader.DEFAULT_LANE_BUNDLE


def test_explicit_spec_overrides_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        lane_loader, "_lane_entry_points", lambda: [_FakeEntryPoint("mine", _ok_bundle())]
    )
    bundle = lane_loader.load_lane_bundle(lane_loader.DEFAULT_LANE_BUNDLE)
    assert bundle.__name__ == lane_loader.DEFAULT_LANE_BUNDLE


# --- fail loudly ------------------------------------------------------------------------

def test_drifted_bundle_names_its_missing_members(monkeypatch: pytest.MonkeyPatch) -> None:
    half = _bundle(build_registry=lambda **kw: Registry())  # no registry_runner
    monkeypatch.setattr(lane_loader, "_lane_entry_points", lambda: [_FakeEntryPoint("half", half)])
    with pytest.raises(SystemExit) as exc:
        lane_loader.load_lane_bundle()
    assert "registry_runner" in str(exc.value)
    assert "build_registry" not in str(exc.value), "should only name what is MISSING"


def test_non_module_entry_point_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `pkg.mod:attr` value can't expose the two callables — say so at load."""
    monkeypatch.setattr(
        lane_loader, "_lane_entry_points", lambda: [_FakeEntryPoint("attr", object())]
    )
    with pytest.raises(SystemExit, match="must point at a MODULE"):
        lane_loader.load_lane_bundle()


def test_unimportable_explicit_spec_is_rejected() -> None:
    with pytest.raises(SystemExit, match="not importable"):
        lane_loader.load_lane_bundle("no_such_lane_bundle_pkg")


def test_build_registry_rejects_a_non_registry_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard against an obscure AttributeError deep inside next_work's lane resolution."""
    liar = _bundle(build_registry=lambda **kw: {"not": "a registry"},
                   registry_runner=lambda registry, **kw: registry)
    monkeypatch.setattr(lane_loader, "_lane_entry_points", lambda: [_FakeEntryPoint("liar", liar)])
    with pytest.raises(SystemExit, match="did not return an"):
        lane_loader.build_registry()


# --- the wrappers the composition root actually calls ------------------------------------

def test_wrappers_delegate_to_the_resolved_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _build(**kwargs: object) -> Registry:
        seen["build_kwargs"] = kwargs
        return Registry()

    def _runner(registry: Registry, **kwargs: object) -> object:
        seen["runner_registry"] = registry
        seen["runner_kwargs"] = kwargs
        return "the-runner"

    monkeypatch.setattr(
        lane_loader,
        "_lane_entry_points",
        lambda: [_FakeEntryPoint("mine", _bundle(build_registry=_build, registry_runner=_runner))],
    )
    registry = lane_loader.build_registry(include_interactive=False, run_log_root="/tmp/x")
    assert seen["build_kwargs"] == {"include_interactive": False, "run_log_root": "/tmp/x"}
    assert lane_loader.registry_runner(registry, max_workers=3) == "the-runner"
    assert seen["runner_registry"] is registry
    assert seen["runner_kwargs"] == {"max_workers": 3}
