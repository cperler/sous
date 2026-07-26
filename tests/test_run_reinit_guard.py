"""Re-initializing an existing run id must be refused, not silently destructive (#280).

`Engine.create_run` used to `save_run` unconditionally, so reusing a run id — an easy
operator typo — replaced the run document in place: the new run came back with zero task
refs while the previous run's task documents stayed on disk as orphans, and its dependency
graph, state and run-level settings were gone.

These tests pin the guard (`RunExistsError`, checked under the run write lock), the CLI's
loud no-write refusal, and the ONE explicit create-or-reuse entry point that queue
ingestion is allowed to use.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError, RunExistsError, StatusStoreError
from orchestrator.schemas.enums import ExecutionLane
from orchestrator.status_store import StatusStore


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _snapshot(root) -> dict[str, bytes]:
    """Every file under the store root, by relative path → bytes."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- the orphan regression -------------------------------------------------------

def test_recreating_a_run_id_is_refused_and_orphans_nothing(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=5.0, review_workflow=True)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2", depends_on=["t1"])

    before_run = eng.store._run_path("r1").read_bytes()
    before_task = eng.store._task_path("r1", "t1").read_bytes()

    with pytest.raises(RunExistsError) as excinfo:
        eng.create_run("r1", ExecutionLane.LITE)
    assert "r1" in str(excinfo.value)

    # Nothing was replaced: the run doc is byte-identical, its task refs and DAG edges
    # still point at the task docs, and its state/settings survive.
    assert eng.store._run_path("r1").read_bytes() == before_run
    assert eng.store._task_path("r1", "t1").read_bytes() == before_task
    run = eng.store.load_run("r1")
    assert [ref.task_id for ref in run.task_refs] == ["t1", "t2"]
    assert run.lane is ExecutionLane.FULL
    assert run.budget_usd == 5.0
    assert run.review_workflow is True
    # ...and no task doc is left orphaned (on disk but unreferenced by the run).
    for task_id in ("t1", "t2"):
        assert eng.store._task_path("r1", task_id).exists()
    assert eng.store.load_task("r1", "t2").depends_on == ["t1"]


def test_recreate_refusal_writes_nothing_at_all(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    before = _snapshot(tmp_path)
    with pytest.raises(RunExistsError):
        eng.create_run("r1")
    after = {k: v for k, v in _snapshot(tmp_path).items() if not k.endswith(".lock")}
    assert after == {k: v for k, v in before.items() if not k.endswith(".lock")}


def test_corrupt_run_doc_is_not_overwritten_by_a_recreate(tmp_path, project) -> None:
    # A doc that exists but does not parse must still block creation — the guard is a
    # path-existence check under the lock, so it never depends on reading the doc (#112).
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.store._run_path("r1").write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(RunExistsError):
        eng.create_run("r1")
    assert eng.store._run_path("r1").read_text(encoding="utf-8") == "{ not valid json"


# --- CLI: exits non-zero, writes nothing ------------------------------------------

def test_cli_init_run_on_existing_id_exits_nonzero_and_mutates_no_file(
    tmp_path, capsys
) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]

    assert main([*base, "init-run", "--lane", "full"]) == 0
    assert main([*base, "add-task", "--task", "#42"]) == 0
    capsys.readouterr()

    before = _snapshot(tmp_path)
    rc = main([*base, "init-run", "--lane", "lite"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "run1" in payload["error"]

    after = {k: v for k, v in _snapshot(tmp_path).items() if not k.endswith(".lock")}
    assert after == {k: v for k, v in before.items() if not k.endswith(".lock")}
    # in particular: no events line, no run-doc rewrite, task ref intact
    store = StatusStore(tmp_path)
    assert [ref.task_id for ref in store.load_run("run1").task_refs] == ["#42"]


# --- the explicit create-or-reuse API ---------------------------------------------

def test_create_or_reuse_run_is_idempotent(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    run, created = eng.create_or_reuse_run("r1", ExecutionLane.FULL)
    assert created is True
    eng.add_task("r1", "t1")

    again, created_again = eng.create_or_reuse_run("r1", ExecutionLane.FULL)
    assert created_again is False
    assert again.run_id == run.run_id
    # the existing run comes back whole — reuse never resets its task refs
    assert [ref.task_id for ref in again.task_refs] == ["t1"]
    assert [ref.task_id for ref in eng.store.load_run("r1").task_refs] == ["t1"]


def test_create_or_reuse_run_rejects_mismatched_immutable_settings(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_or_reuse_run("r1", ExecutionLane.FULL, budget_usd=5.0)

    with pytest.raises(ContractError) as excinfo:
        eng.create_or_reuse_run("r1", ExecutionLane.LITE, budget_usd=9.0)
    msg = str(excinfo.value)
    assert "lane" in msg and "budget_usd" in msg
    # the persisted run is untouched by the rejected reuse
    run = eng.store.load_run("r1")
    assert run.lane is ExecutionLane.FULL and run.budget_usd == 5.0


def test_create_or_reuse_run_propagates_a_corrupt_run_doc(tmp_path, project) -> None:
    # Corrupt is NOT absent (#112): reuse must raise rather than recreate over it.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.store._run_path("r1").write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(StatusStoreError) as excinfo:
        eng.create_or_reuse_run("r1")
    assert not isinstance(excinfo.value, RunExistsError)
    assert eng.store._run_path("r1").read_text(encoding="utf-8") == "{ not valid json"


# --- the store primitives ---------------------------------------------------------

def test_run_exists_probe_keeps_the_narrow_not_found_semantics(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    assert eng.store.run_exists("never-created") is False
    eng.create_run("r1")
    assert eng.store.run_exists("r1") is True
    eng.store._run_path("r1").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(StatusStoreError):
        eng.store.run_exists("r1")
