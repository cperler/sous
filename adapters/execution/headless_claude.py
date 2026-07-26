"""Headless × claude in-process runner (target.md §4 — the always-works lane).

Drives the model via ``claude -p`` (or the Agent SDK) as a subprocess and returns a
StageResult. Unlike the interactive shim, this runs in-process, so the engine/scheduler
can dispatch it directly (the headless run target). Under the anticipated billing
change this lane bills credit/at API rates — a cost property of the (headless, claude)
cell, not a prohibition.

It is also the reference lane for the multi-agent REVIEW workflow (#73): a plan-bearing
WorkItem is handed to ``review_panel.run_review_panel``, which fans the plan out into finder
and verifier sub-calls over this same transport and returns one StageResult.
"""

from __future__ import annotations

from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import StageResult, WorkItem

from .base import SUPPORTED, CapabilityDescriptor
from .review_panel import run_review_panel
from .transport import (
    RawResult,
    Transport,
    checkpointing_transport,
    classify_raw,
    claude_cli_transport,
    to_stage_result,
)


class HeadlessClaudeRunner:
    def __init__(self, transport: Transport | None = None) -> None:
        # The real transport gets the checkpoint protocol (design pass §3); an
        # injected transport is the caller's choice (tests wrap explicitly).
        self._transport = transport or checkpointing_transport(claude_cli_transport())

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                execution_mode=ExecutionMode.HEADLESS,
                provider=Provider.CLAUDE,
                in_process=True,
                schema_enforced=True,  # via --json-schema
                # #73: the reference lane for a multi-agent REVIEW — the transport can fan a
                # plan's finders/verifiers out as sub-calls within one dispatch (design §2/§5).
                supports_plan=True,
                status=SUPPORTED,
            )
        ]

    def dispatch(self, work: WorkItem) -> StageResult:
        # #73: a plan-bearing dispatch fans out below the seam (finders → dedupe → adversarial
        # verify) and returns one StageResult carrying sub_results/sub_calls. Everything else
        # takes the single-call path below, byte-for-byte as before — the plan-less path is the
        # permanent fallback, not scaffolding.
        if work.plan is not None:
            return run_review_panel(work, self._transport)
        raw: RawResult = self._transport(work)
        status = classify_raw(raw)  # engine retries RATE_LIMITED on a cheaper model
        return to_stage_result(work, raw, status, mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
