"""Registry assembly + the registry-backed runner (target.md §4 Phase 4).

``build_registry`` wires the cells the engine sanctions for a run; ``registry_runner``
turns a registry into a Scheduler ``Runner`` so the scheduler drives the engine fully
in-process for the headless lane (the headless run target). The interactive×claude
cell stays external (served by the Workflow shim), so a registry-backed runner only
dispatches in-process cells (headless/codex) — exactly the headless/codex modes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from adapters.project.base import ProjectConfig
from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import StageResult, WorkItem

from .base import SUPPORTED, CapabilityDescriptor, Registry
from .codex import CodexRunner, SchemaProvider
from .headless_claude import HeadlessClaudeRunner
from .transport import (
    Transport,
    checkpointing_transport,
    claude_cli_transport,
    codex_cli_transport,
    stream_teeing_transport,
)


def _schema_json_provider(schema_for: SchemaProvider) -> Callable[[str], str | None]:
    """Adapt a project's ``schema_for(ref) -> dict`` into the inline-JSON callable
    ``claude_cli_transport`` needs for ``--json-schema`` (the CLI takes the schema JSON
    itself, not a path). Serialized once per ref and cached. Without this the headless lane
    never sends a schema, so ``claude -p`` answers in prose and every stage is a
    SCHEMA_VIOLATION (the codex lane already gets its schema provider; this is its
    headless×claude twin)."""
    cache: dict[str, str | None] = {}

    def json_for(ref: str) -> str | None:
        if ref not in cache:
            schema = schema_for(ref) if schema_for is not None else None
            cache[ref] = json.dumps(schema) if schema is not None else None
        return cache[ref]

    return json_for


def build_registry(
    *,
    headless_transport: Transport | None = None,
    headless_schema_provider: SchemaProvider | None = None,
    codex_transport: Transport | None = None,
    codex_schema_provider: SchemaProvider | None = None,
    setup_project: ProjectConfig | None = None,
    include_interactive: bool = True,
    run_log_root: str | Path | None = None,
) -> Registry:
    """Registry covering interactive×claude (external) + headless×claude + any×codex +
    the deterministic ENGINE lane.

    ``headless_schema_provider`` wires the project's stage schemas into the real
    headless×claude transport (``--json-schema``) so structured output is actually
    enforced; it is ignored when an explicit ``headless_transport`` is injected (tests
    wrap their own). The schema-wired transport keeps the checkpoint/reset protocol.

    ``setup_project`` (the ProjectConfig) wires the in-process DeterministicSetupRunner
    for the ENGINE lane (intake worktree/baseline, no model call). When omitted the
    ENGINE cell is still registered as a sanctioned descriptor (for the lane audit) but
    has no runner — real deterministic dispatch needs the project.

    ``run_log_root`` (the run's status/log dir) wires the #56 stream-teeing wrapper around
    the auto-built provider transports, so each headless/codex call's full raw stdout/stderr
    is retained under ``stages/<task>/`` for post-mortems. It is ignored for an explicitly
    injected transport (tests wrap their own) and when None (no run dir — no teeing).
    """
    reg = Registry()
    if include_interactive:
        reg.register_external(
            CapabilityDescriptor(
                execution_mode=ExecutionMode.INTERACTIVE,
                provider=Provider.CLAUDE,
                in_process=False,
                schema_enforced=True,
                status=SUPPORTED,
            )
        )
    if headless_transport is None and headless_schema_provider is not None:
        # #66: pass the run dir INTO the transport so it streams `stream-json` and tees stdout
        # line-by-line as it arrives (in-flight tailable), not after the call returns. The
        # stream_teeing wrapper stays as the post-hoc fallback: it passes through untouched once
        # the transport has already streamed+stamped stream_files (#56 evidence retention).
        inner = claude_cli_transport(
            _schema_json_provider(headless_schema_provider), run_log_root=run_log_root
        )
        if run_log_root is not None:
            inner = stream_teeing_transport(inner, run_log_root)
        headless_transport = checkpointing_transport(inner)
    reg.register_runner(HeadlessClaudeRunner(headless_transport))
    if codex_transport is None and (codex_schema_provider is not None or run_log_root is not None):
        # Mirror the headless×claude wiring: thread the project's stage schema into the codex
        # transport so it enforces `--output-schema` AND runs the schema-validate-and-retry loop
        # where a corrective follow-up can be issued (#21 parity). codex `--json` is already a
        # JSONL stream, so passing the run dir tees it live (#66, the claude twin). Ignored for
        # an explicitly injected transport (tests wrap their own).
        codex_schema_json = (
            _schema_json_provider(codex_schema_provider) if codex_schema_provider else None
        )
        inner = codex_cli_transport(codex_schema_json, run_log_root=run_log_root)
        if run_log_root is not None:
            inner = stream_teeing_transport(inner, run_log_root)
        codex_transport = checkpointing_transport(inner)
    reg.register_runner(CodexRunner(codex_transport, codex_schema_provider))
    if setup_project is not None:
        from .deterministic_setup import DeterministicSetupRunner

        reg.register_runner(DeterministicSetupRunner(setup_project))
    else:
        reg.register_external(
            CapabilityDescriptor(
                execution_mode=ExecutionMode.ENGINE,
                provider=Provider.NONE,
                in_process=True,
                schema_enforced=True,
                status=SUPPORTED,
            )
        )
    return reg


def registry_runner(registry: Registry, *, max_workers: int | None = None):
    """A Scheduler Runner that dispatches each WorkItem via its cell's in-process runner.

    Raises (via Registry.resolve) if a WorkItem targets an external cell (interactive),
    which is the correct failure: headless drive can't run an interactive lane. The
    batch is dispatched concurrently (runners are subprocess-bound; the batch size is
    already capped upstream by the engine's capacity dispatch_limit), order preserved.
    """

    def run(workitems: list[WorkItem]) -> list[StageResult]:
        if not workitems:
            return []
        # Resolve first so an external/missing cell raises before any work starts.
        pairs = [(w, registry.resolve(w.lane_policy)) for w in workitems]
        workers = max_workers or len(pairs)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(runner.dispatch, w) for (w, runner) in pairs]
            return [f.result() for f in futures]

    return run
