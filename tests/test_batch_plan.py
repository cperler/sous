"""Batch plan producer (#57): schema + DAG validation, topo order, and apply-to-run.

Covers the deterministic half of the auto-analysis producer over an ALREADY-FILED batch
of issues — ``orchestrator/batch_plan.py``, the ``list_tasks`` + Depends-on parsing on the
GitHub source, and the ``batch-plan`` CLI. The analysis itself is the skill's job (model
work); here we exercise only the code around it.
"""

from __future__ import annotations

import json

import pytest

from adapters.project.github_issues import GitHubIssuesSource, _parse_depends_on
from orchestrator.batch_plan import (
    BatchPlanError,
    apply_plan,
    load_plan,
    topological_order,
    validate_plan,
)
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Stage
from orchestrator.status_store import StatusStore


def _plan(**over) -> dict:
    base = {
        "tasks": [
            {"task_id": "#1", "pipeline": "full", "rationale": "root"},
            {"task_id": "#2", "depends_on": ["#1"], "pipeline": "lite",
             "provider_tag": "codex", "rationale": "needs #1's schema"},
            {"task_id": "#3", "depends_on": ["#1", "#2"], "pipeline": "micro",
             "deterministic_stages": ["test", "deliver"], "rationale": "docs only"},
        ]
    }
    base.update(over)
    return base


def _engine(tmp_path, project) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project)


# --- schema validation -------------------------------------------------------------

def test_validate_accepts_a_well_formed_plan() -> None:
    validate_plan(_plan(), known_ids=["#1", "#2", "#3"])  # no raise


@pytest.mark.parametrize("mutate, needle", [
    (lambda p: p.update(tasks=[]), "non-empty"),
    (lambda p: p["tasks"][0].pop("task_id"), "task_id"),
    (lambda p: p["tasks"][0].update(pipeline="mega"), "mega"),
    (lambda p: p["tasks"][0].update(extra="nope"), "Additional properties"),
    (lambda p: p["tasks"][2].update(deterministic_stages=["nonsense"]), "nonsense"),
])
def test_schema_failures_are_clear(mutate, needle) -> None:
    plan = _plan()
    mutate(plan)
    with pytest.raises(BatchPlanError) as exc:
        validate_plan(plan)
    assert "schema validation" in str(exc.value)
    assert needle in str(exc.value)


# --- DAG validation (reuses spec_intake semantics) ---------------------------------

def test_duplicate_task_id_is_reported() -> None:
    plan = _plan()
    plan["tasks"][2]["task_id"] = "#1"
    with pytest.raises(BatchPlanError, match="duplicate task id"):
        validate_plan(plan)


def test_self_dependency_is_reported() -> None:
    plan = _plan()
    plan["tasks"][0]["depends_on"] = ["#1"]
    with pytest.raises(BatchPlanError, match="depends on itself"):
        validate_plan(plan)


def test_cycle_is_reported() -> None:
    plan = {"tasks": [
        {"task_id": "#a", "depends_on": ["#b"]},
        {"task_id": "#b", "depends_on": ["#a"]},
    ]}
    with pytest.raises(BatchPlanError, match="cycle"):
        validate_plan(plan)


def test_edge_to_unknown_id_is_rejected_when_known_ids_given() -> None:
    plan = {"tasks": [{"task_id": "#1", "depends_on": ["#999"]}]}
    with pytest.raises(BatchPlanError, match="neither a task in the plan nor a known"):
        validate_plan(plan, known_ids=["#1"])


def test_edge_to_already_terminal_known_id_is_accepted() -> None:
    # #99 is not in the plan but is a real (already-terminal) issue — a valid external edge.
    plan = {"tasks": [{"task_id": "#1", "depends_on": ["#99"]}]}
    validate_plan(plan, known_ids=["#1", "#99"])  # no raise


def test_external_refs_unverified_when_known_ids_is_none() -> None:
    # Offline validate (no task source) can't verify external ids — it permits them but
    # still enforces internal structure.
    plan = {"tasks": [{"task_id": "#1", "depends_on": ["#99"]}]}
    validate_plan(plan, known_ids=None)  # no raise


# --- topological order -------------------------------------------------------------

def test_load_plan_reports_missing_file(tmp_path) -> None:
    with pytest.raises(BatchPlanError, match="not found"):
        load_plan(tmp_path / "nope.json")


def test_load_plan_reports_bad_json(tmp_path) -> None:
    p = tmp_path / "plan.json"
    p.write_text("{not json")
    with pytest.raises(BatchPlanError, match="not valid JSON"):
        load_plan(p)


def test_load_plan_reads_and_schema_validates(tmp_path) -> None:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(_plan()))
    assert [t["task_id"] for t in load_plan(p)["tasks"]] == ["#1", "#2", "#3"]


def test_topological_order_places_deps_first() -> None:
    order = topological_order(_plan())
    assert order.index("#1") < order.index("#2") < order.index("#3")


def test_topological_order_ignores_external_edges() -> None:
    # An edge to an external terminal id doesn't affect ordering among plan tasks.
    plan = {"tasks": [
        {"task_id": "#2", "depends_on": ["#99"]},
        {"task_id": "#1"},
    ]}
    assert topological_order(plan) == ["#2", "#1"]  # stable by input order


# --- apply -------------------------------------------------------------------------

def test_apply_adds_tasks_in_topo_order_with_all_fields(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    result = apply_plan(eng, "r1", _plan(), known_ids=["#1", "#2", "#3"])

    assert result["order"] == ["#1", "#2", "#3"]
    assert [e["task_id"] for e in result["added"]] == ["#1", "#2", "#3"]

    t1 = eng.store.load_task("r1", "#1")
    t2 = eng.store.load_task("r1", "#2")
    t3 = eng.store.load_task("r1", "#3")

    # pipeline hint -> lane preset.
    assert t1.execution_lane is ExecutionLane.FULL
    assert t2.execution_lane is ExecutionLane.LITE
    assert t3.execution_lane is ExecutionLane.MICRO

    # depends_on edges applied.
    assert t2.depends_on == ["#1"]
    assert t3.depends_on == ["#1", "#2"]

    # provider_tag + deterministic_stages threaded through.
    assert t2.provider_tag == "codex"
    assert t3.deterministic_stages == (Stage.TEST, Stage.DELIVER)


def test_apply_drops_external_deps_from_the_scheduling_graph(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    plan = {"tasks": [{"task_id": "#5", "depends_on": ["#99"], "rationale": "after #99"}]}
    result = apply_plan(eng, "r1", plan, known_ids=["#5", "#99"])

    # The external already-terminal edge is recorded but NOT handed to add_task (the
    # engine's Dag would reject an edge to a non-task).
    assert result["dropped_external_deps"] == {"#5": ["#99"]}
    task = eng.store.load_task("r1", "#5")
    assert task.depends_on == []
    run = eng.store.load_run("r1")
    assert run.dependency_graph["#5"] == []


def test_dry_run_adds_nothing_and_emits_no_event(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    result = apply_plan(eng, "r1", _plan(), known_ids=["#1", "#2", "#3"], dry_run=True)

    assert result["dry_run"] is True
    assert result["order"] == ["#1", "#2", "#3"]
    run = eng.store.load_run("r1")
    assert run.task_refs == []
    assert [e for e in eng.store.read_events("r1") if e["type"] == "batch_planned"] == []


def test_apply_emits_a_batch_planned_event(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    apply_plan(eng, "r1", _plan(), known_ids=["#1", "#2", "#3"])
    events = [e for e in eng.store.read_events("r1") if e["type"] == "batch_planned"]
    assert len(events) == 1
    assert events[0]["count"] == 3
    assert events[0]["order"] == ["#1", "#2", "#3"]


# --- Depends-on parsing + list_tasks -----------------------------------------------

@pytest.mark.parametrize("body, expected", [
    ("Scope: x\n\nDepends-on: #12, #34", ["#12", "#34"]),
    ("depends-on: #7", ["#7"]),  # case-insensitive
    ("Depends on: #1 and also #2", ["#1", "#2"]),
    ("no deps here", []),
    ("Depends-on: #5, #5, #6", ["#5", "#6"]),  # de-duplicated
])
def test_parse_depends_on(body, expected) -> None:
    assert _parse_depends_on(body) == expected


def test_list_tasks_builds_query_and_parses_deps() -> None:
    calls: list[list[str]] = []

    def fake_gh(argv: list[str]) -> str:
        calls.append(argv)
        return json.dumps([
            {"number": 1, "title": "Schema", "body": "define it", "labels": [{"name": "backend"}]},
            {"number": 2, "title": "Use schema", "body": "wire it\n\nDepends-on: #1",
             "labels": [{"name": "backend"}]},
        ])

    src = GitHubIssuesSource("owner/repo", runner=fake_gh)
    tasks = src.list_tasks(label="ready", limit=25)

    # Query shape: open issues, label + limit filters, JSON fields.
    argv = calls[0]
    assert argv[:3] == ["gh", "issue", "list"]
    assert "--state" in argv and argv[argv.index("--state") + 1] == "open"
    assert "--label" in argv and argv[argv.index("--label") + 1] == "ready"
    assert "--limit" in argv and argv[argv.index("--limit") + 1] == "25"

    assert [t.task_id for t in tasks] == ["#1", "#2"]
    assert tasks[0].depends_on == []
    assert tasks[1].depends_on == ["#1"]  # pre-populated from the Depends-on line
    assert tasks[1].labels == ["backend"]


def test_list_tasks_omits_label_filter_when_none() -> None:
    src = GitHubIssuesSource("owner/repo", runner=lambda argv: "[]")
    assert src.list_tasks() == []


# --- CLI ---------------------------------------------------------------------------

def _write_plan(tmp_path, plan: dict) -> str:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan))
    return str(p)


def test_cli_validate_ok(tmp_path, capsys) -> None:
    path = _write_plan(tmp_path, _plan())
    rc = main(["batch-plan", "validate", path])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["order"] == ["#1", "#2", "#3"]


def test_cli_validate_rejects_a_cycle(tmp_path, capsys) -> None:
    path = _write_plan(tmp_path, {"tasks": [
        {"task_id": "#a", "depends_on": ["#b"]},
        {"task_id": "#b", "depends_on": ["#a"]},
    ]})
    rc = main(["batch-plan", "validate", path])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "cycle" in out["error"]


def test_cli_apply_smoke(tmp_path, capsys) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run"]) == 0
    capsys.readouterr()
    path = _write_plan(tmp_path, _plan())
    rc = main([*base, "batch-plan", "apply", path])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["order"] == ["#1", "#2", "#3"]
    assert [e["task_id"] for e in out["added"]] == ["#1", "#2", "#3"]


def test_cli_apply_dry_run_adds_nothing(tmp_path, capsys) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run"]) == 0
    capsys.readouterr()
    path = _write_plan(tmp_path, _plan())
    rc = main([*base, "batch-plan", "apply", path, "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    # Nothing was actually registered on the run.
    store = StatusStore(tmp_path)
    assert store.load_run("run1").task_refs == []


def test_cli_candidates_smoke(tmp_path, capsys, monkeypatch) -> None:
    from adapters.project.base import TaskSpec
    from tests.conftest import FakeProject

    project = FakeProject()
    project.task_source.candidates = [
        TaskSpec(task_id="#1", title="A", body="do a", labels=["ready"]),
        TaskSpec(task_id="#2", title="B", body="do b", depends_on=["#1"], labels=["ready"]),
    ]
    monkeypatch.setattr("orchestrator.cli.load_project", lambda _arg: project)

    rc = main(["--project", "ignored", "batch-plan", "candidates", "--label", "ready"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["candidates"][1]["depends_on"] == ["#1"]
