"""Durable observability: events.jsonl timeline + per-stage stages/ tree."""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _drive(eng, run="r1", task="t1"):
    while (w := eng.next_work(run, task)) is not None:
        eng.record(run, make_result(w))


def test_events_jsonl_timeline(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    _drive(eng)

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    assert types.count("stage_dispatched") == 6
    assert types.count("stage_recorded") == 6
    assert "task_completed" in types
    assert types[-1] == "run_finalized"
    # dispatched precedes recorded for each stage
    assert types.index("stage_dispatched") < types.index("stage_recorded")


def test_per_stage_log_tree(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    _drive(eng)

    stage_dir = tmp_path / "stages" / "t1"
    files = sorted(p.name for p in stage_dir.glob("*.json"))
    assert files == [
        "01-intake.json",
        "02-scope.json",
        "03-implement.json",
        "04-test.json",
        "05-deliver.json",
        "06-review.json",
    ]
    # each captures the durable StageResult payload (structured_output + lane + cost)
    deliver = json.loads((stage_dir / "05-deliver.json").read_text())
    assert deliver["structured_output"]["pr_url"].endswith("/1234")
    assert deliver["lane_used"]["execution_mode"] == "interactive"
    assert deliver["status"] == "success" and "cost_usd" in deliver
