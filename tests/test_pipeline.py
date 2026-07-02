"""Per-task pipeline (2026-07-01 design pass §1): the Stage enum is a vocabulary,
Task.pipeline is the sequence, lanes are presets.

Schema v2: a v1 doc without `pipeline` derives it from execution_lane on load.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import LANE_STAGES, ExecutionLane, Stage, StageStatus, TaskState
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


# --- v1 -> v2 migration ------------------------------------------------------

def test_v1_doc_without_pipeline_derives_it_from_lane(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.LITE)
    eng.add_task("r1", "t1")
    # Rewrite the persisted doc as a v1 doc: no pipeline field, schema_version 1.
    path = tmp_path / "status-r1-t1.json"
    raw = json.loads(path.read_text())
    del raw["pipeline"]
    raw["schema_version"] = "1"
    path.write_text(json.dumps(raw))
    task = eng.store.load_task("r1", "t1")
    assert task.pipeline == LANE_STAGES[ExecutionLane.LITE]
    # ...and the doc round-trips as v2 (pipeline persisted on the next save).
    eng.store.save_task(task)
    assert "pipeline" in json.loads(path.read_text())


def test_constructed_task_without_pipeline_gets_lane_preset() -> None:
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x",
                execution_lane=ExecutionLane.MICRO)
    assert task.pipeline == LANE_STAGES[ExecutionLane.MICRO]


# --- validation ---------------------------------------------------------------

def test_empty_and_duplicate_pipelines_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Task(task_id="t", run_id="r", created_at="x", updated_at="x",
             pipeline=(Stage.IMPLEMENT, Stage.IMPLEMENT))
    with pytest.raises(ValueError):  # unknown stage name fails enum coercion
        Task(task_id="t", run_id="r", created_at="x", updated_at="x",
             pipeline=("decompose",))


def test_add_task_rejects_duplicate_stage_pipeline(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    with pytest.raises(ValueError):
        eng.add_task("r1", "t1", pipeline=[Stage.SCOPE, Stage.SCOPE])


# --- custom pipelines run end-to-end -------------------------------------------

def test_custom_two_stage_pipeline_runs_and_skips_the_rest(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", pipeline=[Stage.SCOPE, Stage.REVIEW])
    dispatched: list[Stage] = []
    while (w := eng.next_work("r1", "t1")) is not None:
        dispatched.append(w.stage)
        eng.record("r1", make_result(w))
    assert dispatched == [Stage.SCOPE, Stage.REVIEW]
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.COMPLETED
    for stage in (Stage.INTAKE, Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER):
        assert task.stages[stage].status is StageStatus.SKIPPED


def test_pipeline_order_is_the_callers_order_not_stage_order(tmp_path, project) -> None:
    # The vocabulary's display order is NOT a sequencing constraint (design pass §1):
    # a task may run review-style stages before implement-style ones.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW, Stage.IMPLEMENT])
    dispatched: list[Stage] = []
    while (w := eng.next_work("r1", "t1")) is not None:
        dispatched.append(w.stage)
        eng.record("r1", make_result(w))
    assert dispatched == [Stage.REVIEW, Stage.IMPLEMENT]


def test_lane_preset_resolves_to_pipeline_at_add(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    task = eng.add_task("r1", "t1", ExecutionLane.MICRO)
    assert task.pipeline == LANE_STAGES[ExecutionLane.MICRO]
    assert task.execution_lane is ExecutionLane.MICRO  # provenance retained


# --- resume over a custom pipeline ---------------------------------------------

def test_resume_lands_mid_custom_pipeline(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", pipeline=[Stage.INTAKE, Stage.IMPLEMENT, Stage.REVIEW])
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w))  # intake done
    # Simulate a crash mid-implement: dispatch, then "die" before recording.
    eng.next_work("r1", "t1")
    fresh = Engine(eng.store, eng.ledger, eng.project)  # new engine, same durable state
    assert fresh.resume("r1") == {"t1": "implement"}
