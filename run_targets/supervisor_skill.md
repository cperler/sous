---
name: orchestrate-task-interactive
description: Drive one task through the engine on the interactive×claude lane. Thin supervisor — all deterministic logic lives in the `orchestrator` CLI; this skill only sequences calls.
---

# Supervisor — interactive×claude lane

You are the **supervisor**. The deterministic logic (stage order, retry, cost,
status, capacity) lives in the `orchestrator` engine CLI. Your only job is the loop:
**ask what's ready → dispatch via the Workflow shim → record the result → repeat.**
You never call a model directly and you never run `claude -p`.

## Constants
- `ROOT` = the run's status/ledger dir (e.g. `runs/<run-id>`).
- `RUN` = the run id. `TASK` = the task id (a GitHub issue, e.g. `#505`).
- `PROJECT` = `<your-project-adapter>` (e.g. `adapters.project.heysoo`, the reference; or your own).
- Engine call shape: `uv run orchestrator --root "$ROOT" --run "$RUN" --project "$PROJECT" <cmd> ...`

## One-time setup
1. `… init-run --lane full` (or `lite`/`micro`).
2. `… add-task --task "$TASK"` (resolves the issue via the GitHub task source).

## The loop (repeat until the task is terminal)
1. **next**: `WORK=$(… next --task "$TASK" --util auto)`.
   - If `WORK` is `null`, the task is done — stop.
   - **Deterministic stages are already done for you.** `next` runs any leading
     deterministic stage (e.g. `intake` — worktree/branch/baseline) in-process on the
     `engine:none` lane and returns the first *model* WorkItem. You never create a
     worktree by hand or dispatch intake to a model (heysoo #227).
   - `--util auto` probes the account's REAL 5h utilization (see `orchestrator util`;
     a probe miss falls back to 0.0 and says so) and the engine turns it into the binding
     dispatch limit. **Do not exceed the engine's limit** even though the Workflow
     cap could allow more — the engine's number is the policy, the Workflow cap is a
     ceiling.
2. **dispatch**: invoke the **Workflow shim** (`run_targets/workflow_shim.js`) with
   `{ workItems: [WORK], dispatchLimit: <engine limit>, now: <ISO timestamp>, schemas: {...} }`.
   The shim CANNOT enforce `timeout_s` (no clock in the Workflow sandbox) — YOU own it: if a dispatch visibly exceeds the WorkItem's `timeout_s`, stop waiting, hand-craft that item's `StageResult` with `status: "timeout"` and a one-line `error`, and record it — the engine classifies TIMEOUT and retries from the checkpoint. Never leave a hung dispatch un-recorded.
   The shim calls `agent()` in-session and **returns** an array of `StageResult`
   objects (it cannot write to disk). It does the actual work in the task's worktree.
3. **persist + record**: write each returned `StageResult` to a temp file and run
   `… record --result <file>`. Read the JSON outcome:
   - `task_completed` → the PR is open; stop (success).
   - `task_failed_*` → stop (failure); surface the reason.
   - `stage_completed` / `stage_failed_will_retry` → loop again (the engine re-emits
     the same stage with appended learnings on a retry).

   Preserve the agent's narrative: pass its full prose back as the `StageResult`'s
   `raw_output` (the shim does this from `agentResult.raw`), not just the structured
   fields — the per-stage log keeps it as the durable "why", and the review stage's
   narrative is the human-readable audit of the approve/deny decision.

## Evidence-out (automatic at `task_completed`)
When the task completes, the engine publishes the run's evidence through the project
adapter — no supervisor action needed:
- files each **non-blocking** review finding (`review.non_blocking[*]`) as a
  `deferred-scope` follow-up issue (a reviewer's nits are never dropped);
- **self-improvement loop:** files the review's `improvement` idea as an `enhancement`
  issue and renders both it and the `retrospective` (process lesson) into the note — so a
  completed run also grows the roadmap, not just ships a fix;
- posts a completion note (stage table + PR + verdict + follow-ups + self-improvement)
  to the PR/issue.

These run through optional `file_followup` / `publish_note` task-source hooks; an adapter
that omits them is a silent no-op — satisfying the "nothing silently dropped" norm.

## Resumability
Because the shim only returns results **on Workflow completion**, a session death
mid-dispatch loses only the in-flight **batch**. The crash leaves the dispatch lease
held, so a plain `… next` refuses (it guards the in-flight result) — recover with
`… next --resume`, which re-emits the un-recorded stage at the same attempt. Keep
batches small.

## Audit (every gate)
Run `… status` and check `lane_audit.clean == true`: every recorded model call must
be `interactive:claude` with zero `unattributed`/`off_lane`. A hidden `claude -p`
would show up here — there should be none.
