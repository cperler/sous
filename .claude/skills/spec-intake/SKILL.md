---
name: spec-intake
description: Turn a raw idea into a validated, dependency-ordered spec and file it as a batch of GitHub issues that feed the batch lane. You run the conversation and the decomposition; deterministic `orchestrator spec` code validates, plans, and files. The front door above intake — issues out, not code.
---

# Spec intake — idea → validated spec → dependency-ordered issues

You are the **front door**. A run starts from an already-written issue; nothing upstream
turns *an idea* into well-scoped, dependency-ordered issues. That's this skill. You own
the conversation and the decomposition; the deterministic `orchestrator spec` commands own
validation, ordering, and filing. You never file issues by hand — `spec file` does it, in
dependency order, translating local ids to real issue refs. **Filing is outward-facing:
the human confirms the plan before anything is created.**

## Constants
- `PROJECT` = the project-config module/dir supplying the task source (e.g. `adapters.project.heysoo`).
- Command shape: `uv run orchestrator [--project "$PROJECT"] spec <validate|plan|file> <file>`.

## The flow
1. **Interrogate the idea.** Pin down the goal, the hard constraints, and the explicit
   non-goals. Ask the 2–3 questions that most change the decomposition; don't interview.
2. **Decompose.** Break it into **small, independently-shippable tasks**, each with its
   own acceptance criteria and explicit dependencies. A good task is one PR's worth of
   work with a clear "done" test. Make dependencies real (t2 needs t1's output), not
   incidental ordering. Prefer more, smaller tasks over few big ones.
3. **Write the spec JSON** to a file (schema: `orchestrator/schemas/spec.json`):
   ```json
   {
     "title": "...", "summary": "goal / constraints / non-goals",
     "tasks": [
       { "id": "t1", "title": "...",
         "body": "Scope: ...\nAcceptance: ...\nOut-of-scope: ...",
         "depends_on": [], "labels": ["..."],
         "provider_tag": null, "pipeline": "lite", "estimate": "S" }
     ]
   }
   ```
   `id` is a **local** id (t1, t2, …), used only to express `depends_on`; `spec file`
   swaps them for real issue refs. Put full scope + acceptance + out-of-scope in `body` —
   that becomes the issue.
4. **Validate, then plan.**
   - `uv run orchestrator spec validate <file>` — schema + DAG (unknown refs, cycles,
     dup ids) checks. Fix any reported error before continuing.
   - `uv run orchestrator spec plan <file>` — prints the exact filing order, the
     `spec:<slug>` batch label, and each task's labels/deps.
5. **Show the human the plan and STOP.** Filing opens real issues. Do not run `spec file`
   until the human confirms the plan.
6. **File.** `uv run orchestrator --project "$PROJECT" spec file <file>` (add `--dry-run`
   first to preview exactly what would be created). It files each task in topological
   order, writes a `Depends-on: #N` line into each dependent's body, applies the task's
   labels plus a `spec:<slug>` label (so the whole batch is queryable), and returns the
   local-id → issue-ref mapping.
7. **Offer the follow-on.** The filed issues now feed the batch lane. Offer to run them:
   point at `orchestrate-batch-interactive` (`add-task --task "#N"` for each filed ref;
   the `spec:<slug>` label lists them as a group). The engine builds the DAG and drives.

## Notes
- Every issue a spec files carries `spec:<slug>` — `gh issue list --label spec:<slug>`
  recovers the batch later.
- `spec validate` / `spec plan` are pure (no `--project`); only `spec file` needs the
  task source. `--dry-run` files nothing.
- You decompose and converse; the deterministic code validates/orders/files. Don't
  re-implement DAG or filing logic here.
