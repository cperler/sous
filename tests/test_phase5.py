"""Phase 5 — generality: a second project (self-host) + the bootstrap scaffold,
both driving the UNCHANGED engine. Done: second project completes a task with
changes confined to its adapter; the scaffold produces a working skeleton."""

from __future__ import annotations

import importlib
import json
import sys

from adapters.project.base import ProjectConfig
from adapters.project.selfhost import get_config as selfhost_config
from adapters.project.selfhost.config import SelfHostConfig
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.scaffold import scaffold_adapter
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _tasks_file(tmp_path, mapping: dict) -> str:
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(mapping))
    return str(p)


def _drive(eng, run, task):
    while (w := eng.next_work(run, task)) is not None:
        eng.record(run, make_result(w))


# --- the self-host adapter satisfies the protocol & is genuinely different ---
def test_selfhost_satisfies_protocol() -> None:
    cfg = selfhost_config()
    assert isinstance(cfg, ProjectConfig)
    assert cfg.install_cmd() == ["uv", "sync"]
    assert cfg.fresh_install_paths() == [".venv"]
    assert [name for name, _ in cfg.worktree_origin_probes()] == [
        "pytest shebang interpreter", "orchestrator module",
    ]
    assert cfg.test_e2e_cmd() == ["true"]  # this repo has no E2E layer
    assert cfg.typecheck_cmd() == ["uv", "run", "ruff", "check", "."]


def test_selfhost_defaults_to_own_github_issue_log(monkeypatch) -> None:
    from adapters.project.github_issues import GitHubIssuesSource

    monkeypatch.delenv("SELFHOST_TASKS", raising=False)
    cfg = selfhost_config()
    assert isinstance(cfg.task_source, GitHubIssuesSource)
    assert cfg.task_source.repo == "cperler/sous"


def test_selfhost_env_selects_local_file_mode(tmp_path, monkeypatch) -> None:
    tasks = _tasks_file(tmp_path, {"T1": {"title": "t"}})
    monkeypatch.setenv("SELFHOST_TASKS", tasks)
    cfg = selfhost_config()
    assert cfg.task_source.resolve("T1").title == "t"


def test_selfhost_classifier_taxonomy() -> None:
    cfg = selfhost_config()
    fails = cfg.classifier.classify("FAILED tests/test_x.py::test_y\nsrc/a.py:1:1: E501 line too long\n")
    kinds = {f.test: f.kind.value for f in fails}
    assert kinds["tests/test_x.py::test_y"] == "unit"
    assert "src/a.py:1:1" in kinds  # ruff lint surfaced too


# --- a task completes end-to-end through the UNCHANGED engine (generality) ---
def test_second_project_completes_a_task_engine_untouched(tmp_path) -> None:
    tasks = _tasks_file(tmp_path, {"T1": {"title": "Tidy a module", "body": "do it"}})
    cfg = SelfHostConfig(tasks_path=tasks)
    eng = Engine(StatusStore(tmp_path / "run"), CostLedger(tmp_path / "run" / "c.jsonl"), cfg)
    eng.create_run("r1")
    eng.add_task("r1", "T1")
    _drive(eng, "r1", "T1")

    status = eng.status("r1")
    assert status["tasks"]["T1"]["state"] == "completed"
    assert status["lane_audit"]["clean"] is True
    # the local-file task source recorded completion (not a GitHub comment)
    assert (tmp_path / "completed.log").read_text().startswith("T1\t")


def test_local_task_source_resolves_dependencies(tmp_path) -> None:
    tasks = _tasks_file(tmp_path, {"A": {"title": "A"}, "B": {"title": "B", "depends_on": ["A"]}})
    cfg = SelfHostConfig(tasks_path=tasks)
    assert cfg.task_source.resolve("B").depends_on == ["A"]


# --- the bootstrap scaffold produces a WORKING adapter skeleton --------------
def test_scaffold_produces_working_adapter(tmp_path) -> None:
    pkg = scaffold_adapter("demo-svc", tmp_path)
    assert (pkg / "config.py").exists() and (pkg / "task_source.py").exists()

    sys.path.insert(0, str(tmp_path))
    try:
        mod = importlib.import_module("demo_svc")
        cfg = mod.get_config()
        assert isinstance(cfg, ProjectConfig)  # generated skeleton satisfies the contract

        # and it can actually drive a task through the unchanged engine
        tasks = _tasks_file(tmp_path, {"X": {"title": "demo"}})
        cfg = mod.SelfHostConfig if hasattr(mod, "SelfHostConfig") else mod.get_config().__class__
        run_cfg = cfg(tasks_path=tasks)
        eng = Engine(StatusStore(tmp_path / "r"), CostLedger(tmp_path / "r" / "c.jsonl"), run_cfg)
        eng.create_run("r1")
        eng.add_task("r1", "X")
        _drive(eng, "r1", "X")
        assert eng.status("r1")["tasks"]["X"]["state"] == "completed"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("demo_svc", None)
