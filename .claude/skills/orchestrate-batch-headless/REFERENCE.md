# Headless lane — recovery and lane details

Loaded on demand from `SKILL.md`. Everything here is for when something has already gone
wrong, or when you need to reason about the lane's mechanics.

## Symptom: `run-headless` returns immediately and does nothing

The output looks like a normal end-of-run status dump — `run_state: running`, tasks showing
`stage: <something>`, `stale: false`, `lane_audit.clean: true`. It reads as a successful
no-op. It is not.

**Check `events_audit`.** If `outstanding > 0` (i.e. `dispatched > recorded`), the run holds
orphaned dispatch leases: a previous driver was killed mid-stage, leaving tasks `RUNNING`
with `pending_work_item_id` set. `dispatchable()` correctly excludes in-flight tasks, so the
dispatchable set is empty and `Scheduler.run` exits on its first tick.

`Scheduler.run`'s docstring claims "Resumable: call again on the same run to continue after a
kill." **That claim does not hold in this state** — tracked as #313. Until it lands, recover
by hand.

Confirm nothing is actually alive first:
```
ps -eo pid,etime,command | grep "[r]un-headless"
ps -eo pid,etime,command | grep "[c]laude -p"
```
If a driver *is* alive, do nothing — you are looking at a healthy run.

### Recovery: record a timeout per orphaned dispatch

`resume` is read-only (it only reports resume points) and `abandon` is terminal (it sets
`state = FAILED`). Neither recovers. The working path is to record a `timeout` StageResult
for each held lease: `TIMEOUT` is in `SALVAGEABLE_FAILURE_STATUSES` (`engine.py:191`), so the
stage re-dispatches at the next attempt from its checkpoint instead of failing the task.

Build one result file per orphaned dispatch, reading the lease and the original dispatch
event straight off disk so the `work_item_id` and the **full 64-char** `content_hash` are
exact (a truncated hash is accepted silently — #311):

```python
import json, datetime, pathlib
run = "<RUN>"
ev = [json.loads(l) for l in open(f"runs/{run}/events.jsonl")]
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
for t in ("<TASK>", ...):
    task = json.load(open(f"runs/{run}/status-{run}-#{t}.json"))
    wid, chash = task["pending_work_item_id"], task["pending_content_hash"]
    disp = [e for e in ev if e["type"] == "stage_dispatched"
            and e["task_id"] == f"#{t}" and e.get("work_item_id") == wid][0]
    pathlib.Path(f"scratchpad/timeout-{t}.json").write_text(json.dumps({
        "schema_version": "1", "work_item_id": wid, "content_hash": chash,
        "run_id": run, "task_id": f"#{t}", "stage": disp["stage"],
        "attempt": disp["attempt"], "model": disp["model"], "effort": disp.get("effort"),
        "status": "timeout", "structured_output": None, "raw_output": None,
        "error": "driver process exited mid-dispatch; stream ended without a result event",
        "lane_used": {"execution_mode": "headless", "provider": "claude",
                      "invocation": f"claude -p --model {disp['model']}"},
        "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "cost_usd": None, "completed_at": now}, indent=1))
```

Then hand the human one `record` per file, followed by the driver command again:
```
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT record --result scratchpad/timeout-<TASK>.json
```

Each `record` reports `outcome: stage_failed` and the task moving to `retrying` — that is
correct, not a problem.

**Tell the human the cost:** this burns one attempt per task. They each have one fewer retry
than a clean run. If a task later fails at the attempt ceiling, discount it — that is partly
the recovery, not the model. It also costs real money: on `batch-headless-1` the killed
attempt burned 2 of 4 scope calls totalling $2.11.

Verify recovery worked before walking away — you want `timeout` at attempt N and a fresh
dispatch at attempt N+1:
```
python3 -c "
import json
for l in open('runs/RUN/events.jsonl'):
    e=json.loads(l)
    if e['type'] in ('stage_dispatched','stage_recorded'):
        print(e['type'], e['task_id'], e.get('stage'), 'att=', e.get('attempt'), e.get('status',''))
"
```

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
