# Batch runner — headless×claude lane

Set up a batch and hand the human a command that drives it to completion **outside this
session**. The engine's own `Scheduler` is the supervisor here — not you. Your job is
scaffolding (create the run, add the tasks, sanity-check the DAG), the **handoff**, and
reading the result afterwards.

**Prefer this skill over `orchestrate-batch-interactive` for ordinary batches.** On the
interactive lane every stage's prompt and result flows through the session context, and the
run records `$0.00`/zero tokens because the Workflow shim cannot report usage — 15 runs of
history are financially invisible for exactly this reason. The headless lane spends its
tokens in a subprocess, records real per-stage cost in `stage-costs.jsonl`, chains provider
sessions with `--resume` (measured 92–96% cache hits on long stages), and leaves the session
free. Use the interactive skill only when a human needs to watch each stage as it happens.

## Constants
- `ROOT` = the shared runs-root (top-level `runs/`). `RUN` = run id. `PROJECT` = project
  adapter module (e.g. `adapters.project.selfhost`).
- **Always pass `--shared-root`** when `ROOT` is the top-level `runs/` (#91) — it forces the
  per-run nest even on a fresh `runs/` the auto-detect heuristic can't recognize. No-op once
  nesting exists, so it is safe on every call.
- No `--mode` flag is needed: `run-headless` forces `ExecutionMode.HEADLESS` itself
  (`cli.py:191-192`), regardless of the global default (`interactive`).

## 1. Scaffold (you do this, via Bash — it is cheap and deterministic)
```
uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" init-run --lane full
uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" add-task --task "#NNN"
```
One `add-task` per issue; the task source supplies each task's `depends_on` and the engine
builds the DAG. Then confirm the shape before handing over:
```
uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" dispatchable --util auto --max-concurrent 3
```
Check the DAG is what you intended (no accidental serialization, no missing edge) and that
`limit > 0`. If `limit` is 0 you are capacity-stalled — say so rather than handing over a
command that will spin.

Run-level settings must be chosen **now**: `--lane`, `--budget-usd`, `--review-workflow`,
`--max-filed-followups`, `--progress-comments` all live on the Run doc and cannot be added
later (every subcommand rebuilds the Engine from constructor defaults — see CLAUDE.md).

## 2. Hand off (do NOT run this yourself)
Give the human the driver command as a **single clean line, in its own code block, with no
trailing `#` comments** — interactive zsh has `interactive_comments` off and will choke:

```
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT run-headless --wait
```

Say these three things with it, every time:

1. **The driver runs in the foreground and owns the run for its whole duration.** Leave that
   terminal alone. Ctrl-C sends SIGINT to the process group and kills the `claude -p`
   children mid-stage — the failure that produced #313.
2. **Monitor from a second terminal** — `dashboard --watch`, or `watch --activity`.
3. **What it will do outwardly** — how many PRs it may open, against which repo. For a live
   run against a real product repo, the human picks the issues and approves *before* any
   write or PR (CLAUDE.md hard checkpoint).

`--wait` sleeps through capacity stalls and rate-limit cooldowns instead of returning;
without it the driver returns on the first stall. `--util auto` probes real 5h utilization.
`--max-concurrent` defaults to 3.

## 3. Read the result (after the human reports it finished)
```
uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" status
```
Gate on:
- `run_state` is `completed` / `failed`.
- `lane_audit.clean == true` — every recorded call `headless:claude`, zero unattributed.
  **This lane is newly exercised**; if it reports false, check for an attribution bug in the
  headless path before concluding the run misbehaved.
- `events_audit` — `dispatched == recorded`. A non-zero `outstanding` alongside
  `"clean": true` means orphaned leases, not a healthy run (see REFERENCE.md).

Then per CLAUDE.md: merge the PRs in dependency order, verify each issue actually closed,
clean worktrees/branches/checkpoint tags — but **never** `rm -rf runs/<RUN>/`. Run
`trunk-gate` over the merged trunk. Offer `triage-followups` for the issues the run auto-filed.

## Recovery, resume, and lane details
If the run is stuck, a dispatch was killed, or `run-headless` returns immediately without
doing anything, read `REFERENCE.md` in this skill directory. Don't inline that material
here — it is only needed when something has already gone wrong.
