"""Dashboard snapshot fields (#525): the one structure both the console and web views read.

Pins the fields added for the redesign — per-task detail (title, links, current stage and
its age, model, per-stage progress and cost, dependencies and what a task waits on, stream
activity), the plain-words driver summary, attention items that carry their exact release
or resume commands, the per-run cost breakdown with unmetered counts (#331), readable
recent events, the header's project grouping — and the #498 fix: a degraded row keeps the
exception that caused it, and an adapter that will not resolve is no longer called
unreadable.

Runs are built the way ``test_dashboard.py`` builds them (Engine + FakeProject in a
``runs/<id>/`` store root), so the assembly reads genuine status/events/ledger files.
"""

from __future__ import annotations

import json
import os
import time

from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import (
    AdapterUnresolved,
    _driver_summary,
    dashboard_snapshot,
    event_sentence,
    render_dashboard,
)
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir
from tests.conftest import FakeProject, make_result

ISSUE_BASE = "https://github.com/x/y/issues/"


def _engine(run_root) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    project = FakeProject()
    # The optional duck-typed task-source hook the engine's `_issue_url` reads (#409).
    project.task_source.issue_url = lambda tid: f"{ISSUE_BASE}{tid}"  # type: ignore[attr-defined]
    return Engine(StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), project)


def _factory():
    return lambda run_root, project_ref=None: _engine(run_root)


def _snapshot(root, **kw):
    kw.setdefault("engine_factory", _factory())
    # A clock a little ahead of real time, so "seconds in the current stage" is positive
    # and deterministic enough to bound.
    kw.setdefault("clock", lambda: time.time() + 120)
    return dashboard_snapshot(root, **kw)


def _intake(eng, run_id, task_id, **kw) -> None:
    eng.add_task(run_id, task_id, **kw)
    eng.record(run_id, make_result(eng.next_work(run_id, task_id)))


def _write_stream(run_root, task_id, stage, *, age_s: float = 0.0) -> None:
    d = stages_dir(run_root, task_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stage}-attempt0.stream.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}) + "\n"
    )
    if age_s:
        then = time.time() - age_s
        os.utime(path, (then, then))


# --- fixtures -------------------------------------------------------------------------


def _mid_run(tmp_path):
    """One running task with a live stream, one blocked at the human gate, and one waiting
    on the unfinished running task."""
    rr = tmp_path / "mid"
    eng = _engine(rr)
    eng.create_run("mid")
    _intake(eng, "mid", "t1")
    assert eng.next_work("mid", "t1").stage is Stage.SCOPE  # t1 now RUNNING at scope
    _write_stream(rr, "t1", "scope")
    _intake(eng, "mid", "t2")
    eng.record("mid", make_result(eng.next_work("mid", "t2"), structured_output={
        "feasible": False, "blocked_reason": "needs an API that does not exist", "plan": []}))
    eng.add_task("mid", "t3", depends_on=["t1"])
    return rr


def _finished_run(tmp_path):
    rr = tmp_path / "done"
    eng = _engine(rr)
    eng.create_run("done")
    eng.add_task("done", "t1")
    while (w := eng.next_work("done", "t1")) is not None:
        eng.record("done", make_result(w))
    return rr


def _tasks(row) -> dict[str, dict]:
    return {t["task_id"]: t for t in row["tasks"]}


# --- mid-run fixture ------------------------------------------------------------------


def test_running_task_detail(tmp_path) -> None:
    _mid_run(tmp_path)
    row = _snapshot(tmp_path)["runs"][0]
    t1 = _tasks(row)["t1"]

    assert t1["title"] == "Fake t1"
    assert t1["issue_url"] == f"{ISSUE_BASE}t1"
    assert t1["pr_url"] is None
    assert t1["state"] == "running"
    assert t1["current_stage"] == "scope"
    # Seconds in the CURRENT stage, from its StageRecord.started_at against the clock.
    assert t1["stage_age_s"] is not None and 100 <= t1["stage_age_s"] <= 200
    assert t1["model"]  # the model the running stage was dispatched on
    # The stream grew just now → active, with its current tool line.
    assert t1["activity"]["state"] == "active"
    assert "Bash" in t1["activity"]["line"]
    assert t1["depends_on"] == [] and t1["waiting_on"] == []


def test_per_stage_progress_follows_the_task_pipeline(tmp_path) -> None:
    _mid_run(tmp_path)
    row = _snapshot(tmp_path)["runs"][0]
    stages = _tasks(row)["t1"]["stages"]

    names = [s["stage"] for s in stages]
    assert names[:2] == ["intake", "scope"]
    assert "deliver" in names  # the whole pipeline, not just the stages that ran
    by_name = {s["stage"]: s for s in stages}
    assert by_name["intake"]["status"] == "completed"
    assert by_name["intake"]["duration_s"] is not None
    assert by_name["scope"]["status"] == "running"
    assert by_name["deliver"]["status"] == "pending"
    assert by_name["deliver"]["duration_s"] is None
    for entry in stages:
        assert {"attempt", "cost_usd", "metered", "calls", "unmetered_calls"} <= set(entry)


def test_blocked_task_attention_carries_release_commands(tmp_path) -> None:
    rr = _mid_run(tmp_path)
    snap = _snapshot(tmp_path)
    blocked = [a for a in snap["attention"] if a["kind"] == "blocked_on_human"]
    assert len(blocked) == 1
    item = blocked[0]
    assert item["task_id"] == "t2"

    commands = [c["command"] for c in item["commands"]]
    verbs = [c.split(" --task ")[0].rsplit(" ", 1)[-1] for c in commands]
    assert verbs == ["approve", "reject", "abandon"]
    # Rooted at THIS row's store dir, so the line runs verbatim.
    assert all(f"--root {rr}" in c and "--run mid" in c for c in commands)
    # The gate the task parked at rides on the approve line, as in the task_blocked alert.
    assert "--note" in commands[0]

    t2 = _tasks(snap["runs"][0])["t2"]
    assert t2["state"] == "blocked_on_human"
    assert t2["blocked_reason"] == "needs an API that does not exist"
    # Parked, not in flight: no stage age, no stream.
    assert t2["stage_age_s"] is None
    assert t2["activity"]["state"] == "none"


def test_task_waiting_on_an_unfinished_dependency(tmp_path) -> None:
    _mid_run(tmp_path)
    t3 = _tasks(_snapshot(tmp_path)["runs"][0])["t3"]
    assert t3["depends_on"] == ["t1"]
    assert t3["waiting_on"] == ["t1"]
    assert t3["current_stage"] is None and t3["stages"][0]["status"] == "pending"


def test_stalled_stream_is_classified_stalled(tmp_path) -> None:
    rr = _mid_run(tmp_path)
    _write_stream(rr, "t1", "scope", age_s=1000)
    t1 = _tasks(_snapshot(tmp_path, stall_after_s=300)["runs"][0])["t1"]
    assert t1["activity"]["state"] == "stalled"
    assert t1["activity"]["seconds_since_event"] >= 300


def test_mid_run_driver_and_events(tmp_path) -> None:
    _mid_run(tmp_path)
    row = _snapshot(tmp_path)["runs"][0]
    # No headless driver ever claimed this run: "none", NOT "dead" (liveness reports an
    # unclaimed run as alive=False, which must not read as a dead driver).
    assert row["driver"]["state"] == "none"
    assert "no headless driver" in row["driver"]["summary"]

    sentences = [e["sentence"] for e in row["recent_events"]]
    assert any(s.startswith("t1: started SCOPE") for s in sentences)
    assert any(s.startswith("t2: blocked") for s in sentences)
    # Every row reads as words: no raw event-type identifier survives into a sentence.
    assert all(e["type"] not in e["sentence"] for e in row["recent_events"])


def test_recent_events_are_capped(tmp_path) -> None:
    _mid_run(tmp_path)
    row = _snapshot(tmp_path, recent_event_limit=3)["runs"][0]
    assert len(row["recent_events"]) == 3


def test_paused_run_attention_carries_unpause(tmp_path) -> None:
    rr = tmp_path / "p"
    eng = _engine(rr)
    eng.create_run("p")
    _intake(eng, "p", "t1")
    eng.pause_run("p", "batch circuit breaker")
    snap = _snapshot(tmp_path)
    paused = next(a for a in snap["attention"] if a["kind"] == "paused")
    assert [c["command"] for c in paused["commands"]] == [
        f"orchestrator --root {rr} --run p unpause"
    ]


def test_header_groups_rows_by_project(tmp_path) -> None:
    _mid_run(tmp_path)
    _finished_run(tmp_path)
    snap = _snapshot(tmp_path)
    groups = snap["header"]["projects"]
    assert [g["project"] for g in groups] == ["fake"]
    assert sorted(groups[0]["run_ids"]) == ["done", "mid"]
    assert groups[0]["runs"] == 2
    assert groups[0]["attention"] == 1  # the blocked task


# --- finished fixture -----------------------------------------------------------------


def test_finished_run_task_has_pr_and_complete_stage_list(tmp_path) -> None:
    _finished_run(tmp_path)
    row = _snapshot(tmp_path)["runs"][0]
    t1 = _tasks(row)["t1"]
    assert t1["state"] == "completed"
    assert t1["pr_url"] == "https://github.com/x/y/pull/1234"
    assert t1["title"] == "Fake t1"
    assert t1["stage_age_s"] is None and t1["model"] is None
    assert t1["activity"]["state"] == "none"
    assert {s["status"] for s in t1["stages"]} <= {"completed", "skipped"}
    assert all(s["duration_s"] is not None for s in t1["stages"] if s["status"] == "completed")
    assert row["driver"]["state"] == "finished"

    sentences = [e["sentence"] for e in row["recent_events"]]
    assert "t1: completed, PR https://github.com/x/y/pull/1234" in sentences
    # Cost is broken down per task and per stage, with every call accounted for.
    breakdown = row["cost_breakdown"]
    assert breakdown["calls"] == row["total_invocations"]
    assert set(breakdown["by_task"]) == {"t1"}
    assert "deliver" in breakdown["by_stage"]
    assert breakdown["input_tokens"] > 0


# --- #331: cost qualified at every level ----------------------------------------------


def test_cost_breakdown_counts_unmetered_per_stage(tmp_path) -> None:
    rr = tmp_path / "c"
    eng = _engine(rr)
    eng.create_run("c")
    eng.add_task("c", "t1")
    eng.add_task("c", "t2")
    rows = [
        {"run_id": "c", "task_id": "t1", "stage": "scope", "attempt": 0, "cost_usd": 0.5,
         "metered": True, "input_tokens": 100, "output_tokens": 20},
        {"run_id": "c", "task_id": "t1", "stage": "scope", "attempt": 1, "cost_usd": 0.0,
         "metered": False, "input_tokens": 0, "output_tokens": 0},
        {"run_id": "c", "task_id": "t2", "stage": "implement", "attempt": 0, "cost_usd": 1.0,
         "metered": True, "input_tokens": 300, "output_tokens": 50},
        # Another run's row in a shared ledger must not leak in (#281).
        {"run_id": "other", "task_id": "t1", "stage": "scope", "cost_usd": 9.0,
         "metered": True},
    ]
    (rr / "stage-costs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    row = _snapshot(tmp_path)["runs"][0]
    b = row["cost_breakdown"]
    assert b["cost_usd"] == 1.5 and b["calls"] == 3 and b["unmetered_calls"] == 1
    assert b["input_tokens"] == 400 and b["output_tokens"] == 70
    assert b["by_stage"]["scope"] == {"cost_usd": 0.5, "calls": 2, "unmetered_calls": 1,
                                      "input_tokens": 100, "output_tokens": 20}
    assert b["by_stage"]["implement"]["unmetered_calls"] == 0
    assert b["by_task"]["t2"]["cost_usd"] == 1.0

    # The per-task stage strip reads the same buckets: summed over attempts, and not
    # claiming to be metered when one of its calls was not.
    scope = next(s for s in _tasks(row)["t1"]["stages"] if s["stage"] == "scope")
    assert scope["cost_usd"] == 0.5
    assert scope["metered"] is False and scope["unmetered_calls"] == 1 and scope["calls"] == 2


# --- #498: degraded rows keep their cause ---------------------------------------------


def test_status_failure_keeps_exception_type_and_message(tmp_path) -> None:
    rr = tmp_path / "r1"
    _engine(rr).create_run("r1")

    def factory(run_root, project_ref=None):
        eng = _engine(run_root)

        def boom(*_a, **_kw):
            raise KeyError("stages")

        eng.status = boom  # type: ignore[method-assign]
        return eng

    snap = _snapshot(tmp_path, engine_factory=factory)
    row = snap["runs"][0]
    assert row["unreadable"] is True and row["degraded"] is True
    assert row["error"] == {"type": "KeyError", "message": "'stages'"}
    assert row["flags"] == ["unreadable status: KeyError"]
    assert snap["attention"][0]["error"]["type"] == "KeyError"
    out = render_dashboard(snap)
    assert "<unreadable status: KeyError: 'stages'>" in out
    assert "UNREADABLE status — KeyError: 'stages'" in out


def test_adapter_unresolved_is_distinct_from_unreadable(tmp_path) -> None:
    for name in ("bad-adapter", "bad-status"):
        _engine(tmp_path / name).create_run(name)

    def factory(run_root, project_ref=None):
        if run_root.name == "bad-adapter":
            raise AdapterUnresolved("project adapter 'gone' did not load: no module named gone")
        eng = _engine(run_root)
        eng.status = lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk"))  # type: ignore[method-assign]
        return eng

    snap = _snapshot(tmp_path, engine_factory=factory)
    rows = {r["run_id"]: r for r in snap["runs"]}
    adapter, unreadable = rows["bad-adapter"], rows["bad-status"]

    assert adapter["unreadable"] is False and adapter["degraded"] is True
    assert adapter["state"] == "<adapter-unresolved>"
    assert adapter["attention_items"][0]["kind"] == "adapter_unresolved"
    assert adapter["error"]["type"] == "AdapterUnresolved"
    assert unreadable["unreadable"] is True and unreadable["state"] == "<unreadable>"
    assert unreadable["attention_items"][0]["kind"] == "unreadable"
    assert unreadable["error"] == {"type": "OSError", "message": "disk"}

    out = render_dashboard(snap)
    adapter_line = next(ln for ln in out.splitlines() if "bad-adapter" in ln and "<" in ln)
    assert "<adapter unresolved>" in adapter_line and "unreadable" not in adapter_line
    assert "<unreadable status: OSError: disk>" in out


def test_long_exception_message_is_capped(tmp_path) -> None:
    rr = tmp_path / "r1"
    _engine(rr).create_run("r1")

    def factory(run_root, project_ref=None):
        eng = _engine(run_root)

        def boom(*_a, **_kw):
            raise RuntimeError("x" * 1000 + "\nsecond line")

        eng.status = boom  # type: ignore[method-assign]
        return eng

    row = _snapshot(tmp_path, engine_factory=factory)["runs"][0]
    assert len(row["error"]["message"]) <= 240
    assert "second line" not in row["error"]["message"]


# --- pure helpers ---------------------------------------------------------------------


def test_driver_summary_plain_words() -> None:
    assert _driver_summary(None, terminal=False)["state"] == "none"
    assert _driver_summary({"state": "unclaimed", "alive": False}, terminal=False)[
        "state"
    ] == "none"
    assert _driver_summary({"state": "dead", "alive": False}, terminal=True)["state"] == (
        "finished"
    )

    waiting = _driver_summary(
        {"state": "live", "alive": True, "last_state": "waiting_on_capacity"}, terminal=False
    )
    assert waiting["state"] == "capacity_wait"
    assert waiting["summary"] == "alive, sleeping out a capacity stall"

    gone = _driver_summary({"state": "dead", "alive": False, "pid": 42}, terminal=False)
    assert gone["state"] == "dead" and gone["reason"] == "its process (pid 42) is gone"

    exited = _driver_summary(
        {"state": "dead", "alive": False, "pid": 42, "exited": True, "exit_reason": "SIGTERM"},
        terminal=False,
    )
    assert exited["reason"] == "it recorded an exit (SIGTERM)"

    wedged = _driver_summary(
        {"state": "live", "alive": False, "heartbeat_stale": True, "heartbeat_age_s": 900},
        terminal=False,
    )
    assert wedged["state"] == "dead"
    assert wedged["summary"] == "driver down: its last heartbeat was 900s ago"

    foreign = _driver_summary({"state": "foreign_host", "alive": None, "host": "h2"},
                              terminal=False)
    assert foreign["state"] == "unknown" and "h2" in foreign["summary"]


def test_event_sentences_cover_the_common_types() -> None:
    cases = [
        ({"type": "stage_dispatched", "task_id": "t1", "stage": "implement",
          "model": "opus", "attempt": 0}, "t1: started IMPLEMENT on opus"),
        ({"type": "stage_dispatched", "task_id": "t1", "stage": "test", "attempt": 2},
         "t1: started TEST (retry 2)"),
        ({"type": "stage_recorded", "task_id": "t1", "stage": "scope", "status": "success",
          "cost_usd": 0.25}, "t1: finished SCOPE ($0.25)"),
        ({"type": "stage_recorded", "task_id": "t1", "stage": "test", "status": "failed",
          "outcome": "stage_failed_retry"}, "t1: TEST did not succeed: stage failed retry"),
        ({"type": "notification", "kind": "task_blocked", "task_id": "t2", "stage": "review",
          "reason": "needs sign-off"},
         "t2: blocked at REVIEW, needs a human decision (needs sign-off)"),
        ({"type": "task_completed", "task_id": "t1", "pr_url": "https://pr/1"},
         "t1: completed, PR https://pr/1"),
        ({"type": "run_paused", "reason": "budget"}, "run paused: budget"),
        ({"type": "driver_claimed", "pid": 7, "host": "mac"}, "driver pid 7 on mac took over "
         "the run"),
        ({"type": "dispatch_reclaimed", "task_id": "t1", "stage": "implement"},
         "t1: IMPLEMENT was re-queued at the same attempt after its driver died"),
        ({"type": "rate_limit_cooldown", "task_id": "t1", "stage": "review",
          "not_before": "14:00"}, "t1: REVIEW hit a rate limit; waiting until 14:00"),
        ({"type": "stage_rerouted_to_engine_lane", "task_id": "t1", "stage": "deliver",
          "reason": "codex sandbox"},
         "t1: DELIVER moved to the deterministic engine lane (codex sandbox)"),
        ({"type": "batch_integration_gate_ran", "green": True}, "batch integration check passed"),
        ({"type": "batch_integration_gate_red", "failing": ["pytest"]},
         "batch integration check failed: pytest"),
        ({"type": "batch_integration_gate_skipped", "reason": "fewer_than_two_completed"},
         "batch integration check skipped (fewer than two completed)"),
        # Humanised fallback for a type with no dedicated sentence.
        ({"type": "task_decomposed", "task_id": "t9", "stage": "scope"},
         "t9: task decomposed (SCOPE)"),
    ]
    for ev, expected in cases:
        assert event_sentence(ev) == expected, ev


def test_notification_duplicates_are_not_repeated(tmp_path) -> None:
    _finished_run(tmp_path)
    row = _snapshot(tmp_path, recent_event_limit=50)["runs"][0]
    completed = [e for e in row["recent_events"] if "completed, PR" in e["sentence"]]
    assert len(completed) == 1  # the task_completed event, not also its notification row


def test_every_row_carries_the_new_keys(tmp_path) -> None:
    """Degraded and readable rows expose the same keys, so a renderer tests content only."""
    _finished_run(tmp_path)
    bad = tmp_path / "bad"
    _engine(bad).create_run("bad")
    (bad / "status-bad.json").write_text("{ not json")
    snap = _snapshot(tmp_path)
    keys = {"tasks", "driver", "cost_breakdown", "recent_events", "degraded", "error"}
    for row in snap["runs"]:
        assert keys <= set(row)
    for item in snap["attention"]:
        assert "commands" in item
