"""Registry assembly + the registry-backed runner (target.md §4 Phase 4).

``build_registry`` wires the cells the engine sanctions for a run; ``registry_runner``
turns a registry into a Scheduler ``Runner`` so the scheduler drives the engine fully
in-process for the headless lane (the headless run target). The interactive×claude
cell stays external (served by the Workflow shim), so a registry-backed runner only
dispatches in-process cells (headless/codex) — exactly the headless/codex modes.
"""

from __future__ import annotations

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
                supports_streaming=True,
                schema_enforced=True,
                status=SUPPORTED,
            )
        )
    reg.register_runner(HeadlessClaudeRunner(headless_transport))
    reg.register_runner(CodexRunner(codex_transport, codex_schema_provider))
    return reg


def registry_runner(registry: Registry):
    """A Scheduler Runner that dispatches each WorkItem via its cell's in-process runner.

    Raises (via Registry.resolve) if a WorkItem targets an external cell (interactive),
    which is the correct failure: headless drive can't run an interactive lane.
    """

    def run(workitems: list[WorkItem]) -> list[StageResult]:
        out: list[StageResult] = []
        for w in workitems:
            runner = registry.resolve(w.lane_policy)
            out.append(runner.dispatch(w))
        return out

    return run
