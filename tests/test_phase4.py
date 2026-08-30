"""Phase 4: headless + codex runners, headless run target, conformance, config-only mode."""

from __future__ import annotations

import pytest

from adapters.execution.base import Registry
from adapters.execution.codex import CodexRunner
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.runners import build_registry, registry_runner
from adapters.execution.transport import RawResult
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import NoRunnerError
from orchestrator.routing import Router
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import (
    STAGE_ORDER,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import LanePolicy, TokenUsage, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, _default_output, make_result

POLICY_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
POLICY_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _wi(stage=Stage.IMPLEMENT, policy=POLICY_HEADLESS) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=stage, prompt="p",
        schema_ref="implement", model="claude-opus-5", lane_policy=policy,
        created_at="2026-06-22T00:00:00Z",
    )


def fake_headless_transport(work: WorkItem) -> RawResult:
    return RawResult(
        structured_output=_default_output(work.stage),
        usage=TokenUsage(input=100, output=50),
        invocation="claude -p (fake)",
    )


# --- runners --------------------------------------------------------------
def test_headless_claude_runner_success() -> None:
    runner = HeadlessClaudeRunner(transport=fake_headless_transport)
    sr = runner.dispatch(_wi())
    assert sr.status is ResultStatus.SUCCESS
    assert sr.lane_used.execution_mode is ExecutionMode.HEADLESS
    assert sr.lane_used.provider is Provider.CLAUDE


def test_headless_runner_schema_violation_on_no_output() -> None:
    runner = HeadlessClaudeRunner(transport=lambda w: RawResult(structured_output=None))
    assert runner.dispatch(_wi()).status is ResultStatus.SCHEMA_VIOLATION


def test_headless_runner_failure_on_nonzero_exit() -> None:
    runner = HeadlessClaudeRunner(transport=lambda w: RawResult(None, exit_code=1, error="boom"))
    assert runner.dispatch(_wi()).status is ResultStatus.FAILURE


# --- codex: tightened FULL schema validation ------------------------------
_SCHEMA = {
    "type": "object",
    "required": ["committed"],
    "properties": {"committed": {"type": "boolean"}},
}


def _codex(out: dict | None):
    return CodexRunner(
        transport=lambda w: RawResult(structured_output=out, invocation="codex exec (fake)"),
        schema_provider=lambda ref: _SCHEMA,
    )


def test_codex_full_validation_passes_valid() -> None:
    assert _codex({"committed": True}).dispatch(_wi(policy=POLICY_CODEX)).status is ResultStatus.SUCCESS


def test_codex_full_validation_rejects_wrong_type() -> None:
    # required key IS present (the as-built heuristic would PASS) but the type is wrong
    # -> full validation rejects it. This is the fix-forward (§2 #5).
    sr = _codex({"committed": "yes"}).dispatch(_wi(policy=POLICY_CODEX))
    assert sr.status is ResultStatus.SCHEMA_VIOLATION


def test_codex_full_validation_rejects_missing_required() -> None:
    assert _codex({}).dispatch(_wi(policy=POLICY_CODEX)).status is ResultStatus.SCHEMA_VIOLATION


def test_codex_lane_is_headless_codex() -> None:
    sr = _codex({"committed": True}).dispatch(_wi(policy=POLICY_CODEX))
    assert (sr.lane_used.execution_mode, sr.lane_used.provider) == (ExecutionMode.HEADLESS, Provider.CODEX)


# --- registry / registry_runner -------------------------------------------
def test_registry_runner_dispatches_headless() -> None:
    reg = build_registry(headless_transport=fake_headless_transport, include_interactive=False)
    results = registry_runner(reg)([_wi()])
    assert results[0].status is ResultStatus.SUCCESS


def test_registry_runner_rejects_external_interactive() -> None:
    reg = build_registry(headless_transport=fake_headless_transport)  # interactive is external
    with pytest.raises(NoRunnerError):
        registry_runner(reg)([_wi(policy=LanePolicy(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE))])


# --- headless run target (config-only mode flip) --------------------------
def _headless_engine(tmp_path) -> tuple[Engine, Registry]:
    reg = build_registry(
        headless_transport=fake_headless_transport,
        setup_project=FakeProject(),  # wires the deterministic ENGINE-lane intake runner
        include_interactive=False,
    )
    eng = Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        FakeProject(),
        router=Router(execution_mode=ExecutionMode.HEADLESS),
        registry=reg,
    )
    return eng, reg


def test_headless_run_target_completes_in_process(tmp_path) -> None:
    eng, reg = _headless_engine(tmp_path)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    status = Scheduler(eng, max_concurrent=1).run("r1", registry_runner(reg))

    assert status["tasks"]["t1"]["state"] == "completed"
    audit = status["lane_audit"]
    assert audit["clean"] is True
    # intake and publish run on the deterministic ENGINE lane (#389); the five model
    # stages are headless:claude.
    assert audit["by_lane"] == {"engine:none": 2, "headless:claude": 5}
    assert "headless:claude" in audit["sanctioned_lanes"]


def _trajectory(eng: Engine, run: str, task: str) -> list[tuple[str, str]]:
    t = eng.store.load_task(run, task)
    return [(s.value, t.stages[s].status.value) for s in STAGE_ORDER] + [("__task__", t.state.value)]


def test_conformance_interactive_equals_headless(tmp_path) -> None:
    # Same task spec, two modes differing ONLY by the Router; identical stage-DAG
    # trajectory + terminal disposition (non-deterministic fields excluded).
    di, dh = tmp_path / "i", tmp_path / "h"

    eng_i = Engine(StatusStore(di), CostLedger(di / "c.jsonl"), FakeProject())  # default = interactive
    eng_i.create_run("r")
    eng_i.add_task("r", "t1")
    while (w := eng_i.next_work("r", "t1")) is not None:
        eng_i.record("r", make_result(w))  # interactive simulated runner

    eng_h, reg = _headless_engine(dh)
    eng_h.create_run("r")
    eng_h.add_task("r", "t1")
    Scheduler(eng_h, max_concurrent=1).run("r", registry_runner(reg))

    assert _trajectory(eng_i, "r", "t1") == _trajectory(eng_h, "r", "t1")
