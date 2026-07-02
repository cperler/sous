"""Per-task session continuity (2026-07-01 design pass §2).

The invariant under test everywhere here: a session ref is ROUTING METADATA. Prompts
stay self-contained, content_hash ignores it, failure clears it, and a dead session
cold-starts inside the same dispatch — correctness never depends on continuity.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.transport import claude_cli_transport
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


# --- engine threading ---------------------------------------------------------

def test_next_stage_receives_previous_stages_session_ref(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    assert intake.session_ref is None  # first stage: nothing to resume
    eng.record("r1", make_result(intake, session_ref="sess-abc"))
    scope = eng.next_work("r1", "t1")
    assert scope.session_ref == "sess-abc"


def test_runner_reporting_no_ref_keeps_the_prior_one(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, session_ref="sess-abc"))
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w))  # e.g. the interactive lane: no session reported
    assert eng.next_work("r1", "t1").session_ref == "sess-abc"


def test_failure_clears_the_session_ref_no_warm_retry(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, session_ref="sess-abc"))
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, status=ResultStatus.FAILURE, error="boom",
                                 session_ref="sess-abc"))
    retry = eng.next_work("r1", "t1")
    assert retry.attempt == 1
    assert retry.session_ref is None  # fresh session after a failure (design §2)


def test_session_ref_is_excluded_from_content_hash() -> None:
    kw = dict(
        id="wi-1", run_id="r", task_id="t", stage=Stage.SCOPE, prompt="p",
        schema_ref="scope", model="m", created_at="now",
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )
    with_ref = WorkItem.create(**kw, session_ref="sess-abc")
    without = WorkItem.create(**kw)
    assert with_ref.content_hash == without.content_hash  # routing metadata, not content


# --- claude transport ---------------------------------------------------------

class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _work(session_ref=None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r", task_id="t", stage=Stage.SCOPE, prompt="p",
        schema_ref="scope", model="m", created_at="now", session_ref=session_ref,
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )


def test_transport_resumes_and_reports_the_session(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Proc(stdout=json.dumps({"structured_output": {"ok": True},
                                        "session_id": "sess-new"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_work(session_ref="sess-old"))
    assert calls[0][-2:] == ["--resume", "sess-old"]
    assert raw.session_ref == "sess-new"  # reported back for the engine to chain
    assert "--resume sess-old" in raw.invocation


def test_transport_cold_starts_without_a_ref(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Proc(stdout=json.dumps({"structured_output": {}, "session_id": "sess-new"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_work())
    assert "--resume" not in calls[0]
    assert raw.session_ref == "sess-new"


def test_lost_session_falls_back_to_fresh_in_the_same_dispatch(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "--resume" in argv:
            return _Proc(returncode=1, stderr="Error: No conversation found with session ID sess-old")
        return _Proc(stdout=json.dumps({"structured_output": {"ok": True},
                                        "session_id": "sess-fresh"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_work(session_ref="sess-old"))
    assert len(calls) == 2 and "--resume" not in calls[1]
    assert raw.exit_code == 0 and raw.session_ref == "sess-fresh"


def test_other_resume_errors_fail_the_dispatch_normally(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Proc(returncode=1, stderr="Error: model not available")

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_work(session_ref="sess-old"))
    assert len(calls) == 1  # no fresh-session retry for a non-session error
    assert raw.exit_code == 1 and "model not available" in raw.error
