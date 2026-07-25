"""Interactive lane result-builder contract (mirrors the JS workflow_shim)."""

from __future__ import annotations

from adapters.execution.interactive import build_stage_result
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, TokenUsage, WorkItem

POLICY = LanePolicy(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE)


def _wi() -> WorkItem:
    return WorkItem.create(
        id="wi-1",
        run_id="r1",
        task_id="#505",
        stage=Stage.IMPLEMENT,
        prompt="do it",
        schema_ref="implement",
        model="claude-opus-5",
        lane_policy=POLICY,
        created_at="2026-06-20T00:00:00Z",
    )


def test_success_when_structured_output_present() -> None:
    sr = build_stage_result(
        work_item=_wi(),
        structured_output={"committed": True},
        usage=TokenUsage(input=10, output=2),
        completed_at="2026-06-20T00:01:00Z",
    )
    assert sr.status is ResultStatus.SUCCESS
    assert sr.lane_used.execution_mode is ExecutionMode.INTERACTIVE
    assert sr.lane_used.provider is Provider.CLAUDE
    assert sr.model == "claude-opus-5"
    # the result echoes the WorkItem it answers (idempotency / contract validation)
    assert sr.work_item_id == "wi-1" and sr.content_hash == _wi().content_hash


def test_schema_violation_when_no_output() -> None:
    sr = build_stage_result(
        work_item=_wi(), structured_output=None, usage=TokenUsage(), completed_at="t"
    )
    assert sr.status is ResultStatus.SCHEMA_VIOLATION


def test_failure_on_error() -> None:
    sr = build_stage_result(
        work_item=_wi(),
        structured_output=None,
        usage=TokenUsage(),
        completed_at="t",
        error="agent crashed",
    )
    assert sr.status is ResultStatus.FAILURE and sr.error == "agent crashed"
