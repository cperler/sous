"""The execution-adapter seam (target.md §4): WorkItem + StageResult.

The engine emits a ``WorkItem`` and consumes a ``StageResult``. It never imports
a runner — execution modes communicate only through these two versioned artifacts,
which is what makes the modes interchangeable and a run resumable across a session
death. Every dispatch (interactive subagent, headless ``claude -p``, codex, or the
former one-shot path) produces exactly one StageResult, so an unattributed model
call is structurally impossible (closes as-built D6).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    SCHEMA_VERSION,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)


class LanePolicy(BaseModel):
    """The (mode, provider) cell the engine wants this WorkItem run on."""

    model_config = ConfigDict(frozen=True)

    execution_mode: ExecutionMode
    provider: Provider
    allow_fallback: bool = False


class LaneUsed(BaseModel):
    """The lane a runner actually used — ground truth for cost attribution."""

    model_config = ConfigDict(frozen=True)

    execution_mode: ExecutionMode
    provider: Provider
    invocation: str  # literal call string, e.g. "agent(model=...)" / "claude -p ..."


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


def compute_content_hash(
    *,
    stage: Stage,
    prompt: str,
    schema_ref: str,
    model: str,
    lane_policy: LanePolicy,
    attempt: int,
) -> str:
    """Idempotency key for a dispatch.

    Includes ``attempt`` so a retry (which mutates the prompt by appending
    learnings) never collides with the prior attempt's key, even in the
    degenerate case where the rendered prompt is byte-identical (target.md §4).
    """

    lane = f"{lane_policy.execution_mode.value}:{lane_policy.provider.value}"
    blob = "\x1f".join([stage.value, prompt, schema_ref, model, lane, str(attempt)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class WorkItem(BaseModel):
    """Engine -> runner. Immutable once emitted."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    id: str
    content_hash: str
    run_id: str
    task_id: str
    stage: Stage
    attempt: int = 0
    prompt: str  # fully rendered by the engine
    schema_ref: str  # key/path the runner fetches; the engine does not interpret it
    model: str
    agent: str | None = None  # persona the runner dispatches (from the project roster)
    lane_policy: LanePolicy
    timeout_s: int | None = None
    # Working directory the runner executes in — the task's worktree (folded from
    # intake's output). None => the runner's process CWD. Dispatch/environment metadata
    # (like timeout_s), NOT part of content_hash: it is derived from the same durable
    # state the prompt is, so it never changes a dispatch's identity.
    cwd: str | None = None
    # Provider session to resume (design pass §2). ROUTING METADATA, not content: the
    # rendered prompt MUST stay fully self-contained — continuity may only make a stage
    # cheaper or richer, never correct. Excluded from content_hash for the same reason
    # as timeout_s/cwd. A transport that cannot resume (codex today) ignores it; a lost
    # session falls back to a fresh one inside the same dispatch.
    session_ref: str | None = None
    created_at: str  # ISO-8601 UTC; stamped by the engine

    @classmethod
    def create(
        cls,
        *,
        id: str,
        run_id: str,
        task_id: str,
        stage: Stage,
        prompt: str,
        schema_ref: str,
        model: str,
        lane_policy: LanePolicy,
        created_at: str,
        agent: str | None = None,
        attempt: int = 0,
        timeout_s: int | None = None,
        cwd: str | None = None,
        session_ref: str | None = None,
    ) -> WorkItem:
        """Build a WorkItem with its content_hash derived consistently."""

        return cls(
            id=id,
            content_hash=compute_content_hash(
                stage=stage,
                prompt=prompt,
                schema_ref=schema_ref,
                model=model,
                lane_policy=lane_policy,
                attempt=attempt,
            ),
            agent=agent,
            run_id=run_id,
            task_id=task_id,
            stage=stage,
            attempt=attempt,
            prompt=prompt,
            schema_ref=schema_ref,
            model=model,
            lane_policy=lane_policy,
            timeout_s=timeout_s,
            cwd=cwd,
            session_ref=session_ref,
            created_at=created_at,
        )


class StageResult(BaseModel):
    """Runner -> engine. The engine advances a task only after recording one."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    work_item_id: str
    content_hash: str
    run_id: str
    task_id: str
    stage: Stage
    attempt: int = 0
    model: str  # the model id the runner used — priced by the single model_table
    status: ResultStatus
    structured_output: dict | None = None
    raw_output: str | None = None
    lane_used: LaneUsed
    # The provider session the runner used or created (design pass §2) — absorbed by
    # the engine on SUCCESS and threaded into the task's next WorkItem. None on
    # providers/lanes without session support.
    session_ref: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    pricing_ref: str | None = None
    error: str | None = None
    completed_at: str  # ISO-8601 UTC

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.SUCCESS
