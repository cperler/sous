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
- `PROJECT` = `<your-project-adapter>` (e.g. `adapters.project.selfhost`, the reference; or your own).
- Engine call shape: `uv run orchestrator --root "$ROOT" --run "$RUN" --project "$PROJECT" <cmd> ...`

## One-time setup
1. `… init-run --lane full` (or `lite`/`micro`).
2. `… add-task --task "$TASK"` (resolves the issue via the GitHub task source).

## The loop (repeat until the task is terminal)
Before every model dispatch, require the Claude Code status-line sensor. Configure
`orchestrator statusline` as this session's `statusLine` command; it caches Claude Code's
`context_window` payload. Confirm it with `orchestrator supervisor-context`.

1. **next**: `WORK=$(… next --task "$TASK" --util auto --guard-supervisor-context
   --supervisor-resume-command "start a fresh Claude Code session and invoke
   /orchestrate-task-interactive for $RUN $TASK")`.
   - If `WORK` is `null`, the task is done — stop.
   - First check `… status`: if `run_state` is `parked`, surface its reason/resume command
     and stop. A fresh session runs `… resume-supervisor` once before restarting the loop.
   - `--util auto` probes the account's REAL 5h utilization (see `orchestrator util`;
     a probe miss falls back to 0.0 and says so) and the engine turns it into the binding
     dispatch limit. **Do not exceed the engine's limit** even though the Workflow
     cap could allow more — the engine's number is the policy, the Workflow cap is a
     ceiling.
2. **dispatch**: invoke the **Workflow shim** (`run_targets/workflow_shim.js`) with
   `{ workItems: [WORK], dispatchLimit: <engine limit>, now: <ISO timestamp>, schemas: {...} }`.
   The shim CANNOT enforce `timeout_s` (no clock in the Workflow sandbox) — YOU own it: if a dispatch visibly exceeds the WorkItem's `timeout_s`, stop waiting, hand-craft that item's `StageResult` with `status: "timeout"` and a one-line `error`, and record it — the engine classifies TIMEOUT and retries from the checkpoint. Never leave a hung dispatch un-recorded.
   **Pass the WorkItem through VERBATIM** — copy the whole JSON object `next` printed;
   never retype or abbreviate a field. `content_hash` is a 64-char digest that ties the
   returned result to this dispatch: `record` refuses a mismatch, and the shim aborts the
   batch before spending anything when the shape is wrong (#311).
   The shim calls `agent()` in-session and **returns** an array of `StageResult`
   objects (it cannot write to disk). It does the actual work in the task's worktree.
3. **persist + record**: write each returned `StageResult` to a temp file and run
   `… record --result <file>`. Read the JSON outcome:
   - `task_completed` → the PR is open; stop (success).
   - `task_failed_*` → stop (failure); surface the reason.
   - `stage_completed` / `stage_failed_will_retry` → loop again (the engine re-emits
     the same stage with appended learnings on a retry).
   - non-zero exit with `{"ok": false, "recorded": false, …}` → the result did not answer
     the outstanding dispatch (also logged as `result_rejected`); nothing was stored. Do
     NOT edit the result's `content_hash` to make it pass — re-dispatch with
     `… next --resume` and record what actually comes back.

## Resumability
Because the shim only returns results **on Workflow completion**, a session death
mid-dispatch loses only the in-flight **batch** — re-run from `… next`/`… resume`
and the engine re-emits the un-recorded stage. Keep batches small.

## Audit (every gate)
Run `… status` and check `lane_audit.clean == true`: every recorded model call must
be `interactive:claude` with zero `unattributed`/`off_lane`. A hidden `claude -p`
would show up here — there should be none.
