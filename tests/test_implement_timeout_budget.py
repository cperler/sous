"""#615 — IMPLEMENT's 30-minute limit can be raised for a large task.

prediction#1291 finished all 15 findings but timed out twice running its final suite, so the
task failed with no PR. The limit was fixed at 1800s. Now a task carries a ``long`` budget
(pinned at add-task / in a batch plan, or chosen by SCOPE), and a project may declare its own
IMPLEMENT default.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.batch_plan import BatchPlanError, apply_plan, validate_plan
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.decomposition import parse_subtasks
from orchestrator.engine import Engine, implement_timeout_s
from orchestrator.schemas.enums import ExecutionLane, ImplementationBudget, Stage
from orchestrator.schemas.status import Task
from orchestrator.stages import STAGE_SPECS, render_prompt
from orchestrator.state_machine import _absorb_scope_budget
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

B = ImplementationBudget
SPEC = STAGE_SPECS[Stage.IMPLEMENT].timeout_s


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _events(eng: Engine, etype: str, run: str = "r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == etype]


def _work_until(eng: Engine, stage: Stage, *, scope_output: dict | None = None):
    """Drive ``t1`` forward to ``stage``, answering SCOPE with ``scope_output``."""
    while (work := eng.next_work("r1", "t1")).stage is not stage:
        out = scope_output if work.stage is Stage.SCOPE else None
        eng.record("r1", make_result(work, structured_output=out))
    return work


def _scope(**extra) -> dict:
    return {"feasible": True, "plan": ["do it"], **extra}


# --- the limit each budget selects ----------------------------------------------------------


@pytest.mark.parametrize(
    ("budget", "project_default", "expected"),
    [
        (None, None, SPEC),
        (B.STANDARD, None, SPEC),
        (B.SHORT, None, 900),
        (B.LONG, None, 3600),
        (None, 2700, 2700),
        (B.STANDARD, 2700, 2700),
        (B.SHORT, 2700, 900),
        (B.LONG, 2700, 3600),
        (B.LONG, 5400, 5400),  # asking for more time never shortens the limit
    ],
)
def test_budget_precedence(budget, project_default, expected) -> None:
    assert SPEC == 1800
    assert implement_timeout_s(
        budget, spec_timeout_s=SPEC, project_default_s=project_default
    ) == expected


# --- add-task / batch-plan pins ---------------------------------------------------------------


def test_add_task_cli_pin_persists_and_only_moves_implement(tmp_path, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    capsys.readouterr()
    assert main([*base, "add-task", "--task", "#42", "--implementation-budget", "long"]) == 0
    assert json.loads(capsys.readouterr().out)["implementation_budget"] == "long"
    # A fresh engine (as every CLI subcommand builds) reads the same limit back off the doc.
    store = StatusStore(tmp_path)
    assert store.load_task("run1", "#42").implementation_budget is B.LONG
    eng = Engine(store, CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject())
    timeouts: dict[Stage, int | None] = {}
    while (work := eng.next_work("run1", "#42")) is not None and work.stage not in timeouts:
        timeouts[work.stage] = work.timeout_s
        eng.record("run1", make_result(work))
        if work.stage is Stage.IMPLEMENT:
            break
    assert timeouts[Stage.IMPLEMENT] == 3600
    assert timeouts[Stage.SCOPE] == STAGE_SPECS[Stage.SCOPE].timeout_s


def test_add_task_cli_rejects_unknown_budget(tmp_path) -> None:
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    assert main([*base, "init-run", "--lane", "full"]) == 0
    with pytest.raises(SystemExit):
        main([*base, "add-task", "--task", "#42", "--implementation-budget", "huge"])


def _plan(**task) -> dict:
    return {"tasks": [{"task_id": "#1", "pipeline": "full", **task}]}


def test_batch_plan_carries_the_budget(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    preview = apply_plan(eng, "r1", _plan(implementation_budget="long"), known_ids=["#1"],
                         dry_run=True)
    assert preview["added"][0]["implementation_budget"] == "long"
    apply_plan(eng, "r1", _plan(implementation_budget="long"), known_ids=["#1"])
    assert eng.store.load_task("r1", "#1").implementation_budget is B.LONG


def test_batch_plan_schema_refuses_an_unknown_budget() -> None:
    validate_plan(_plan(implementation_budget=None), known_ids=["#1"])  # no raise
    with pytest.raises(BatchPlanError, match="implementation_budget"):
        validate_plan(_plan(implementation_budget="huge"), known_ids=["#1"])


# --- SCOPE sizing -----------------------------------------------------------------------------


def test_scope_long_raises_an_unpinned_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = _work_until(eng, Stage.IMPLEMENT, scope_output=_scope(implementation_budget="long"))
    assert work.timeout_s == 3600
    assert eng.store.load_task("r1", "t1").implementation_budget is B.LONG
    (ev,) = _events(eng, "implementation_budget_set")
    assert ev["applied"] is True and ev["requested"] == "long" and "level" not in ev


def test_scope_cannot_override_a_pin(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", implementation_budget="short")
    work = _work_until(eng, Stage.IMPLEMENT, scope_output=_scope(implementation_budget="long"))
    assert work.timeout_s == 900
    (ev,) = _events(eng, "implementation_budget_set")
    assert ev["applied"] is False and ev["reason"] == "pinned" and ev["level"] == "warning"
    assert ev["current"] == "short"


@pytest.mark.parametrize("value", ["standard", None])
def test_scope_standard_or_absent_changes_nothing(tmp_path, project, value) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    out = _scope() if value is None else _scope(implementation_budget=value)
    work = _work_until(eng, Stage.IMPLEMENT, scope_output=out)
    assert work.timeout_s == SPEC
    assert eng.store.load_task("r1", "t1").implementation_budget is None
    assert _events(eng, "implementation_budget_set") == []


def test_scope_short_is_not_a_scope_choice() -> None:
    """``short`` is a decomposition-child control; SCOPE shortening its own task is evented
    as unknown rather than applied (the fold is tested directly: the schema forbids it)."""
    task = Task.model_validate({
        "task_id": "t1", "run_id": "r1", "created_at": "t", "updated_at": "t",
        "pipeline": ["intake", "implement"],
    })
    (notice,) = _absorb_scope_budget(task, "short")
    assert notice["applied"] is False and notice["reason"] == "unknown_budget"
    assert task.implementation_budget is None


def test_decomposition_child_accepts_long() -> None:
    (child,) = parse_subtasks(
        {"subtasks": [{"id": "big", "description": "d", "implementation_budget": "long"}]}
    )
    assert child.implementation_budget is B.LONG


def test_scope_prompt_offers_the_budget_with_intact_spacing() -> None:
    prompt = render_prompt(Stage.SCOPE, task_id="t1", title="t", body="b")
    assert (
        "(\"not on a latency-sensitive path\" is a complete note).\n"
        "If the task is large but should still ship as one change, set "
        "`implementation_budget` to `long` to give IMPLEMENT 60 minutes instead of 30 "
        "(#615); omit it otherwise.\nReturn: feasible," in prompt
    )
    assert "optional implementation_budget (standard|long), and optional subtasks" in prompt
    assert "implementation_budget: standard|short|long, depends_on" in prompt


# --- the project default ----------------------------------------------------------------------


def test_project_default_applies_and_long_stays_above_it(tmp_path, project) -> None:
    project.implement_timeout_s = 2700
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.IMPLEMENT).timeout_s == 2700
    assert _events(eng, "implement_timeout_rejected") == []


@pytest.mark.parametrize("bad", [0, -5, "3600", 1.5, True])
def test_unusable_project_default_is_evented_and_ignored(tmp_path, project, bad) -> None:
    project.implement_timeout_s = bad
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _work_until(eng, Stage.IMPLEMENT).timeout_s == SPEC
    (ev,) = _events(eng, "implement_timeout_rejected")
    assert ev["level"] == "warning" and ev["value"] == repr(bad)
