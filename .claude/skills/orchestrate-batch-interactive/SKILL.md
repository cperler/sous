---
name: orchestrate-batch-interactive
description: Drive a multi-task batch through the engine scheduler on the interactive×claude lane. Thin supervisor — the DAG/retry/cascade/capacity logic is all in the `orchestrator` engine; this skill only sequences the fan-out.
---

# Batch supervisor — interactive×claude lane

You are the **batch supervisor**. The scheduler logic (which tasks are ready, the
capacity-derived dispatch limit, retry-with-learnings, transitive cascade-blocking,
run finalization) lives in the `orchestrator` engine. Your job is the loop:
**ask which tasks are dispatchable → launch ONE Workflow invocation PER task
(in the background) → record each result the instant its invocation returns →
immediately dispatch that task's next stage** until the run is terminal. You never
call a model directly and never run `claude -p`.

**Per-task dispatch, not per-round barrier (#97).** Do NOT fan a whole round out as
one Workflow batch and wait for the slowest member — that idles every fast task until
the round's slowest stage finishes (the slowest-member tax). Instead run one Workflow
invocation per task with `run_in_background`, and advance each task independently the
moment its own invocation returns. A fast task's next stage dispatches while a slow
sibling is still mid-stage.

## Constants
- `ROOT` = the shared runs-root (the top-level `runs/` dir). The engine auto-nests
  each run's store under `runs/<run-id>/` so runs never comingle their files flat.
- `RUN` = run id. `PROJECT` = `<your-project-adapter>` (e.g. `adapters.project.heysoo`).
- Engine call shape: `uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" <cmd> ...`
  - **Always pass `--shared-root` when `ROOT` is the top-level `runs/` dir** (#102): it
    forces the per-run nest even on a *fresh* `runs/` the auto-detect heuristic can't yet
    recognize (no KB / sibling stores exist on day one). It's a no-op once nesting is
    established, so it's safe to pass on every call. Omit it only if you point `ROOT`
    directly at a pre-existing per-run dir (`runs/<run-id>`).

## One-time setup
1. `uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" init-run --lane full`.
2. `… --shared-root … add-task --task "#NNN"` for each task in the batch (the task
   source supplies each task's `depends_on`; the engine builds the DAG).

## Capacity: bind across concurrent invocations
`dispatchable` reports the DAG-ready set AND the in-flight lease count, so you can size
remaining headroom yourself:

```
D=$(uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" dispatchable --util auto --max-concurrent 3)
```
- `D.dispatchable` — DAG-ready, unleased tasks (EXCLUDES anything already in flight).
- `D.limit` — the engine's capacity-derived dispatch cap (binding — never exceed it).
- `D.in_flight` / `D.in_flight_count` — tasks with a live dispatch lease right now
  (one background Workflow invocation out per task).
- **Remaining headroom = `limit - in_flight_count`.** Launch at most that many NEW
  invocations. Because there is no per-round barrier, you MUST re-check this before
  **every** follow-on dispatch (not once per round): a task returning frees one slot,
  and the concurrency cap has to bind across all concurrently-live invocations. This
  is also correct after a resume — a crashed invocation leaves its lease held, so
  `in_flight` re-counts it and you don't over-dispatch.
- If `dispatchable` is empty but tasks remain non-terminal and `in_flight_count == 0`,
  you're capacity-stalled (`limit == 0`) — wait for the usage window, then retry.

## The loop (repeat until `status.run_state` is terminal)
Maintain a set of in-flight background dispatches (task id → background invocation +
its `timeout_s` + start time). Each pass:

1. **Fill headroom.** Compute `slots = limit - in_flight_count` from a fresh
   `dispatchable`. For up to `slots` tasks from `D.dispatchable`:
   - `WORK=$(… --shared-root … next --task "$T")` → one WorkItem (the engine drains
     any leading deterministic stage in-process and returns the first model WorkItem;
     `null` means that task is done — skip it).
   - **Launch ONE Workflow invocation for that task with `run_in_background`**:
     invoke `run_targets/workflow_shim.js` with
     `{ workItems: [WORK], dispatchLimit: <engine limit>, now: <ISO>, schemas: {...} }`.
     The shim runs the stage in-session and **returns** its StageResult (it cannot
     persist). Record `T`, the background handle, `WORK.timeout_s`, and the start time.
2. **Reap returns.** As each background invocation returns, immediately:
   - **record** its StageResult: write to a temp file → `… --shared-root … record --result <file>`.
     The engine advances that task, retries a failed stage with learnings, and
     cascade-blocks dependents of any task that fails — you don't manage that.
   - **re-check `dispatchable`** and, if that task is now dispatchable and there's
     headroom, immediately launch its next stage (step 1). Do not wait for siblings.
3. **Own per-item timeouts individually.** The shim CANNOT enforce `timeout_s` (no
   clock in the Workflow sandbox). Track each in-flight dispatch's own deadline: if
   one visibly exceeds ITS `timeout_s`, stop waiting on that one only, hand-craft its
   `StageResult` with `status: "timeout"` and a one-line `error`, and record it. Never
   leave a hung dispatch un-recorded, and never let one slow task block reaping others.
4. Loop. Stop when `… --shared-root … status` shows `run_state` = `completed` or `failed`.

## Resumability
All state is persisted. If the session dies mid-batch, just start this skill again on
the same `ROOT`/`RUN`: `dispatchable` re-derives what's left, `in_flight` re-counts any
still-leased tasks, and the engine re-emits any un-recorded stage (a crash-marked
RUNNING stage re-dispatches at the same attempt — no double-execution). **Resume
granularity is per task, not per round** — only the individual in-flight task's stage
is re-run, never a whole round. Resume only on a genuine crash — always capture `next`
into `WORK` in the single call above, never `next` then `next --resume` for the same
live stage. A resume that supersedes a still-held lease is self-describing in the
timeline (#142): the re-dispatch's `stage_dispatched` carries `resume: true` /
`supersedes: <old work_item_id>` and the retired lease gets a `lease_superseded` event,
so a consumer counting `stage_dispatched` can discount the superseded one.

## Audit (every gate)
`… --shared-root … status` → `lane_audit.clean == true`: every recorded call
`interactive:claude`, zero unattributed. The durable timeline is `events.jsonl`;
per-stage records are under `stages/<task>/NN-<stage>.json`.

## Post-run follow-up triage (opt-in: `--triage-followups`)
A completed run's evidence-out seam files the review stages' `non_blocking` findings and
`improvement` ideas as GitHub issues **automatically, with no human gate** (the run must
never block on a human). That grows the backlog with issues the human never chose to track
and often can't parse. If the invocation args include **`--triage-followups`** (or the
human asks to triage the run's filed issues), then **after** the run reaches a terminal
`run_state` and the audit gate above is clean, invoke the **`triage-followups`** skill on
this `RUN`:
- It enumerates only the issues THIS run filed (matched by each issue's
  `Filed automatically from the <task_id> review` provenance footer, open issues only) and
  walks them **one at a time**, explaining each from its source review finding
  (`stages/<task>/NN-review.json`) and the code it points at, then takes the human's
  keep / close / promote / edit decision per issue.
- It is a pure human-judgment gate — read-only on the run, acting only on GitHub. It is
  re-runnable (open-only enumeration), so the human can also defer triage and run
  `triage-followups` on this run later instead of inline. Without the flag, behavior is
  exactly as before: the run finalizes and its auto-filed issues stay untriaged.
