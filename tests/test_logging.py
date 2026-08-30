"""Durable observability: events.jsonl timeline + per-stage stages/ tree."""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage, StageStatus
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
    assert types.count("stage_dispatched") == 7  # #389 added PUBLISH behind REVIEW
    assert types.count("stage_recorded") == 7
    assert "task_completed" in types
    # run_finalized is immediately followed by its alerting `notification` row (#55).
    # Anchored on the finalize index rather than the tail: every finalized run now also
    # emits its retrospective receipt afterwards, so "last event" is no longer the assertion
    # this cares about — adjacency is.
    fin = types.index("run_finalized")
    assert types[fin + 1] == "notification"
    assert events[fin + 1]["kind"] == "run_finalized"
    assert "retrospective_emitted" in types
    # dispatched precedes recorded for each stage
    assert types.index("stage_dispatched") < types.index("stage_recorded")


def test_malformed_pr_number_now_fails_the_publish_stage(tmp_path, project) -> None:
    """#351 supersedes #201's warning for this case: a DELIVER that cannot name a real PR
    is vetoed to a FAILURE, which is strictly louder than a warning event on a green stage.

    #201's pr_field_dropped remains as defense in depth, for a value that passes the gate
    but still fails assignment — exercised directly at the fold in test_state_machine.
    """
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.PUBLISH:
            eng.record("r1", make_result(
                w,
                structured_output={"pr_number": "", "pr_url": "https://example.test/pr/9"},
            ))
            break
        eng.record("r1", make_result(w))

    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.PUBLISH].status is StageStatus.FAILED
    assert "no pull request was opened" in (task.last_error or "")
    assert task.pr_url is None, "a vetoed publish must not fold its outputs"

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert not [e for e in events if e["type"] == "task_completed"]


def test_context_value_truncated_event_on_oversized_scope_plan(tmp_path, project) -> None:
    """#289: a context value the fold had to cap emits a warning-grade
    context_value_truncated event naming the field, the part and how much was dropped —
    previously the only trace was a '… [truncated]' marker inside the next prompt, so a
    downstream stage working from a degraded plan was invisible in the timeline."""
    from orchestrator.state_machine import _MAX_PLAN_ITEM_STR

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.SCOPE:
            eng.record("r1", make_result(w, structured_output={
                "feasible": True,
                "plan": ["in-budget subtask", "z" * (_MAX_PLAN_ITEM_STR + 250)],
            }))
        else:
            eng.record("r1", make_result(w))

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    truncated = [e for e in events if e["type"] == "context_value_truncated"]
    assert len(truncated) == 1, "exactly one truncation event, for the oversized subtask"
    ev = truncated[0]
    assert ev["run_id"] == "r1" and ev["task_id"] == "t1"
    assert ev["stage"] == Stage.SCOPE.value
    assert ev["field"] == "plan"
    assert ev["part"] == "item[1]"
    assert ev["dropped_chars"] == 250


def test_in_budget_scope_plan_emits_no_truncation_event(tmp_path, project) -> None:
    """#289: the common case is SILENT because nothing was dropped — a plan of >500-char
    prose subtasks (the shape that used to be cut) folds whole and emits no event."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    plan = ["s" * 900, "t" * 1500, "u" * 800]
    while (w := eng.next_work("r1", "t1")) is not None:
        output = {"feasible": True, "plan": plan} if w.stage is Stage.SCOPE else None
        eng.record("r1", make_result(w, structured_output=output))

    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert not [e for e in events if e["type"] == "context_value_truncated"]
    assert not [e for e in events if e["type"] == "context_key_evicted"]
    assert eng.store.load_task("r1", "t1").context["plan"] == plan


def test_context_key_evicted_event_when_the_ceiling_binds(tmp_path, project) -> None:
    """#289: the whole-context ceiling still binds — and shedding a key is now reported
    rather than silently shrinking every later prompt."""
    from orchestrator.state_machine import _MAX_CONTEXT_BYTES, _context_bytes

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    huge_plan = ["p" * 3990 for _ in range(6)]  # ~24KB > the 16KB ceiling
    while (w := eng.next_work("r1", "t1")) is not None:
        output = {"feasible": True, "plan": huge_plan} if w.stage is Stage.SCOPE else None
        eng.record("r1", make_result(w, structured_output=output))

    task = eng.store.load_task("r1", "t1")
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    evicted = [e for e in events if e["type"] == "context_key_evicted"]
    assert [e["field"] for e in evicted] == ["plan"]
    assert evicted[0]["stage"] == Stage.SCOPE.value
    assert evicted[0]["bytes"] > _MAX_CONTEXT_BYTES


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
        "07-publish.json",  # #389: the PR is opened after review
    ]
    # each captures the durable StageResult payload (structured_output + lane + cost)
    deliver = json.loads((stage_dir / "05-deliver.json").read_text())
    assert deliver["structured_output"]["pushed_head_sha"]
    assert deliver["lane_used"]["execution_mode"] == "interactive"
    assert deliver["status"] == "success" and "cost_usd" in deliver
    publish = json.loads((stage_dir / "07-publish.json").read_text())
    assert publish["structured_output"]["pr_url"].endswith("/1234")
