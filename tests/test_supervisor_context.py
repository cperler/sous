"""Interactive supervisor context sensing and clean park lifecycle (#259)."""

from __future__ import annotations

import io
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError, SupervisorParkDeferred
from orchestrator.schemas.enums import RunState, Stage
from orchestrator.status_store import StatusStore
from orchestrator.supervisor_context import (
    SupervisorContext,
    capture_statusline_context,
    read_supervisor_context,
)
from tests.conftest import FakeProject, make_result


def _engine(tmp_path) -> Engine:
    return Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), FakeProject()
    )


def _context(*, remaining: float, session: str = "session-1") -> SupervisorContext:
    return SupervisorContext(
        available=True,
        observed_at=100.0,
        session_id=session,
        cwd="/project",
        context_window_size=200_000,
        used_percentage=100 - remaining,
        remaining_percentage=remaining,
    )


def _through_intake(engine: Engine, run: str, task: str) -> None:
    work = engine.next_work(run, task, supervisor_context=_context(remaining=1))
    assert work is not None and work.stage is Stage.INTAKE
    engine.record(run, make_result(work))


def test_statusline_capture_round_trips_and_stale_fails_closed(tmp_path) -> None:
    payload = {
        "session_id": "s1",
        "cwd": str(tmp_path / "project"),
        "context_window": {"context_window_size": 200_000, "used_percentage": 73.5},
    }
    path = capture_statusline_context(payload, cache_root=tmp_path / "cache", now=100.0)
    assert path is not None
    stored = json.loads(path.read_text())
    assert stored["remaining_percentage"] == 26.5

    fresh = read_supervisor_context(
        tmp_path / "project", cache_root=tmp_path / "cache", now=110.0
    )
    assert fresh.available is True
    assert fresh.session_id == "s1"
    assert fresh.remaining_tokens == 53_000

    stale = read_supervisor_context(
        tmp_path / "project", cache_root=tmp_path / "cache", now=131.0
    )
    assert stale.available is False
    assert stale.reason == "supervisor context sensor is stale"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available", "yes"),
        ("observed_at", "recent"),
        ("context_window_size", True),
        ("context_window_size", 0),
        ("used_percentage", []),
        ("remaining_percentage", float("nan")),
        ("session_id", 123),
        ("cwd", None),
    ],
)
def test_malformed_valid_json_cache_fails_closed(tmp_path, field, value) -> None:
    project = tmp_path / "project"
    path = capture_statusline_context(
        {
            "session_id": "s1",
            "cwd": str(project),
            "context_window": {
                "context_window_size": 200_000,
                "remaining_percentage": 50,
            },
        },
        cache_root=tmp_path / "cache",
        now=100.0,
    )
    assert path is not None
    row = json.loads(path.read_text())
    row[field] = value
    path.write_text(json.dumps(row))

    snapshot = read_supervisor_context(
        project, cache_root=tmp_path / "cache", now=110.0
    )
    assert snapshot.available is False
    assert snapshot.reason == "supervisor context sensor unavailable"
    assert snapshot.projected("next prompt", min_remaining_pct=20)["should_park"] is True


def test_statusline_cli_captures_context_for_sensor_command(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRATOR_SUPERVISOR_CONTEXT_DIR", str(tmp_path / "cache"))
    payload = {
        "session_id": "cli-session",
        "cwd": str(tmp_path),
        "context_window": {
            "context_window_size": 200_000,
            "remaining_percentage": 64.0,
        },
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("orchestrator.usage_probe.read_usage", lambda: None)
    assert main(["statusline"]) == 0
    capsys.readouterr()

    assert main(["supervisor-context", "--max-age", "60"]) == 0
    sensed = json.loads(capsys.readouterr().out)
    assert sensed["available"] is True
    assert sensed["session_id"] == "cli-session"
    assert sensed["remaining_tokens"] == 128_000


def test_guarded_next_cli_parks_then_fresh_session_resumes(tmp_path, monkeypatch, capsys) -> None:
    """The CLI must carry the sensor into ``next_work``, not merely expose it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ORCHESTRATOR_SUPERVISOR_CONTEXT_DIR", str(tmp_path / "cache"))
    engine = _engine(tmp_path)
    engine.create_run("r1")
    engine.add_task("r1", "t1")
    _through_intake(engine, "r1", "t1")
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]

    capture_statusline_context(
        {
            "session_id": "old-session",
            "cwd": str(tmp_path),
            "context_window": {"context_window_size": 200_000, "remaining_percentage": 1},
        }
    )
    assert main([
        *base,
        "next",
        "--task",
        "t1",
        "--guard-supervisor-context",
        "--supervisor-resume-command",
        "fresh-session /orchestrate-task-interactive r1 t1",
    ]) == 0
    assert json.loads(capsys.readouterr().out) is None
    assert engine.store.load_run("r1").state is RunState.PARKED
    assert engine.store.load_task("r1", "t1").pending_work_item_id is None

    capture_statusline_context(
        {
            "session_id": "new-session",
            "cwd": str(tmp_path),
            "context_window": {"context_window_size": 200_000, "remaining_percentage": 90},
        }
    )
    assert main([*base, "resume-supervisor"]) == 0
    assert json.loads(capsys.readouterr().out) == {"resumed": "r1", "state": "running"}
    assert engine.store.load_run("r1").state is RunState.RUNNING

    assert main([*base, "next", "--task", "t1", "--guard-supervisor-context"]) == 0
    work = json.loads(capsys.readouterr().out)
    assert work["stage"] == "scope"
    assert engine.store.load_task("r1", "t1").pending_work_item_id == work["id"]


def test_low_context_parks_before_prompt_artifact_or_lease(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.create_run("r1")
    engine.add_task("r1", "t1")
    _through_intake(engine, "r1", "t1")

    work = engine.next_work(
        "r1",
        "t1",
        supervisor_context=_context(remaining=20),
        supervisor_resume_command="fresh-session /orchestrate-task-interactive r1 t1",
    )
    assert work is None
    run = engine.store.load_run("r1")
    task = engine.store.load_task("r1", "t1")
    assert run.state is RunState.PARKED
    assert task.pending_work_item_id is None
    assert task.stages[Stage.SCOPE].status.value == "pending"
    assert not list((tmp_path / "stages" / "t1").glob("scope-*.prompt.txt"))

    events = engine.store.read_events("r1")
    parked = [event for event in events if event["type"] == "supervisor_parked"]
    assert len(parked) == 1
    assert parked[0]["context"]["projected_prompt_tokens"] > 0
    assert parked[0]["resume_command"].startswith("fresh-session")
    assert not [
        event
        for event in events
        if event["type"] == "stage_dispatched" and event.get("stage") == "scope"
    ]

    status = engine.status("r1", stale_after_s=-1)
    assert status["run_state"] == "parked"
    assert status["supervisor_parked"]["reason"]
    assert status["tasks"]["t1"]["stale"] is False
    assert engine.dispatchable("r1") == []

    # Repeating park is a no-op for the episode, not event spam.
    engine.park_supervisor("r1", reason="again", resume_command="again")
    assert len([
        event for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_parked"
    ]) == 1


def test_fresh_supervisor_resumes_and_dispatches_same_stage(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.create_run("r1")
    engine.add_task("r1", "t1")
    _through_intake(engine, "r1", "t1")
    assert engine.next_work("r1", "t1", supervisor_context=_context(remaining=1)) is None

    with pytest.raises(ContractError, match="fresh supervisor context snapshot"):
        engine.resume_supervisor("r1")
    with pytest.raises(ContractError, match="fresh Claude Code session"):
        engine.resume_supervisor("r1", supervisor_session_id="session-1")
    resumed = engine.resume_supervisor("r1", supervisor_session_id="session-2")
    assert resumed.state is RunState.RUNNING
    work = engine.next_work(
        "r1", "t1", supervisor_context=_context(remaining=90, session="session-2")
    )
    assert work is not None and work.stage is Stage.SCOPE
    assert engine.store.load_task("r1", "t1").pending_work_item_id == work.id


def test_batch_low_context_defers_park_until_existing_lease_drains(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.create_run("r1")
    for task in ("t1", "t2"):
        engine.add_task("r1", task)
        _through_intake(engine, "r1", task)

    live = engine.next_work("r1", "t1", supervisor_context=_context(remaining=90))
    assert live is not None
    with pytest.raises(SupervisorParkDeferred) as caught:
        engine.next_work("r1", "t2", supervisor_context=_context(remaining=1))
    assert caught.value.in_flight == ["t1"]
    assert engine.store.load_run("r1").state is RunState.RUNNING
    assert engine.store.load_task("r1", "t2").pending_work_item_id is None

    engine.record("r1", make_result(live))
    assert engine.next_work("r1", "t2", supervisor_context=_context(remaining=1)) is None
    assert engine.store.load_run("r1").state is RunState.PARKED


def test_concurrent_fresh_dispatch_commits_before_low_context_can_park(
    tmp_path, monkeypatch
) -> None:
    """The park decision and every fresh lease commit share one run-level lock."""
    engine = _engine(tmp_path)
    engine.create_run("r1")
    for task in ("t1", "t2"):
        engine.add_task("r1", task)
        _through_intake(engine, "r1", task)

    commit_entered = threading.Event()
    release_commit = threading.Event()
    original_commit = engine.store.commit_task_events

    def delayed_commit(*args, **kwargs):
        commit_entered.set()
        assert release_commit.wait(timeout=5)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(engine.store, "commit_task_events", delayed_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(
            engine.next_work, "r1", "t1", supervisor_context=_context(remaining=90)
        )
        assert commit_entered.wait(timeout=5)
        park = pool.submit(
            engine.next_work, "r1", "t2", supervisor_context=_context(remaining=1)
        )
        release_commit.set()
        live = dispatch.result(timeout=5)
        assert live is not None
        with pytest.raises(SupervisorParkDeferred) as caught:
            park.result(timeout=5)

    assert caught.value.in_flight == ["t1"]
    assert engine.store.load_run("r1").state is RunState.RUNNING
    assert engine.store.load_task("r1", "t1").pending_work_item_id == live.id
    assert engine.store.load_task("r1", "t2").pending_work_item_id is None


def test_concurrent_dispatch_refuses_commit_after_low_context_parks(
    tmp_path, monkeypatch
) -> None:
    engine = _engine(tmp_path)
    engine.create_run("r1")
    for task in ("t1", "t2"):
        engine.add_task("r1", task)
        _through_intake(engine, "r1", task)

    park_entered = threading.Event()
    release_park = threading.Event()
    original_park = engine._park_supervisor_locked

    def delayed_park(*args, **kwargs):
        park_entered.set()
        assert release_park.wait(timeout=5)
        return original_park(*args, **kwargs)

    monkeypatch.setattr(engine, "_park_supervisor_locked", delayed_park)
    with ThreadPoolExecutor(max_workers=2) as pool:
        park = pool.submit(
            engine.next_work, "r1", "t1", supervisor_context=_context(remaining=1)
        )
        assert park_entered.wait(timeout=5)
        dispatch = pool.submit(
            engine.next_work, "r1", "t2", supervisor_context=_context(remaining=90)
        )
        release_park.set()
        assert park.result(timeout=5) is None
        assert dispatch.result(timeout=5) is None

    assert engine.store.load_run("r1").state is RunState.PARKED
    for task in ("t1", "t2"):
        assert engine.store.load_task("r1", task).pending_work_item_id is None
        assert not list((tmp_path / "stages" / task).glob("scope-*.prompt.txt"))


def test_concurrent_record_finalization_wins_over_stale_park_snapshot(
    tmp_path, monkeypatch
) -> None:
    """A final record may finish between the lease scan and park run-doc commit."""
    engine = _engine(tmp_path)
    engine.create_run("r1")
    engine.add_task("r1", "t1", pipeline=[Stage.SCOPE])
    work = engine.next_work(
        "r1", "t1", supervisor_context=_context(remaining=90)
    )
    assert work is not None

    record_at_finalize = threading.Event()
    release_record = threading.Event()
    original_finalize = engine._maybe_finalize_run

    def delayed_finalize(run_id):
        record_at_finalize.set()
        assert release_record.wait(timeout=5)
        return original_finalize(run_id)

    park_at_commit = threading.Event()
    release_park = threading.Event()
    original_commit = engine.store.commit_run_events

    def delayed_park_commit(*args, **kwargs):
        park_at_commit.set()
        assert release_park.wait(timeout=5)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(engine, "_maybe_finalize_run", delayed_finalize)
    monkeypatch.setattr(engine.store, "commit_run_events", delayed_park_commit)

    with ThreadPoolExecutor(max_workers=2) as pool:
        record = pool.submit(engine.record, "r1", make_result(work))
        assert record_at_finalize.wait(timeout=5)
        assert engine.store.load_task("r1", "t1").pending_work_item_id is None

        park = pool.submit(
            engine.park_supervisor,
            "r1",
            reason="context exhausted",
            resume_command="resume r1",
        )
        assert park_at_commit.wait(timeout=5)
        release_record.set()
        record.result(timeout=5)
        assert engine.store.load_run("r1").state is RunState.COMPLETED

        release_park.set()
        with pytest.raises(ContractError, match="cannot park terminal run"):
            park.result(timeout=5)

    assert engine.store.load_run("r1").state is RunState.COMPLETED
    assert not [
        event
        for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_parked"
    ]


def test_park_and_resume_recover_event_first_crash_without_duplicate(
    tmp_path, monkeypatch
) -> None:
    engine = _engine(tmp_path)
    engine.create_run("r1")
    engine.add_task("r1", "t1")

    original_write = engine.store._write_run
    crash_state: RunState | None = RunState.PARKED

    def crash_once(run):
        nonlocal crash_state
        if run.state is crash_state:
            crash_state = None
            raise RuntimeError("simulated crash before run commit")
        return original_write(run)

    monkeypatch.setattr(engine.store, "_write_run", crash_once)
    context = {"session_id": "old-session", "remaining_percentage": 1}

    with pytest.raises(RuntimeError, match="before run commit"):
        engine.park_supervisor(
            "r1",
            reason="context exhausted",
            resume_command="resume r1",
            context=context,
        )
    assert engine.store.load_run("r1").state is RunState.RUNNING
    parked_events = [
        event
        for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_parked"
    ]
    assert len(parked_events) == 1

    parked = engine.park_supervisor(
        "r1",
        reason="context exhausted",
        resume_command="resume r1",
        context=context,
    )
    assert parked.state is RunState.PARKED
    assert parked.supervisor_parked_at == parked_events[0]["ts"]
    assert len([
        event
        for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_parked"
    ]) == 1

    crash_state = RunState.RUNNING
    with pytest.raises(RuntimeError, match="before run commit"):
        engine.resume_supervisor("r1", supervisor_session_id="new-session")
    assert engine.store.load_run("r1").state is RunState.PARKED
    resumed_events = [
        event
        for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_resumed"
    ]
    assert len(resumed_events) == 1

    resumed = engine.resume_supervisor("r1", supervisor_session_id="new-session")
    assert resumed.state is RunState.RUNNING
    assert len([
        event
        for event in engine.store.read_events("r1")
        if event["type"] == "supervisor_resumed"
    ]) == 1
