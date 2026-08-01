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
- `PROJECT` = the project-config module/dir supplying the task source (e.g. `adapters.project.selfhost`).
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
         "body": "## Scope\n...\n\n## Acceptance criteria\n- ...\n- ...\n\n## Out of scope\n...",
         "depends_on": [], "labels": ["..."],
         "provider_tag": null, "pipeline": "lite", "estimate": "S" }
     ]
   }
   ```
   `id` is a **local** id (t1, t2, …), used only to express `depends_on`; `spec file`
   swaps them for real issue refs. Put full scope + acceptance + out-of-scope in `body` —
   that becomes the issue.
   - **Model tier is not a spec field.** The spec files issues; a per-task model pin is applied
     later at `add-task --model fable` (claude-fable-5, #84) when a heavy-architecture task is
     scheduled onto a run. If a task is design-heavy, note that in its body so the batch step
     knows to pin the Mythos tier.
   - **Body headings are a contract, not decoration.** Author each task body with markdown
     `##` sections, and give the acceptance criteria their own **`## Acceptance criteria`**
     heading with one bullet per criterion. The conformance gate (step 8) parses that exact
     heading back out to build the acceptance checklist; a criterion buried in prose is a
     criterion the gate can't itemize. End the acceptance section at the next `##` heading
     (e.g. `## Out of scope`) so the parser knows where it stops.
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
   local-id → issue-ref mapping. It also **archives** the filed spec to `./specs/<slug>.json`
   (override with `--archive-dir`, skipped on `--dry-run`) — the spec text plus the
   local-id→issue-ref map. That archive is what the acceptance pass (step 8) reads later;
   keep it.
7. **Offer the follow-on.** The filed issues now feed the batch lane. Offer to run them:
   point at `orchestrate-batch-interactive` (`add-task --task "#N"` for each filed ref;
   the `spec:<slug>` label lists them as a group). The engine builds the DAG and drives.
8. **Acceptance pass (after the batch completes).** Per-task review checks each task
   against its own issue; nothing else checks the assembled whole against *this spec*.
   Slice-level green is not whole-spec green — close the loop here:
   - Run the deterministic gate:
     `uv run orchestrator --project "$PROJECT" spec conformance ./specs/<slug>.json`
     (add `--json` for the raw checklist). It lists every spec task's filed issue, its
     state, any discoverable PR, and the acceptance criteria parsed from the body, and
     **exits non-zero while any issue is still open** — an incomplete batch fails the gate
     mechanically. Don't proceed past an open issue.
   - Then do the part code can't: walk **each acceptance criterion** in the checklist
     against the *actual merged changes* — read the PR diffs and the code, not just the
     green checkmarks. A closed issue means a task shipped, NOT that the spec's promise was
     kept. Ask, per criterion: is this genuinely delivered in what merged?
   - For any criterion **not genuinely met**, file a follow-up issue labeled **`spec-gap`**,
     its body citing the spec slug (`spec:<slug>`) and quoting the unmet criterion, so the
     gap is tracked as real scope rather than silently dropped (per the scope-ledger norm).
   - **STOP for the human before filing** any `spec-gap` issue — filing is outward-facing,
     same rule as step 5. Show the human the criteria you judge unmet and the issues you'd
     open; file only on confirmation.
   You run the per-criterion judgment; the `spec conformance` command owns the
   deterministic checklist and the open/closed gate. Don't re-implement either here.

## Notes
- **Two producers, one scheduler.** This skill is for a *new idea* — it files the issues and
  their edges together. When the issues **already exist** (filed independently, no encoded
  edges) and only need dependency analysis + lane wiring, use the **`batch-plan`** skill
  instead; both hand the batch lane the same dependency-ordered shape.
- Every issue a spec files carries `spec:<slug>` — `gh issue list --label spec:<slug>`
  recovers the batch later.
- `spec validate` / `spec plan` are pure (no `--project`); only `spec file` needs the
  task source. `--dry-run` files nothing.
- You decompose and converse; the deterministic code validates/orders/files. Don't
  re-implement DAG or filing logic here.
