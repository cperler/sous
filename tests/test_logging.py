"""Durable observability: events.jsonl timeline + per-stage stages/ tree."""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
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
    # run_finalized is immediately followed by its alerting `notification` row (#55),
    # so it's the penultimate event and the finalize notification is the last.
    assert types[-2] == "run_finalized"
    assert types[-1] == "notification"
    assert events[-1]["kind"] == "run_finalized"
    # dispatched precedes recorded for each stage
    assert types.index("stage_dispatched") < types.index("stage_recorded")


def test_pr_field_dropped_event_on_malformed_pr_value(tmp_path, project) -> None:
    """#201: a malformed model pr_number dropped at the fold emits a warning-grade
    pr_field_dropped audit event, so the drop is no longer silent. The valid pr_url
    sibling still folds and contributes no event."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.DELIVER:
            eng.record("r1", make_result(
                w,
                structured_output={"pr_number": "", "pr_url": "https://example.test/pr/9"},
            ))
        else:
            eng.record("r1", make_result(w))

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    dropped = [e for e in events if e["type"] == "pr_field_dropped"]
    assert len(dropped) == 1, "exactly one drop event for the malformed pr_number"
    ev = dropped[0]
    assert ev["run_id"] == "r1"
    assert ev["task_id"] == "t1"
    assert ev["stage"] == Stage.DELIVER.value
    assert ev["field"] == "pr_number"
    assert ev["value"] == "''"  # bounded repr keeps the empty-string type visible
    assert ev["reason"]  # a non-empty reason string
    # The valid sibling folded onto the task (no event for it).
    assert eng.store.load_task("r1", "t1").pr_url == "https://example.test/pr/9"


def test_finalize_sweeps_lock_sentinels_but_keeps_audit_trail(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    # Mid-run: a live run leaves flock sentinels next to the files being written.
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    assert list(tmp_path.rglob("*.lock")), "expected lock sentinels while the run is active"

    # Drive to completion → finalize sweeps them.
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert eng.status("r1")["run_state"] == "completed"
    assert not list(tmp_path.rglob("*.lock")), "finalize should sweep lock sentinels"

    # A poll of the finished run recreates a cost-artifact lock, then sweeps it too.
    eng.status("r1")
    assert not list(tmp_path.rglob("*.lock"))

    # The durable audit trail is never swept.
    assert (tmp_path / "events.jsonl").exists()
    assert (tmp_path / "stage-costs.jsonl").exists()


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
