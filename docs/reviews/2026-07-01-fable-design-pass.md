# Design pass — reserved items from the 2026-07-01 review (Fable)

Companion to `2026-07-01-fable.md`. These are the three items explicitly reserved out of
the Opus execution pass (Phases A–B), designed here so implementation later is execution,
not invention. All three assume Phase B (task context plane, `WorkItem.cwd`) has landed;
none of them block Phase B. References are to symbols, not line numbers — the code is
moving under this doc.

**Suggested build order:** §1 (pipeline schema) → §2 (session continuity) → §3
(checkpoints). §1 first because it's the schema bump and every week of delay adds
migration surface; §3 last because it wants both `WorkItem.cwd` (Phase B) and the
context plane's absorb path. §4 (`blocked_on_human`) should ride the same schema bump
as §1 — two `SCHEMA_VERSION` bumps in one month is one too many.

---

## 1. Stage enum → per-task pipeline (the "vocabulary, not sequence" migration)

**Problem.** The linear 6-stage shape is load-bearing in three places: `STAGE_ORDER`
(enums), `LANE_STAGES` (enums), and `state_machine._active_sequence`, which resolves a
task's sequence by lane lookup. Decomposition/design/gate/docs node types don't want
`intake→scope→implement→test→deliver→review`, and today they can't opt out without a
schema rewrite.

**Design.**

- `Stage` stays a `StrEnum` — it becomes the *vocabulary* of dispatchable stage kinds
  (each with a `StageSpec`: schema_ref, model role, template). New node types later add
  vocabulary entries (e.g. `DECOMPOSE`, `GATE`); that is additive, not a reshape.
- `Task` gains `pipeline: tuple[Stage, ...]` — the ordered stage list *this task* runs.
  Persisted in the task doc. Set once at `Engine.add_task` and immutable afterward
  (mutating a pipeline mid-run would invalidate the resume cursor's meaning).
- `ExecutionLane` survives as a **preset**, not a mechanism: `add_task` resolves
  `lane → LANE_STAGES[lane]` and stores the result as `task.pipeline`. CLI/UX unchanged.
  A future decomposer passes an explicit pipeline instead of a lane.
- `state_machine._active_sequence(task)` returns `task.pipeline`. `plan_lane` (rename to
  `plan_pipeline`) marks stages *not in the pipeline* as SKIPPED — same idempotent logic,
  keyed off the task's own list instead of the global table. `next_stage`, `resume_point`,
  `is_done` need no semantic change.
- `Task.stages` stays keyed by **all** vocabulary stages (off-pipeline records sit at
  SKIPPED). This keeps the status-file shape stable and the migration one field, at the
  cost of a few dead rows per doc. Revisit only if the vocabulary grows past ~12.
- `STAGE_ORDER` shrinks to one duty: the default FULL pipeline definition. Nothing else
  may import it for sequencing (grep-audit: `state_machine`, `render`, tests).

**Validation at `add_task`:** pipeline must be non-empty, duplicate-free, and a subset of
the vocabulary. Do **not** enforce "must be a subsequence of STAGE_ORDER" — that would
re-bake the linear shape this change exists to remove. Order is the caller's intent.

**Migration.** Bump `SCHEMA_VERSION` to "2". Loader shim in `StatusStore.load_task`: a
v1 doc without `pipeline` gets `LANE_STAGES[task.execution_lane]` stamped in. One-line
default, no data rewrite. `execution_lane` stays on the doc for provenance/rendering.

**Tests.** (a) v1 doc loads and round-trips with the derived pipeline; (b) a custom
2-stage pipeline (`SCOPE, REVIEW`) runs end-to-end through the fake transport with the
other four stages SKIPPED; (c) resume mid-custom-pipeline lands on the right stage;
(d) `add_task` rejects empty/duplicate/unknown-stage pipelines; (e) the interactive≡
headless conformance test re-run over a non-FULL pipeline.

**Explicitly not now:** heterogeneous node types, per-stage schemas beyond the existing
vocabulary, conditional/branching pipelines. The review's instruction is to make the
*schemas* stop assuming linearity — not to build the graph-shaped pipeline engine.

---

## 2. Per-task session continuity on the headless lane (`session_ref`)

**Problem.** Every WorkItem cold-starts a fresh agent (`workflow_shim.js` and both CLI
transports); implement never sees scope's reasoning except through the re-rendered
summary; the prompt-cache thesis has no mechanism. The fix is transport-layer: chain a
task's stages through one provider session.

**Design.**

- `StageResult` gains `session_ref: str | None` — the provider session id the runner
  used or created. The claude transport reads it from `claude -p --output-format json`
  (the payload carries `session_id`); the codex transport reports None until codex grows
  an equivalent (do not block on it).
- The engine absorbs `session_ref` into the task (via the Phase B context plane — it is
  exactly the kind of stage output `_absorb_outputs`' generalization exists to carry)
  and threads it into the next `WorkItem` as `session_ref: str | None`.
- The claude transport, given a `session_ref`, resumes (`claude -p --resume <id>`)
  instead of cold-starting. On a resume failure that looks like *session-not-found /
  expired* (and only that), it falls back to a fresh session **within the same
  dispatch** and reports the new id. Any other error fails the dispatch normally.

**The invariant that makes this safe (write it into the docstring):**

> A session ref is routing metadata. The rendered prompt MUST remain fully
> self-contained; continuity may only make a stage cheaper or richer, never correct.

Concretely: `render_prompt` output does not change based on `session_ref`;
`compute_content_hash` does **not** include it (same reasoning as lane fallback — it's
how the work runs, not what the work is); a crash-resume that has lost the session
simply cold-starts. Correctness never depends on continuity, so the engine stays
deterministic and resume stays trivial.

**Retry semantics (decision, not default-drift):** reuse the session across *successful*
stage transitions only. A retry after FAILURE gets a **fresh** session — the engine
clears the stored ref when a stage fails. Rationale: a failed attempt's context is as
likely poisoned as useful, and learnings already carry the distilled failure forward.
This is the conservative call; if the eval bench (review innovation #2) later shows
warm-retry wins, flip it there, with evidence.

**Interaction with §1/§3:** none structural. Sessions chain whatever pipeline the task
has; checkpoint resets (§3) are worktree state, orthogonal to conversation state.

**Cost measurement caveat:** this is the change that makes `cache_read` rows meaningful
— but only if the shim/transport usage capture actually works (review: it likely records
zeros on the default lane). Land the measurement-integrity fix first or the win will be
invisible.

**Tests.** Fake transport that records `session_ref` per call: (a) stage N+1 receives
stage N's ref; (b) failure → next dispatch has no ref; (c) session-not-found fallback
produces a fresh ref and a SUCCESS StageResult; (d) content_hash identical with/without
ref; (e) resume-after-crash dispatches cleanly with no ref.

---

## 3. Stage-commit checkpoint protocol

**Problem.** Attempts are not idempotent: a failed implement leaves debris the retry
inherits silently, and the DEFERRED committed-work/timeout-recovery row is this problem
in special-case clothing. Make git effects part of the contract.

**Design — who does what (the split that keeps the engine pure):**

- **The runner/transport wrapper** (execution adapter, not the model, not the engine)
  owns all git I/O, operating in `WorkItem.cwd`:
  - *Before dispatch*, if the WorkItem carries `reset_to`, hard-reset the worktree to
    that ref (`git reset --hard <ref> && git clean -fd`, scoped to the task worktree —
    which is precisely why this waits for `WorkItem.cwd`).
  - *After a SUCCESS raw result* in a git-affecting stage, create tag
    `task/<task_id>/<stage>/<attempt>` at HEAD and stamp `checkpoint: {tag, sha}` into
    the StageResult. Tag creation is deterministic bookkeeping — models are unreliable
    at it and the engine must not do I/O.
- **The engine** stays pure: it absorbs `checkpoint` into the task context (Phase B
  absorb path) and, when building a WorkItem for a retry (attempt > 0) or a
  crash-resume of a git-affecting stage, sets `reset_to` = the checkpoint of the most
  recent *successful* stage. Both fields are plain data; determinism is untouched.
- **Which stages are git-affecting:** a boolean on `StageSpec` (e.g. `checkpoint=True`
  for INTAKE, IMPLEMENT, TEST, DELIVER; False for SCOPE, REVIEW). This is vocabulary
  metadata, consistent with §1.

**Resume semantics:** never skip a stage because its tag exists. A crash after
commit-and-tag but before `record` re-runs the stage; the pre-dispatch reset makes the
re-run safe (it starts from the prior checkpoint, not the orphaned work). The orphaned
tag for the same attempt is overwritten (`git tag -f`). Trusting git state to
short-circuit the state machine would put correctness outside the durable artifacts —
exactly what the moat forbids.

**Failure modes to decide up front:**
- *Dirty-but-valuable state:* `git clean -fd` deletes untracked files a failed attempt
  created. That is the point (idempotent retries), but it must run **only** inside a
  task-scoped worktree — assert the cwd is not the repo root before any destructive op.
- *Tag namespace collisions across runs:* include the run id if the same task can recur
  (`task/<run_id>/<task_id>/...`) — decide once, it's in the contract string.
- *Cleanup:* keep tags at run finalize (they are the audit trail the ledger can anchor
  to); a `gc` CLI verb can prune terminated runs later.

**Retires:** the DEFERRED committed-work/timeout-recovery row (this is its general
form) — re-disposition it when this lands.

**Tests.** With a scratch git repo fixture: (a) success ⇒ tag exists at HEAD and
StageResult carries the sha; (b) failed implement leaving tracked+untracked debris ⇒
retry WorkItem carries `reset_to` and the wrapper leaves a clean tree at the checkpoint;
(c) crash between tag and record ⇒ re-run overwrites the attempt tag, no skip;
(d) refusal to reset when cwd is the repo root.

---

## 4. Rider: `blocked_on_human` as a task state (same schema bump as §1)

The review's "missing #3": the HARD CHECKPOINT is prose, not mechanism. Minimal
mechanization, designed now because it belongs in the §1 `SCHEMA_VERSION` bump:

- `TaskState.BLOCKED_ON_HUMAN` — non-terminal; the scheduler treats it like BLOCKED
  (not dispatchable) but nothing upstream of it cascades. Excluded from
  `TERMINAL_TASK_STATES`.
- An approval artifact in the store: `approval-<task_id>.json` `{approved_by, at, what}`
  — written by a human-facing CLI verb (`orchestrator approve <run> <task>`), checked by
  the engine before the gated transition. The *artifact* is the gate; prose stays as
  documentation.
- First consumers: the decomposition front door (approve the emitted graph before
  `create_task` fans out) and any deliver stage that would push/PR to a real repo.

Full approval-workflow design belongs with the decomposition build; only the enum value,
the terminal-set exclusion, and the artifact shape need to land with the schema bump.

---

## Open questions for Craig (blocking none of the above starts)

1. §1: keep `execution_lane` on the Task doc indefinitely (provenance), or deprecate
   once callers pass pipelines? (Doc assumes: keep.)
2. §2: warm-retry (reuse session after failure) is OFF by default — agree, or want it
   flag-gated from day one?
3. §3: tag namespace — include run_id or not? (Doc leans yes if tasks can recur across
   runs, e.g. bench replays.)
