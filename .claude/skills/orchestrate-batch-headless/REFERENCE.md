# Headless lane — recovery and lane details

Loaded on demand from `SKILL.md`. Everything here is for when something has already gone
wrong, or when you need to reason about the lane's mechanics.

## The driver was killed mid-dispatch

A kill (Ctrl-C, reboot) while dispatches are outstanding leaves each in-flight task `RUNNING`
with `pending_work_item_id` set. `dispatchable()` correctly excludes leased tasks, so before
#313 the next `run-headless` found nothing to do and printed an end-of-run status dump that
read as a successful no-op.

**Since #313, just re-invoke the driver command.** At startup `Scheduler.run` reclaims the
leases left by the driver claim on the Run doc when that process is this one or is provably
dead on this host: the lease is cleared, the stage stays `RUNNING`, and `next_work`
re-dispatches the **same attempt** from the last checkpoint. No `record` by hand, and no
retry budget spent. Each release is evented as `dispatch_reclaimed` (which `events_audit`
counts as closing its `stage_dispatched`), and the run's `scheduler.reclaimed` block lists
what was recovered.

### Symptom: it stops with `blocked_on_orphaned_dispatches`

`run-headless` exits **non-zero** with `scheduler.exit_reason = blocked_on_orphaned_dispatches`
when tasks hold leases it may not reclaim — `scheduler.driver_at_start.state` says which:

- `live` — another driver on this host is still running those dispatches. **Do nothing**;
  you are looking at a healthy run. Confirm with
  `ps -eo pid,etime,command | grep "[r]un-headless"`.
- `unclaimed` — no scheduler ever claimed the run (it was driven task-by-task through
  `next`/`record`, whose background invocations hold live leases the same way). The engine
  cannot prove those are orphans, so it refuses to steal them.
- `foreign_host` — the claim came from another machine, where a local pid check means nothing.

Once you have confirmed by hand that nothing is alive, the escape hatch is still terminal:
`orchestrator abandon --run RUN --task <id> --reason "driver killed"` releases the lease and
FAILS the task (`--min-idle-s` / `--force` govern its own liveness guard). There is no
"steal anyway" flag by design: a wrongly reclaimed live lease double-dispatches the stage.

`watch --activity` distinguishes the two shapes of a frozen stream: `STREAM STALLED` (the
model went quiet) vs `NO LIVE DRIVER` (the claiming process is gone — re-invoke the driver).

## Genuine crash resume (no orphaned leases)

If the driver died *between* stages rather than mid-dispatch, just re-run the driver command.
All state is persisted; `dispatchable` re-derives what is left and `in_flight` re-counts any
still-leased task. A crash-marked RUNNING stage re-dispatches at the same attempt — no
double-execution. Resume granularity is per task, never a whole round.

A resume that supersedes a still-held lease is self-describing (#142): the re-dispatch's
`stage_dispatched` carries `resume: true` / `supersedes: <old work_item_id>`, and the retired
lease gets a `lease_superseded` event.

## Session continuity — how to actually check it

The lane chains provider sessions across stages: a stage records its `session_id`, the engine
stores it as `task.session_ref`, and the next stage dispatches with `--resume <id>` so the
model keeps its own context. This is where the high cache-read rates come from.

**You cannot verify this from the timeline.** `stage_dispatched` does not carry `session_ref`
(#314) — only `stage_recorded` does. Reading the dispatch events alone makes continuity look
like it never engages, which is wrong and has already misled one audit. Check the task doc or,
while the run is live, the process table:
```
python3 -c "import json;d=json.load(open('runs/RUN/status-RUN-#TASK.json'));print(d.get('session_ref'), d.get('session_provider'))"
ps -eo pid,command | grep "[c]laude -p" | grep -o "\-\-resume [a-f0-9-]*"
```

Continuity is provider-gated (#9): a claude session ref never rides a codex dispatch or the
reverse. It is also deliberately dropped after a failure — warm retry is off by design.

## Measuring what a run cost

Real per-stage dollars land in `runs/<RUN>/stage-costs.jsonl`. Raw provider streams are under
`runs/<RUN>/stages/<task>/<stage>-attempt<N>.stream.jsonl`; the final `result` event carries
`session_id`, `total_cost_usd`, and `usage` (including `cache_read_input_tokens` /
`cache_creation_input_tokens`) if you need to reason about cache behaviour.

Dispatched prompts are **not** persisted anywhere (#314), so prompt size and cross-stage
prefix stability are not auditable after the fact.

## Related run modes

- **`run-queue`** — drains a queue file, forcing HEADLESS with a fresh engine per claimed
  entry (`--root <root>/<run_id>/`), so derived runs never comingle stores. Different entry
  point from `run-headless`; don't conflate them.
- **`orchestrate-batch-interactive`** — the in-session lane. Slower, context-hungry, and
  records no cost, but lets a human watch each stage.
