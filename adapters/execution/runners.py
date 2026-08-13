"""Registry assembly + the registry-backed runner (target.md §4 Phase 4).

``build_registry`` wires the cells the engine sanctions for a run; ``registry_runner``
turns a registry into a Scheduler runner (a streaming ``RegistryPool``, #318) so the
scheduler drives the engine fully in-process for the headless lane — recording each stage
as it completes instead of at its batch's barrier. The interactive×claude
cell stays external (served by the Workflow shim), so a registry-backed runner only
dispatches in-process cells (headless/codex) — exactly the headless/codex modes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from orchestrator.ports.execution import SUPPORTED, CapabilityDescriptor, Registry
from orchestrator.ports.project import ProjectConfig
from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import StageResult, WorkItem

from .codex import CodexRunner, SchemaProvider
from .headless_claude import HeadlessClaudeRunner
from .transport import (
    Transport,
    checkpointing_transport,
    claude_cli_transport,
    codex_cli_transport,
    stream_teeing_transport,
)

# JSON-Schema meta-keys the CLI's `--json-schema` validator cannot accept: it treats a
# top-level `$schema`/`$id` as a `$ref` to resolve and fails closed —
#   Error: --json-schema is not a valid JSON Schema: no schema with key or ref
#          "https://json-schema.org/draft/2020-12/schema"
# — which kills the dispatch at argv parsing, before any model call. Every canonical stage
# schema carries `$schema` (they are Draft 2020-12 documents), so WITHOUT this strip the
# headless lane cannot dispatch ANY stage (#282). The interactive lane has always done this
# in `run_targets/workflow_shim.js::sanitizeSchema`; this is its headless twin, and the two
# must keep stripping the same keys or the lanes diverge on what they will accept.
_SCHEMA_META_KEYS = ("$schema", "$id")


def _without_meta_keys(schema: dict) -> dict:
    """Top-level meta-keys removed; everything else (including nested `$ref`/`$defs`, which
    the validator resolves fine) untouched. Shallow by design — only the ROOT keys break it."""
    return {k: v for k, v in schema.items() if k not in _SCHEMA_META_KEYS}


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
            cache[ref] = json.dumps(_without_meta_keys(schema)) if schema is not None else None
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
                # #262: the Workflow shim executes the same finder/verifier contract as the
                # headless review-panel driver; #288 requires the flag to follow that behavior.
                supports_plan=True,
                # #302 (decided, not deferred again): stays False — `run_targets/
                # workflow_shim.js` calls `agent(prompt, {model, effort, agentType, schema})`,
                # which exposes NO tool restriction, so True would be the same silent
                # over-promise `supports_plan` already rules out. The degradation is no longer
                # just the warning event: `render_prompt` now states the posture IN-BAND for
                # this lane (`_unenforced_tool_posture_directive`), so the dispatch itself
                # differs. Flip to True in the SAME change that lands a tool option on
                # `agent()`, which also retires that directive.
                enforces_tool_policy=False,
                verifies_worktree_origin=False,
                # No claude CLI argv exists on this lane at all (the call is in-session), so
                # the permission posture is untranslatable here for the same reason. It keeps
                # the BYPASS default only because nothing reads it for an external cell.
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
    reg.register_runner(HeadlessClaudeRunner(headless_transport, review_project=setup_project))
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
    reg.register_runner(CodexRunner(
        codex_transport, codex_schema_provider, review_project=setup_project
    ))
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


class RegistryPool:
    """Registry-backed dispatch pool: submit WorkItems, harvest StageResults AS THEY COMPLETE.

    Implements the scheduler's ``StreamingRunner`` protocol over a persistent thread pool
    (runners are subprocess-bound, so threads are the right shape). Completion-ordered
    harvesting is the #318 fix: the previous ``[f.result() for f in futures]`` collected in
    SUBMISSION order inside a single batch call, so a stage that finished first was not even
    observed until the slowest one landed — and could not be, because the list-in/list-out
    signature had nowhere to put a partial answer.

    Also callable (``pool(workitems) -> list[StageResult]``, submission order preserved) so
    the narrow ``Runner`` form keeps working for direct callers and tests.

    Single-consumer: submit/harvest/close are called from the scheduler's own thread; only
    the dispatches themselves run concurrently (and they touch no engine state).
    """

    # The real cap on concurrent dispatches is the engine's capacity ``dispatch_limit``
    # (single digits); this is only the pool's ceiling, sized so it never becomes the
    # binding constraint. An explicit ``max_workers`` overrides it.
    DEFAULT_WORKERS = 32

    def __init__(self, registry: Registry, *, max_workers: int | None = None) -> None:
        self._registry = registry
        self._max_workers = max_workers or self.DEFAULT_WORKERS
        self._ex: ThreadPoolExecutor | None = None
        self._futures: list[Future[StageResult]] = []

    def submit(self, work: list[WorkItem]) -> None:
        if not work:
            return
        # Resolve first so an external/missing cell (interactive — headless drive can't run
        # it) raises before any work starts.
        pairs = [(w, self._registry.resolve(w.lane_policy)) for w in work]
        if self._ex is None:
            self._ex = ThreadPoolExecutor(
                max_workers=self._max_workers, thread_name_prefix="dispatch"
            )
        self._futures.extend(self._ex.submit(runner.dispatch, w) for (w, runner) in pairs)

    def pending(self) -> int:
        return len(self._futures)

    def harvest(self, *, block: bool = True) -> list[StageResult]:
        """Every result available now — waiting for the first completion when asked to
        block. A dispatch that raised re-raises here (same as the old ``f.result()``)."""
        if not self._futures:
            return []
        done = {f for f in self._futures if f.done()}
        if not done and block:
            done = set(wait(self._futures, return_when=FIRST_COMPLETED).done)
        if not done:
            return []
        harvested = [f for f in self._futures if f in done]  # submission order within a batch
        self._futures = [f for f in self._futures if f not in done]
        return [f.result() for f in harvested]

    def close(self) -> None:
        """Release the worker threads. Idempotent, and the pool is reusable afterwards (the
        executor is rebuilt lazily on the next submit) — the scheduler closes the pool it was
        handed on every exit path, including ones a caller may follow with another run.
        Never waits: an abandoned dispatch must not hang the driver on its way out."""
        ex, self._ex = self._ex, None
        self._futures = []
        if ex is not None:
            ex.shutdown(wait=False, cancel_futures=True)

    def __call__(self, workitems: list[WorkItem]) -> list[StageResult]:
        """The legacy list-in/list-out ``Runner`` form: dispatch the batch, drain it, and
        return the results in submission order."""
        self.submit(workitems)
        out: list[StageResult] = []
        while self.pending():
            out.extend(self.harvest(block=True))
        order = {w.id: i for i, w in enumerate(workitems)}
        return sorted(out, key=lambda r: order.get(r.work_item_id, len(order)))


def registry_runner(registry: Registry, *, max_workers: int | None = None) -> RegistryPool:
    """A Scheduler runner that dispatches each WorkItem via its cell's in-process runner.

    Returns a :class:`RegistryPool` — usable both as the streaming pool the scheduler
    prefers and as the old list-in/list-out callable.
    """
    return RegistryPool(registry, max_workers=max_workers)
