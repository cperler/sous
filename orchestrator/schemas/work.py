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
    Effort,
    ExecutionMode,
    ModelId,
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


class FinderSpec(BaseModel):
    """One independent finder lens in a multi-agent REVIEW plan (#73 design §1).

    Each finder is a fully engine-rendered prompt for a single lens
    (correctness/spec/design/tests); the runner dispatches them blind to each other and
    validates every sub-call against ``schema_ref`` (``review_findings``). ``agent`` is the
    persona the runner injects (None => the base reviewer persona)."""

    model_config = ConfigDict(frozen=True)

    lens: str
    prompt: str  # fully rendered by the engine, like WorkItem.prompt
    agent: str | None = None
    schema_ref: str


class ReviewPlan(BaseModel):
    """The engine-authored, runner-executed plan for a multi-agent REVIEW (#73 design §1).

    CONTENT, not routing: it is part of what the work *is* (which finders run, how findings
    are verified and deduped), so it folds into ``compute_content_hash`` and two dispatches
    with different finder sets are different work. It is explicitly NOT routing metadata like
    ``session_ref``/``cwd`` and must NOT join any content-hash exclusion set."""

    model_config = ConfigDict(frozen=True)

    finders: tuple[FinderSpec, ...]
    verify_template: str  # prompt template with mechanical slots the runner fills per finding
    verify_schema_ref: str
    dedupe_rule: str


class SubCall(BaseModel):
    """One model sub-call inside a single dispatch (#73 design §2/§4) — a finder or a
    verifier — so no model call is unattributed even below the WorkItem seam. The ledger
    writes one row per SubCall (priced from the engine model_table, never this self-report)."""

    model_config = ConfigDict(frozen=True)

    phase: str  # discriminator, e.g. "find:code" / "verify:3"
    model: ModelId
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_s: float
    session_id: str | None = None
    stream_file: str | None = None
    # Corrective schema-retries the transport spent inside THIS sub-call (#32 semantics, one
    # level down): a `_schema_retry_loop` run against one finder's output rides that finder's
    # own count, so its ledger row — not the dispatch, and not a sibling sub-call — carries the
    # extra turns (design §4). 0 on the first-try-valid path and on lanes without the loop.
    schema_retries: int = 0


def compute_content_hash(
    *,
    stage: Stage,
    prompt: str,
    schema_ref: str,
    model: ModelId,
    lane_policy: LanePolicy,
    attempt: int,
    effort: Effort | None = None,
    plan: ReviewPlan | None = None,
) -> str:
    """Idempotency key for a dispatch.

    Includes ``attempt`` so a retry (which mutates the prompt by appending
    learnings) never collides with the prior attempt's key, even in the
    degenerate case where the rendered prompt is byte-identical (target.md §4).

    ``effort`` (#96) is part of a dispatch's identity exactly like ``model`` — the same
    prompt at a different reasoning effort is a different call. It is appended ONLY when
    set, so an effort-less dispatch hashes byte-identically to the pre-#96 formula and an
    in-flight pre-#96 lease still verifies on record.

    ``plan`` (#73) is CONTENT, folded the same append-only way: a REVIEW dispatch's finder
    set is part of what the work *is*, so two plans with different finders yield different
    hashes. It is appended ONLY when set — a plan-less dispatch hashes byte-identically to
    the pre-#73 formula WITHOUT any exclusion (plan is NOT routing metadata like
    session_ref/cwd and must never join a content-hash exclusion set — design §1 Identity).
    """

    lane = f"{lane_policy.execution_mode.value}:{lane_policy.provider.value}"
    parts = [stage.value, prompt, schema_ref, model, lane, str(attempt)]
    if effort is not None:
        parts.append(effort)
    if plan is not None:
        parts.append(plan.model_dump_json())
    blob = "\x1f".join(parts)
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
    # #161/#202: an OPEN ``ModelId`` newtype over str (not a closed enum) — a retired id
    # still loads. Serializes/hashes byte-identically to the bare-str shape.
    model: ModelId
    # Reasoning effort this dispatch runs at (#96): an Effort value ("low"/"medium"/"high"),
    # or None for the provider default (every pre-#96 dispatch). Routing CONTENT like
    # ``model`` — it is folded into content_hash — translated per execution adapter
    # (claude ``--effort``, codex ``model_reasoning_effort``); deterministic ENGINE-lane
    # stages never carry one. #161/#202: tightened from ``str | None`` to ``Effort | None``
    # — pydantic coerces a bare "high" to Effort.HIGH at construction; StrEnum serializes as
    # its value, so the stored shape is unchanged (no SCHEMA_VERSION bump).
    effort: Effort | None = None
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
    # Checkpoint protocol (design pass §3) — both engine-derived bookkeeping, excluded
    # from content_hash like timeout_s/cwd/session_ref:
    #   checkpoint_tag: the tag the runner-side wrapper creates at HEAD after a
    #     successful git-affecting stage (`git tag -f` — a crash between tag and record
    #     re-runs the stage and overwrites it; git state never short-circuits the
    #     state machine).
    #   reset_to: the last-good checkpoint tag; the wrapper hard-resets the worktree to
    #     it BEFORE dispatch (retry/crash-resume only), so attempts never inherit a
    #     failed attempt's debris.
    checkpoint_tag: str | None = None
    reset_to: str | None = None
    # Salvage protocol (#59) — the last-good checkpoint tag the runner-side wrapper diffs
    # (``<salvage_anchor>..HEAD``) on a FAILED/TIMED-OUT result to report any commits the
    # attempt made past it, so the engine can KEEP that work for the retry instead of
    # resetting it away. Always populated on a checkpoint stage (independent of reset_to,
    # which is only set on a discard); engine-derived bookkeeping, excluded from
    # content_hash like checkpoint_tag/reset_to. None off a checkpoint stage / no anchor.
    salvage_anchor: str | None = None
    # Read-only task context the deterministic ENGINE-lane runners read STRUCTURALLY
    # (the folded context plane — state_machine CONTEXT_KEYS — plus the few task fields
    # deterministic TEST/DELIVER need: baseline_failures, issue_number, title, pr_url).
    # It is the SAME durable state the prompt is rendered from, so it is excluded from
    # content_hash like cwd/session_ref/checkpoint_tag. The model lanes ignore it (they
    # read the rendered prompt); it is populated only for deterministic stages, None
    # otherwise. NEVER a channel for correctness the prompt lacks — a convenience so a
    # script doesn't have to re-parse its own rendered prompt.
    context: dict | None = None
    # Extra environment variables to export into the stage's subprocess (#5: per-task port
    # block — ORCHESTRATOR_PORT_BASE/COUNT/PORT + any project-specific names). The runner
    # merges these OVER the inherited process env when it shells the model CLI / test
    # commands, so parallel worktrees don't collide on fixed dev/test-server ports. Engine-
    # derived from the same durable task context the prompt is (the folded port_base), so —
    # like cwd/context — it is excluded from content_hash: it never changes a dispatch's
    # identity. None (the default) means "inherit the process env unchanged".
    env: dict[str, str] | None = None
    # Multi-agent REVIEW plan (#73 design §1): the finder set + verify/dedupe rules the
    # runner executes below the seam. CONTENT, not routing — folded into content_hash so
    # two dispatches with different finder sets are different work; explicitly NOT excluded
    # like session_ref/cwd/context. None (the default) is a plan-less dispatch, which hashes
    # byte-identically to the pre-#73 formula.
    plan: ReviewPlan | None = None
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
        model: ModelId,
        lane_policy: LanePolicy,
        created_at: str,
        effort: Effort | None = None,
        agent: str | None = None,
        attempt: int = 0,
        timeout_s: int | None = None,
        cwd: str | None = None,
        session_ref: str | None = None,
        checkpoint_tag: str | None = None,
        reset_to: str | None = None,
        salvage_anchor: str | None = None,
        context: dict | None = None,
        env: dict[str, str] | None = None,
        plan: ReviewPlan | None = None,
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
                effort=effort,
                plan=plan,
            ),
            effort=effort,
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
            checkpoint_tag=checkpoint_tag,
            reset_to=reset_to,
            salvage_anchor=salvage_anchor,
            context=context,
            env=env,
            plan=plan,
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
    # the model id the runner used — priced by the single model_table. #161/#202: an OPEN
    # ``ModelId`` newtype (not a closed enum) so a result naming a retired id still loads.
    model: ModelId
    # The reasoning effort the dispatch ran at (#96), echoed from the WorkItem so the
    # cost-ledger row and stage events can attribute effort alongside model. Pure audit
    # metadata — it never feeds a verdict or a transition. None on effort-less dispatches
    # (every pre-#96 result) and the deterministic ENGINE lane. #161/#202: tightened from
    # ``str | None`` to ``Effort | None`` (StrEnum serializes as its value — stored shape
    # unchanged).
    effort: Effort | None = None
    status: ResultStatus
    structured_output: dict | None = None
    raw_output: str | None = None
    lane_used: LaneUsed
    # The provider session the runner used or created (design pass §2) — absorbed by
    # the engine on SUCCESS and threaded into the task's next WorkItem. None on
    # providers/lanes without session support.
    session_ref: str | None = None
    # {"tag": ..., "sha": ...} stamped by the runner-side checkpoint wrapper after a
    # successful git-affecting stage (design pass §3); absorbed into
    # task.last_checkpoint. None when the stage doesn't checkpoint or tagging failed
    # (fail-open: a missing checkpoint only means no reset anchor later).
    checkpoint: dict | None = None
    # Salvage report (#59): committed work the failed/timed-out attempt made past the
    # last checkpoint — ``{"anchor", "count", "commits": [{"sha", "subject"}, ...]}`` —
    # stamped by the runner-side wrapper (a pure git read of ``anchor..HEAD``). None when
    # the stage succeeded, made no commits past the anchor, or has no anchor. The engine
    # reads it to decide, by failure KIND, whether to keep that work for the retry.
    salvage: dict | None = None
    # How many corrective schema-retries the transport spent salvaging a malformed structured
    # output before this result (headless×claude schema-validate-and-retry, #32). 0 on the
    # first-try-valid path (the common case) and on lanes without the loop. Pure audit metadata
    # — like session_ref/checkpoint it rides RawResult -> StageResult and is recorded on the
    # cost-ledger row; it never feeds a verdict or a state transition.
    schema_retries: int = 0
    # Paths (relative to the run root) the runner teed this call's FULL raw provider stdout /
    # stderr to, under the per-stage log dir — ``{"stream": ..., "stderr": ...}`` (or
    # ``{"error": ...}`` when the best-effort tee failed) (#56). Pure audit metadata: it lets a
    # human jump from a recorded failure to the raw stream; it never feeds a verdict or a
    # transition. None on lanes without a provider stream (interactive/ENGINE) or when no
    # teeing wrapper is installed.
    stream_files: dict | None = None
    # The stage persona the codex transport injected into the task worktree's AGENTS.md for this
    # dispatch (#74 codex persona parity) — ``{"agent", "path"}`` (or ``{"agent", "error"}`` on a
    # best-effort failure). Pure audit metadata: it rides RawResult -> StageResult -> the stage
    # log (like ``stream_files``), so a human can see which persona a codex-routed stage ran with;
    # it never feeds a verdict or a transition. None on the claude lane (persona arrives via the
    # CLI's ``--agent``) and when no agent resolved.
    persona_injected: dict | None = None
    # Multi-agent REVIEW sub-call output (#73 design §2), populated only by a plan-bearing
    # dispatch:
    #   sub_results: the raw, UNFOLDED panel output ``{findings_by_lens, verdicts}`` the
    #     engine's deterministic synthesis fold consumes at record() to produce canonical
    #     review.json. None on every non-workflow dispatch.
    #   sub_calls: one SubCall per model call inside the dispatch (each finder / verifier),
    #     so no model call is unattributed below the seam — the ledger writes one row per
    #     SubCall. None on single-call dispatches.
    sub_results: dict | None = None
    sub_calls: tuple[SubCall, ...] | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    pricing_ref: str | None = None
    error: str | None = None
    completed_at: str  # ISO-8601 UTC

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.SUCCESS
