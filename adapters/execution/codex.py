"""Codex in-process runner (target.md §4 — the codex provider, fix-forward §2 #5).

Drives ``codex exec`` as a subprocess and bills OpenAI. The headline fix-forward
vs. the as-built: success requires **FULL JSON-Schema validation** of the structured
output, not the as-built "top-level required keys present" heuristic — so codex can
be a primary route without silently accepting malformed output. Codex is always
headless (codex×interactive is an honest empty cell).
"""

from __future__ import annotations

from collections.abc import Callable

from jsonschema import Draft202012Validator

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .base import EXPLICIT_EMPTY, SUPPORTED, CapabilityDescriptor
from .transport import (
    RawResult,
    Transport,
    checkpointing_transport,
    codex_cli_transport,
    is_provider_unavailable,
    is_rate_limited,
    to_stage_result,
)

# schema_ref -> JSON Schema dict (or None if unknown -> can't full-validate).
SchemaProvider = Callable[[str], dict | None]


class CodexRunner:
    def __init__(
        self, transport: Transport | None = None, schema_provider: SchemaProvider | None = None
    ) -> None:
        # The real transport gets the checkpoint protocol (design pass §3); an
        # injected transport is the caller's choice (tests wrap explicitly).
        self._transport = transport or checkpointing_transport(codex_cli_transport())
        self._schema_provider = schema_provider

    def capabilities(self) -> list[CapabilityDescriptor]:
        return [
            CapabilityDescriptor(
                execution_mode=ExecutionMode.HEADLESS,
                provider=Provider.CODEX,
                in_process=True,
                schema_enforced=True,
                # supports_plan stays False (#73 design §5): `codex exec` has no sub-agent
                # primitive, so this cell can't fan a REVIEW plan out below the seam. The
                # engine therefore never attaches one here and codex keeps dispatching the
                # single-reviewer prompt — lane and content_hash stay consistent.
                status=SUPPORTED,
            ),
            # codex never runs in-session — declare the empty cell honestly.
            CapabilityDescriptor(
                execution_mode=ExecutionMode.INTERACTIVE,
                provider=Provider.CODEX,
                in_process=False,
                status=EXPLICIT_EMPTY,
            ),
        ]

    def dispatch(self, work: WorkItem) -> StageResult:
        raw: RawResult = self._transport(work)
        status = self._verdict(work, raw)
        return to_stage_result(work, raw, status, mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)

    def _verdict(self, work: WorkItem, raw: RawResult) -> ResultStatus:
        if raw.exit_code != 0 or raw.error:
            if raw.exit_code == 124:
                return ResultStatus.TIMEOUT
            # The codex PROVIDER itself is out (CLI missing / auth expired) — checked before
            # the rate-limit/failure fallbacks so a persistently-unavailable provider surfaces
            # as PROVIDER_UNAVAILABLE, which the engine can cross-provider-fall-through on (#7)
            # instead of burning retries against a codex that will never answer.
            if is_provider_unavailable(raw):
                return ResultStatus.PROVIDER_UNAVAILABLE
            if is_rate_limited(raw):
                return ResultStatus.RATE_LIMITED  # engine retries on a cheaper model
            return ResultStatus.FAILURE
        # Output must be a JSON object (a list/scalar is not a valid stage result).
        if not isinstance(raw.structured_output, dict):
            return ResultStatus.SCHEMA_VIOLATION
        # TIGHTENED: full schema validation, not just required-keys-present.
        schema = self._schema_provider(work.schema_ref) if self._schema_provider else None
        if schema is not None and next(
            iter(Draft202012Validator(schema).iter_errors(raw.structured_output)), None
        ):
            return ResultStatus.SCHEMA_VIOLATION
        return ResultStatus.SUCCESS
