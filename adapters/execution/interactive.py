"""Interactive x claude lane — the StageResult-construction contract (target.md §4).

The actual model call happens in the in-session Workflow shim (JS), which has no
filesystem: it calls ``agent()`` and RETURNS StageResults to the supervisor, which
persists them via ``orchestrator record``. This module is the Python mirror of the
shim's result mapping so the contract is unit-tested and documented in one place.
"""

from __future__ import annotations

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage, WorkItem

LANE = LaneUsed(
    execution_mode=ExecutionMode.INTERACTIVE,
    provider=Provider.CLAUDE,
    invocation="agent()",  # overridden per call with the concrete model
)


def build_stage_result(
    *,
    work_item: WorkItem,
    structured_output: dict | None,
    usage: TokenUsage,
    completed_at: str,
    raw_output: str | None = None,
    error: str | None = None,
) -> StageResult:
    """Map an in-session ``agent()`` outcome into a StageResult.

    Status policy (interactive x claude): an explicit error -> FAILURE; a missing
    structured output (the schema-enforced ``agent()`` returned nothing usable) ->
    SCHEMA_VIOLATION; otherwise SUCCESS. The lane is always interactive:claude, so
    the cost ledger attributes the call correctly (no hidden ``claude -p``).
    """

    if error:
        status = ResultStatus.FAILURE
    elif structured_output is None:
        status = ResultStatus.SCHEMA_VIOLATION
    else:
        status = ResultStatus.SUCCESS

    return StageResult(
        work_item_id=work_item.id,
        content_hash=work_item.content_hash,
        run_id=work_item.run_id,
        task_id=work_item.task_id,
        stage=work_item.stage,
        attempt=work_item.attempt,
        model=work_item.model,
        effort=work_item.effort,  # #96: echoed for the ledger row / stage events (audit)
        status=status,
        structured_output=structured_output,
        raw_output=raw_output,
        error=error,
        lane_used=LaneUsed(
            execution_mode=ExecutionMode.INTERACTIVE,
            provider=Provider.CLAUDE,
            invocation=f"agent(model={work_item.model})",
        ),
        token_usage=usage,
        completed_at=completed_at,
    )
