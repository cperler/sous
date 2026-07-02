"""Timeout wiring (review Phase A #1).

STAGE_SPECS carries a per-stage wall-clock ceiling; ``next_work`` threads it into the
WorkItem; both real transports pass it to ``subprocess.run`` and convert a
``TimeoutExpired`` into a classifiable TIMEOUT StageResult — a hung CLI must never hang
the scheduler thread.
"""

from __future__ import annotations

import subprocess

from adapters.execution.codex import CodexRunner
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.transport import RawResult, claude_cli_transport, codex_cli_transport
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

POLICY_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
POLICY_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _engine(tmp_path, project, **kw) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project, **kw)


def _wi(*, stage=Stage.IMPLEMENT, policy=POLICY_HEADLESS, timeout_s=1) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=stage, prompt="p",
        schema_ref="implement", model="claude-opus-4-8", lane_policy=policy,
        created_at="2026-06-22T00:00:00Z", timeout_s=timeout_s,
    )


# --- the engine populates timeout_s from the stage spec -------------------
def test_next_work_sets_stage_timeout(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")
    assert work is not None
    # first in-lane stage is intake; its ceiling comes straight from the spec
    assert work.stage is Stage.INTAKE
    assert work.timeout_s == STAGE_SPECS[Stage.INTAKE].timeout_s
    assert work.timeout_s is not None  # regression: was always None -> subprocess timeout=None


def test_every_stage_spec_has_a_positive_timeout() -> None:
    assert all(spec.timeout_s and spec.timeout_s > 0 for spec in STAGE_SPECS.values())


# --- the real transports pass the ceiling to subprocess.run ---------------
def test_claude_transport_passes_timeout_and_classifies(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_wi(timeout_s=7))
    assert seen["timeout"] == 7  # WorkItem ceiling reached subprocess.run
    assert raw.exit_code == 124  # TimeoutExpired -> classifiable timeout, no exception escaped
    # and the runner maps that to a TIMEOUT StageResult (a failure, not a crash)
    sr = HeadlessClaudeRunner(transport=lambda w: raw).dispatch(_wi(timeout_s=7))
    assert sr.status is ResultStatus.TIMEOUT


def test_codex_transport_passes_timeout_and_classifies(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = codex_cli_transport()(_wi(policy=POLICY_CODEX, timeout_s=9))
    assert seen["timeout"] == 9
    assert raw.exit_code == 124
    sr = CodexRunner(transport=lambda w: raw).dispatch(_wi(policy=POLICY_CODEX, timeout_s=9))
    assert sr.status is ResultStatus.TIMEOUT


# --- a transport that genuinely sleeps past the ceiling does NOT hang -----
def _real_sleeping_transport(work: WorkItem) -> RawResult:
    """Shell a real ``sleep`` far longer than the ceiling; the OS-level timeout must
    kill it and yield a 124 RawResult rather than blocking forever."""
    try:
        subprocess.run(["sleep", "30"], capture_output=True, text=True, timeout=work.timeout_s)
    except subprocess.TimeoutExpired:
        return RawResult(None, exit_code=124, error=f"timed out after {work.timeout_s}s")
    return RawResult({"committed": True})  # pragma: no cover - only if sleep somehow returns first


def test_sleeping_transport_produces_failed_result_not_a_hang() -> None:
    runner = HeadlessClaudeRunner(transport=_real_sleeping_transport)
    sr = runner.dispatch(_wi(timeout_s=1))  # sleep 30 vs 1s ceiling -> killed at ~1s
    assert sr.status is ResultStatus.TIMEOUT
    assert "timed out" in (sr.error or "")


# --- a TIMEOUT result is a classifiable stage failure end-to-end ----------
def test_engine_treats_timeout_as_retryable_failure(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    work = eng.next_work("r1", "t1")
    res = make_result(work, status=ResultStatus.TIMEOUT, error="timed out after 300s")
    out = eng.record("r1", res)
    # not a crash and not a completion: it retries with learnings (attempts remain)
    assert out["outcome"] == "stage_failed_will_retry"
    assert out["task_state"] == "retrying"
