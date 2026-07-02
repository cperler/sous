"""Registry assembly + the registry-backed runner (target.md §4 Phase 4).

``build_registry`` wires the cells the engine sanctions for a run; ``registry_runner``
turns a registry into a Scheduler ``Runner`` so the scheduler drives the engine fully
in-process for the headless lane (the headless run target). The interactive×claude
cell stays external (served by the Workflow shim), so a registry-backed runner only
dispatches in-process cells (headless/codex) — exactly the headless/codex modes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import StageResult, WorkItem

from .base import SUPPORTED, CapabilityDescriptor, Registry
from .codex import CodexRunner, SchemaProvider
from .headless_claude import HeadlessClaudeRunner
from .transport import Transport


def build_registry(
    *,
    headless_transport: Transport | None = None,
    codex_transport: Transport | None = None,
    codex_schema_provider: SchemaProvider | None = None,
    include_interactive: bool = True,
) -> Registry:
    """Registry covering interactive×claude (external) + headless×claude + any×codex."""
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
    reg.register_runner(HeadlessClaudeRunner(headless_transport))
    reg.register_runner(CodexRunner(codex_transport, codex_schema_provider))
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
