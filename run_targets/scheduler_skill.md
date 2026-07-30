---
name: orchestrate-batch-interactive
description: Drive a multi-task batch through the engine scheduler on the interactive×claude lane. Thin supervisor — the DAG/retry/cascade/capacity logic is all in the `orchestrator` engine; this skill only sequences the fan-out.
---

# Batch supervisor — interactive×claude lane

You are the **batch supervisor**. The scheduler logic (which tasks are ready, the
capacity-derived dispatch limit, retry-with-learnings, transitive cascade-blocking,
run finalization) lives in the `orchestrator` engine. Your job is the loop:
**ask which tasks are dispatchable → fan out their next stage as ONE Workflow batch
→ record every result → repeat** until the run is terminal. You never call a model
directly and never run `claude -p`.

## Constants
- `ROOT` = the run's status/ledger dir. `RUN` = run id. `PROJECT` = `<your-project-adapter>` (e.g. `adapters.project.heysoo`).
- Engine call shape: `uv run orchestrator --root "$ROOT" --run "$RUN" --project "$PROJECT" <cmd> ...`

## One-time setup
1. `… init-run --lane full`.
2. `… add-task --task "#NNN"` for each task in the batch (the task source supplies
   each task's `depends_on`; the engine builds the DAG).

## The loop (repeat until `status.run_state` is terminal)
1. **dispatchable**: `D=$(… dispatchable --util auto --max-concurrent 3)`.
   - `D.dispatch_now` is the engine-bounded set to run THIS round (DAG-ready ∩
     remaining headroom = `limit - in_flight_count`, so in-flight leases are already
     subtracted). The engine's `limit` is binding — never dispatch more than
     `dispatch_now`, even if the Workflow cap could allow more.
   - If `dispatch_now` is empty but tasks remain non-terminal, you're capacity-
     stalled (`limit==0`) — wait for the usage window, then retry.
2. **next** (per task in `dispatch_now`): `… next --task "$T"` → one WorkItem each.
   Collect them into a single batch.
3. **dispatch ONE Workflow batch**: invoke `run_targets/workflow_shim.js` with
   `{ workItems: [...all collected...], dispatchLimit: <engine limit>, now: <ISO>, schemas: {...} }`.
   The shim CANNOT enforce `timeout_s` (no clock in the Workflow sandbox) — YOU own it: if a dispatch visibly exceeds a WorkItem's `timeout_s`, stop waiting, hand-craft that item's `StageResult` with `status: "timeout"` and a one-line `error`, and record it. Never leave a hung dispatch un-recorded.
   **Pass each WorkItem through VERBATIM** — copy the whole JSON object from `next`,
   never retype or abbreviate a field. `content_hash` is a 64-char digest that ties the
   returned result to its dispatch; a truncated preview or one copied from a *sibling*
   in-flight item (easy with several in the batch) is refused at `record`, and the shim
   now aborts the whole batch before spending anything if the shape is wrong (#311).
   The shim runs the stages in-session (hub-and-spoke) and **returns** the
   StageResults (it cannot persist).
4. **record** each returned StageResult: write to a temp file → `… record --result <file>`.
   The engine advances each task, retries failed stages with learnings, and
   cascade-blocks dependents of any task that fails — you don't manage that.
   A non-zero exit with `{"ok": false, "recorded": false, …}` means the result did not
   answer the outstanding dispatch (also logged as `result_rejected`). Do NOT patch the
   result's `content_hash` to make it pass — re-dispatch that task's stage with
   `… next --resume` and record what actually comes back.
5. Loop. Stop when `… status` shows `run_state` = `completed` or `failed`.

## Resumability
All state is persisted. If the session dies mid-batch, just start this skill again
on the same `ROOT`/`RUN`: `dispatchable` re-derives what's left and the engine
re-emits any un-recorded stage (a crash-marked RUNNING stage re-dispatches at the
same attempt — no double-execution). Resume granularity is the dispatch batch.

## Audit (every gate)
`… status` → `lane_audit.clean == true`: every recorded call `interactive:claude`,
zero unattributed. The durable timeline is `events.jsonl`; per-stage records are
under `stages/<task>/NN-<stage>.json`.
