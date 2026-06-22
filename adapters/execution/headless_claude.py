"""Headless × claude in-process runner (target.md §4 — the always-works lane).

Drives the model via ``claude -p`` (or the Agent SDK) as a subprocess and returns a
StageResult. Unlike the interactive shim, this runs in-process, so the engine/scheduler
can dispatch it directly (the headless run target). Under the anticipated billing
change this lane bills credit/at API rates — a cost property of the (headless, claude)
cell, not a prohibition.
"""

from __future__ import annotations

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .base import SUPPORTED, CapabilityDescriptor
from .transport import RawResult, Transport, claude_cli_transport, to_stage_result


class HeadlessClaudeRunner:
    def __init__(self, transport: Transport | None = None) -> None:
        self._transport = transport or claude_cli_transport()

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                execution_mode=ExecutionMode.HEADLESS,
                provider=Provider.CLAUDE,
                in_process=True,
                supports_streaming=False,
                schema_enforced=True,  # via --json-schema
                cost_metered=True,
                status=SUPPORTED,
            )
        ]

    def dispatch(self, work: WorkItem) -> StageResult:
        raw: RawResult = self._transport(work)
        if raw.exit_code != 0 or raw.error:
            status = ResultStatus.TIMEOUT if raw.exit_code == 124 else ResultStatus.FAILURE
        elif raw.structured_output is None:
            status = ResultStatus.SCHEMA_VIOLATION
        else:
            status = ResultStatus.SUCCESS
        return to_stage_result(work, raw, status, mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
