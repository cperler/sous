---
name: batch-plan
description: Turn a pile of ALREADY-FILED, independently-authored GitHub issues into a validated, dependency-ordered batch plan and apply it to a run. You run the analysis — which issue depends on which, which lane fits each — over the real issue bodies; deterministic `orchestrator batch-plan` code validates and applies. The producer for existing issues (spec-intake is the producer for new work).
---

# Batch plan — existing issues → analyzed DAG → a scheduled batch

You are the **auto-analysis producer for issues that already exist**. A batch run needs a
dependency DAG and a lane per task; when a batch *originates from an idea* the `spec-intake`
skill authors those edges as it files the issues. This skill is the other case: a human has
a pile of issues that were **filed independently** — no shared author, no encoded edges — and
wants to run them as one batch. Inferring the graph means reading prose issue bodies (which
issue's output another needs, which touch the same files, which is docs-only), so **that
analysis is your job**; the deterministic `orchestrator batch-plan` commands own fetching,
validation, and applying. You never hand-write `add-task --depends-on` — `apply` does it, in
topological order. **Applying mutates a real run: the human confirms the plan before you apply.**

## Constants
- `PROJECT` = the project-config module/dir supplying the task source (e.g. `adapters.project.selfhost`).
- Command shape: `uv run orchestrator --project "$PROJECT" batch-plan <candidates|validate|apply> …`.

## The flow
1. **Fetch the candidates.** `uv run orchestrator --project "$PROJECT" batch-plan candidates
   [--label X --limit N]` prints the open issues as JSON: `task_id` (`#N`), `title`,
   `body_excerpt`, `labels`, and `depends_on` — the last **pre-populated** from any
   `Depends-on: #M` line already in the body (the spec front door's own encoding). Those are
   edges someone already recorded; keep them unless a body contradicts them. Narrow with
   `--label` when the human names a batch (e.g. a `spec:<slug>` group, or a milestone label).
2. **Analyze — the model work.** Read each body and infer:
   - **Dependencies.** Real ones only: task B needs task A's *output* (A defines the schema B
     consumes, A adds the endpoint B calls, A's migration must land before B's query). Signals:
     files/modules touched, feature layering, and explicit "after #N"/"depends on #N"/"blocked
     by #N" prose. Do **not** invent ordering edges for things that merely *could* run in
     sequence — an unnecessary edge serializes work that could parallelize.
   - **Lane fit** (`pipeline`): docs-only / pure-config → `micro` (and put `test`,`deliver` in
     `deterministic_stages` — a docs change needs no model test/PR-writing); small mechanical /
     localized → `lite`; risky, cross-cutting, or ambiguous → `full`.
   - **Provider** (`provider_tag`): set `codex` only when a body/label calls for it; else omit.
   - **Model tier** (`model`) and **effort** (`effort`): for an architecture-heavy /
     brainstorming-shaped task, pin the Mythos tier in the plan — `"model": "fable"`
     (Codex-fable-5) — so the design work runs above Opus (#84); `"effort": "high"` raises
     reasoning effort (#96). Both are plan fields (#287) — never hand-write `add-task --model`,
     which would cost you `apply`'s topological ordering. Omit both to keep the role default.
     A pin must match the task's provider (a `codex`-tagged task pins a codex id like
     `gpt-5.5`); `validate` catches a bad alias or a mismatch before anything is added.
3. **Write the plan JSON** to a file (schema: `orchestrator/schemas/batch_plan.json`):
   ```json
   {
     "tasks": [
       { "task_id": "#41", "depends_on": [], "pipeline": "full",
         "model": "fable", "effort": "high",
         "rationale": "adds the auth middleware everything else layers on — protocol design, Mythos tier" },
       { "task_id": "#42", "depends_on": ["#41"], "pipeline": "lite",
         "rationale": "calls the middleware from #41; localized" },
       { "task_id": "#43", "depends_on": ["#41"], "pipeline": "micro",
         "deterministic_stages": ["test", "deliver"],
         "rationale": "docs for the #41 middleware — no code, no model test/PR" }
     ]
   }
   ```
   `task_id` is the **real issue ref** (`#N`), not a local id — these issues already exist.
   Give every non-trivial edge and lane choice a one-line `rationale` (it's human-visible in
   the plan). An edge may point at an already-terminal issue *outside* the batch (a completed
   dependency) — that's allowed and imposes no scheduling constraint.
4. **Validate.** `uv run orchestrator --project "$PROJECT" batch-plan validate <file>` —
   schema + DAG (cycles, dup ids, self-edges) + the `model`/`effort` pins (unknown alias,
   provider mismatch) and, with `--project`, verifies every external edge points at a real
   issue. Fix any reported error before continuing.
5. **Show the human the plan and STOP.** `apply` adds tasks to a real run. Present the ordered
   plan with your rationales and **wait for confirmation.** Do not apply unprompted.
6. **Apply.** `uv run orchestrator --root <dir> --run <R> --project "$PROJECT" batch-plan apply
   <file>` (add `--dry-run` first to preview exactly what would be added). It adds each task in
   topological order with its edges, lane, `provider_tag`, `deterministic_stages`, and any
   `model`/`effort` pin, and emits a `batch_planned` event. The run must already exist (`init-run`).
7. **Drive the batch.** The run now carries the DAG. Point at `orchestrate-batch-interactive`
   to fan the tasks out; the engine walks the graph you built.
8. **Acceptance pass (spec-originated batches only).** If this batch is a `spec:<slug>` group
   (filed by `spec-intake` from a spec), close the loop after it completes: run the
   whole-spec conformance gate and walk each acceptance criterion against the merged changes,
   filing `spec-gap` follow-ups for anything unmet — see the **Acceptance pass** section of
   the `spec-intake` skill for the procedure (`spec conformance ./specs/<slug>.json`). A pile
   of independently-filed issues with no originating spec has no whole-spec gate — per-task
   review is the ceiling there.

## Notes
- **Two producers, one scheduler.** Use `spec-intake` when the work is a *new idea* (it files
  the issues AND their edges together, front-to-back). Use **this** skill when the issues
  **already exist** and only need analysis + wiring. Both hand the batch lane the same shape.
- `candidates` and `validate` are read-only; only `apply` mutates a run (and honors `--dry-run`).
- You analyze and converse; the deterministic code validates/orders/applies. Don't
  re-implement DAG or scheduling logic here.
