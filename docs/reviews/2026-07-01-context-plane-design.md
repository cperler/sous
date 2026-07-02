# Design note — task context plane: the folding bounds (2026-07-01)

Companion to `2026-07-01-fable.md`. Scope of THIS note is deliberately narrow: the
**bounding decisions** for generalizing `_absorb_outputs` into an engine-owned
`task.context` (Phase B step 5). It does not cover step 6 (render) or step 7 (cwd)
beyond what the bounds imply. Written for approval **before** the folding is implemented.

Invariants it must not break: engine stays project-agnostic and never calls a model;
correctness never depends on session state (context is derived only from durable
StageResults and is reconstructible on replay).

---

## 1. What gets folded vs. dropped — an explicit engine-owned whitelist

We do **not** fold whole `structured_output` blobs. Each stage contributes only a
fixed, engine-owned set of keys (the generic stage-contract fields, present in the
canonical `schemas/stages/*.json` — so nothing project-specific leaks into the engine's
context, and a project adding `additionalProperties` can't bloat or poison it).

| Stage | Folded into `task.context` | Dropped (stays in the stage record only) |
|---|---|---|
| intake | `branch`, `worktree` | `baseline_captured`, `baseline` note |
| scope | `plan` (list), `blocked_reason` | `feasible` (drives no downstream prompt) |
| implement | `files_changed`, `summary` | `committed` |
| test | `failures`, `tests_meaningful`, `validation_notes` | `passed` |
| deliver | `pr_number`, `pr_url` | — |
| review | `issues` | `approved` |

Rationale: fold what a *downstream* stage needs to not re-derive (implement needs
scope's `plan`; review needs `pr_url`; a retry of test/implement benefits from prior
`failures`/`validation_notes`). Everything dropped is still durably on the stage record
(`StageRecord.output`) and in the JSONL log — folding is a *prompt-feeding* convenience,
never the system of record. The dedicated `task.pr_number`/`task.pr_url` fields stay
(other consumers read them: `_on_task_completed`, `status()`); deliver's fold mirrors
`pr_url` into context so render has one uniform source. Documented duplication, not drift.

The whitelist is a `dict[Stage, tuple[str, ...]]` constant next to `STAGE_SPECS`. A
key present in the whitelist but absent from a given result is simply skipped (fold is
tolerant — the fail-open discipline the rest of the engine already uses).

## 2. Size caps (bounded by construction)

The context is fed into every subsequent prompt, so it must be bounded regardless of
what a model returns:

- **String values:** truncated to **2000 chars** (with a `… [truncated]` marker).
- **List values:** at most **40 items**, each item a string truncated to **500 chars**;
  an over-long list gets a final synthetic `"… (<n> more)"` element.
- **Whole-context ceiling:** after folding, if the JSON-serialized context exceeds
  **16 KB**, drop entire stage contributions in a **fixed reverse-pipeline priority
  order** (drop review first, then deliver, … keeping intake/scope/implement — the ones
  downstream stages most need) until under the ceiling. The drop is deterministic (fixed
  order, no wall-clock/random), so replay reproduces it exactly.
- Only these three caps; no per-key special-casing. Caps are engine constants.

## 3. Key collisions

- **Across stages:** the whitelist→context key map is **injective** — no two stages
  write the same context key (verified by a unit test over the constant). So a later
  stage never clobbers an earlier stage's contribution. Collisions are impossible by
  construction, not by ordering luck.
- **Across attempts (retries):** `_absorb_outputs` runs only on a `SUCCESS` result, and
  a stage reaches SUCCESS at most once per task, so a stage writes its keys once. If a
  stage were ever re-folded (replay/resume), the fold is **idempotent**: same
  StageResult → same context values (last-write-wins on that stage's own keys, and the
  value is a pure function of the result). No attempt-suffixed keys, no accumulation.

## 4. Cache-stability across stages

The prompt-cache thesis needs a **stable prefix**. The context block is the *volatile
suffix* by design; stability comes from ordering, not from freezing content:

- Render context in **fixed canonical order** (pipeline order: intake → … → review),
  each stage's keys in a fixed order — never dict-insertion/arbitrary order — so the
  block is a pure function of the folded values (byte-identical on replay).
- Step 6 places the block **after** the stable parts (project commands + spec + task
  title/body). Those stay byte-identical across all six stages of a task; only the
  context suffix grows. That is the honest, achievable cache win at the render layer:
  the shared prefix is reused; the growing tail is not. (True per-stage session
  continuity — innovation #1 — is out of scope and reserved.)
- Folding changes the prompt, hence the `content_hash` — correct and intended (a
  different stage/attempt is a different dispatch). No idempotency-key concern.

## 5. Shape summary (for step-5 implementation, pending approval)

- New field: `Task.context: dict = Field(default_factory=dict)` — engine-owned,
  persisted with the task doc (so it survives a crash and drives resume/replay identically).
- `_absorb_outputs(task, result)` generalized: look up `CONTEXT_KEYS[result.stage]`,
  copy each present key (capped) into `task.context`, apply the whole-context ceiling,
  and keep the existing `pr_number`/`pr_url` dedicated-field lift.
- No project hook, no model call, no new I/O. Purely additive to the seam.

**STOP — awaiting approval before implementing step 5.** Steps 6–7 depend on this shape
(render consumes `task.context`; cwd is sourced from the folded `worktree`), so I am not
pre-building them.
