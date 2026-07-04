"""Warm-retry session policy (#8): opt-in reuse of a failed attempt's session.

The default is fresh-after-failure (2026-07-01 design pass §2 — a failed attempt's
context is as likely poisoned as useful). This flag makes reuse an EXPLICIT, conservative
opt-in: keep the session only when the failure was mechanical (timeout / rate-limit /
infra), on the same provider, and the worktree still matches what the session remembers
(salvage kept the committed work, OR the stage does no checkpoint reset). A content
failure (schema violation, real test failure) always retries cold.

The crux under test is the salvage/worktree coupling: a warm session WITHOUT salvage on a
git stage is a trap — the reset throws away edits the session still remembers — so warm
requires salvage there; a non-checkpoint stage (SCOPE/REVIEW) never resets, so it is safe
to keep alone.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.transport import claude_cli_transport
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

ANCHOR = {"tag": "anchor-tag", "sha": "0" * 40}


def _salvage(count: int = 2) -> dict:
    commits = [
        {"sha": "abc123def0aaa", "subject": "add core feature"},
        {"sha": "def456abc0bbb", "subject": "wip tests"},
    ][:count]
    return {"anchor": ANCHOR["tag"], "count": count, "commits": commits}


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _events(eng: Engine, run="r1") -> list[dict]:
    return list(eng.store.read_events(run))


def _warm_events(eng: Engine, run="r1") -> list[dict]:
    return [e for e in _events(eng, run) if e["type"] == "warm_retry_used"]


def _drive_to_implement(eng: Engine, *, warm: bool, session="sess-intake") -> WorkItem:
    """LITE task through intake (baseline checkpoint + a session) to a dispatched IMPLEMENT."""
    eng.create_run("r1", ExecutionLane.LITE, warm_retry=warm)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    assert intake.stage is Stage.INTAKE
    eng.record("r1", make_result(intake, session_ref=session, checkpoint=ANCHOR))
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.IMPLEMENT
    assert work.session_ref == session  # prior success threaded the session in
    return work


# --- flag OFF: the design-pass default is locked ------------------------------

def test_flag_off_clears_the_session_even_with_salvage(tmp_path, project) -> None:
    """Warm retry requires the run flag: OFF → cold even for a salvage-kept timeout."""
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=False)
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out after 300s",
                                 salvage=_salvage(), session_ref="sess-impl"))
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is True  # salvage still keeps the work (independent lever)
    assert t.session_ref is None and t.session_provider is None  # but the session is cold
    assert _warm_events(eng) == []
    assert eng.next_work("r1", "t1").session_ref is None


# --- flag ON, the eligible path: mechanical failure + salvage kept ------------

def test_timeout_with_salvage_keeps_the_session_warm(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    out = eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT,
                                       error="timed out after 300s", salvage=_salvage(),
                                       session_ref="sess-impl"))
    assert out["outcome"] == "stage_failed_will_retry"
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is True  # git stage: salvage kept the work…
    assert t.session_ref == "sess-impl"  # …so the failed attempt's own session is reused
    assert t.session_provider is Provider.CLAUDE
    warm = _warm_events(eng)
    assert len(warm) == 1
    assert warm[0]["kind"] == "timeout"
    assert warm[0]["session_provider"] == "claude"
    assert warm[0]["session_ref"] == "sess-impl"
    assert any("WARM RETRY" in ln for ln in t.learnings)  # the mid-stream heads-up
    retry = eng.next_work("r1", "t1")
    assert retry.session_ref == "sess-impl" and retry.reset_to is None  # warm + no reset


def test_no_reported_ref_keeps_the_prior_one_warm(tmp_path, project) -> None:
    """A warm keep with no ref on the failed result leaves the threaded-in session in place."""
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True, session="sess-intake")
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out",
                                 salvage=_salvage()))  # runner reported no session_ref
    t = eng.store.load_task("r1", "t1")
    assert t.session_ref == "sess-intake"  # the prior success's ref, kept warm
    assert _warm_events(eng)[0]["session_ref"] == "sess-intake"


# --- the crux: worktree/session coupling on a git stage -----------------------

def test_timeout_without_salvage_on_a_git_stage_is_cold(tmp_path, project) -> None:
    """No salvage on a checkpoint stage → the retry resets the tree, so the session would be
    stale. Warm retry refuses it (the crux coupling)."""
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out",
                                 salvage=None, session_ref="sess-impl"))
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is False
    assert t.session_ref is None  # cold: a reset is coming, the session would remember wiped work
    assert _warm_events(eng) == []
    retry = eng.next_work("r1", "t1")
    assert retry.reset_to == ANCHOR["tag"] and retry.session_ref is None  # reset + cold


def test_non_git_stage_keeps_the_session_warm_without_salvage(tmp_path, project) -> None:
    """SCOPE is a non-checkpoint stage — it never resets the tree — so a mechanical failure
    keeps the session warm with no salvage needed (the OR-branch of the coupling)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL, warm_retry=True)
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    eng.record("r1", make_result(intake, session_ref="sess-intake", checkpoint=ANCHOR))
    scope = eng.next_work("r1", "t1")
    assert scope.stage is Stage.SCOPE and scope.session_ref == "sess-intake"
    eng.record("r1", make_result(scope, status=ResultStatus.TIMEOUT, error="timed out",
                                 session_ref="sess-scope"))
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is False  # non-git: nothing to salvage…
    assert t.session_ref == "sess-scope"  # …but the tree never resets, so warm is safe
    assert _warm_events(eng)[0]["stage"] == "scope"
    retry = eng.next_work("r1", "t1")
    assert retry.stage is Stage.SCOPE and retry.session_ref == "sess-scope"


# --- content failures always retry cold ---------------------------------------

def test_genuine_failure_is_always_cold(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    eng.record("r1", make_result(work, status=ResultStatus.FAILURE, error="assertion failed",
                                 salvage=_salvage(), session_ref="sess-impl"))
    t = eng.store.load_task("r1", "t1")
    assert t.salvage_in_place is False  # the committed code may BE the defect — discarded
    assert t.session_ref is None  # …and its context is cold
    assert _warm_events(eng) == []


def test_schema_violation_is_always_cold(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    eng.record("r1", make_result(work, status=ResultStatus.SCHEMA_VIOLATION,
                                 error="missing required key", salvage=_salvage(),
                                 session_ref="sess-impl"))
    t = eng.store.load_task("r1", "t1")
    assert t.session_ref is None and _warm_events(eng) == []


# --- provider coupling: no cross-provider warmth ------------------------------

def test_cross_provider_session_is_never_reused_warm(tmp_path, project) -> None:
    """(d): a session produced on a DIFFERENT provider than the retry routes to is never
    kept warm — composes with #7 fallthrough (a re-routed stage retries cold)."""
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    # The failed dispatch reports a CODEX session, but this LITE task's IMPLEMENT retry
    # routes to claude → provider mismatch → cold despite the timeout + salvage.
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out",
                                 salvage=_salvage(), session_ref="sess-codex",
                                 provider=Provider.CODEX))
    t = eng.store.load_task("r1", "t1")
    assert t.session_ref is None and _warm_events(eng) == []


# --- terminal failure reuses nothing ------------------------------------------

def test_terminal_failure_clears_the_session(tmp_path, project) -> None:
    """Max attempts reached → no retry follows → the session is cleared even for a timeout."""
    eng = _engine(tmp_path, project, max_attempts=1)
    work = _drive_to_implement(eng, warm=True)
    out = eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out",
                                       salvage=_salvage(), session_ref="sess-impl"))
    assert out["outcome"] == "task_failed_max_attempts"
    t = eng.store.load_task("r1", "t1")
    assert t.session_ref is None and _warm_events(eng) == []


# --- the safety net: a lost warm session cold-starts in the transport ---------

def test_warm_retry_dispatch_with_a_dead_session_cold_starts(tmp_path, project, monkeypatch) -> None:
    """End-to-end: a warm retry threads the kept session onto the retry WorkItem; if that id
    has since expired, the transport's session-lost fallback cold-starts inside the dispatch
    (continuity is routing metadata — correctness never depends on it)."""
    eng = _engine(tmp_path, project)
    work = _drive_to_implement(eng, warm=True)
    eng.record("r1", make_result(work, status=ResultStatus.TIMEOUT, error="timed out",
                                 salvage=_salvage(), session_ref="sess-impl"))
    retry = eng.next_work("r1", "t1")
    assert retry.session_ref == "sess-impl"  # warm session threaded onto the retry

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "--resume" in argv:  # the warm id is gone
            return _Proc(returncode=1, stderr="Error: No conversation found with session ID sess-impl")
        return _Proc(stdout=json.dumps({"structured_output": {"ok": True},
                                        "session_id": "sess-fresh"}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(retry)
    assert len(calls) == 2 and "--resume" not in calls[1]  # resumed, lost, then fresh
    assert raw.exit_code == 0 and raw.session_ref == "sess-fresh"


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
