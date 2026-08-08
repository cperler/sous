---
name: orchestrate-batch-headless
description: Drive a multi-task batch on the headless×Codex lane — the default. One `run-headless` driver owns the run and the engine's Scheduler spawns `Codex -p` per stage, so no stage prompt or output passes through the session. Use for ordinary batches; the interactive lane is only for watching stages live.
---

# Batch runner — headless×Codex lane

Set up a batch and get the driver running. The engine's own `Scheduler` is the supervisor
here — not you. Your job is scaffolding (create the run, add the tasks, sanity-check the
DAG), **launching or handing off** the driver, and reading the result afterwards. No stage
prompt or output passes through the session either way.

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
later (every subcommand rebuilds the Engine from constructor defaults — see AGENTS.md).

## 2. Start the driver — three ways, all sanctioned
The driver command is the same in every case:

```
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT run-headless --wait
```

`--wait` sleeps through capacity stalls and rate-limit cooldowns instead of returning;
without it the driver returns on the first stall. `--util auto` probes real 5h utilization.
`--max-concurrent` defaults to 3.

**(a) Backgrounded from this session — the default.** Launch it yourself with the Bash tool
and `run_in_background: true`. This is the preferred shape: the human doesn't have to babysit
a terminal, and you get notified when the driver exits so you can run step 3 unprompted.
Because you launched it, own the consequences:

- **Say that interrupting the session kills the driver.** A backgrounded driver is a child of
  the session's shell, so an interrupt reaches its process group and kills the `Codex -p`
  children mid-stage — the #313 failure. Recoverable, not fatal: re-invoking the same
  `run-headless` command resumes the same attempt from the last checkpoint and spends no
  retry budget (#313), but the human should know before it happens, not after.
- **Confirm it actually started** — poll `status` once and report `driver.alive`,
  `last_state`, and which tasks went `running`. A driver that died on startup and one that is
  working look identical if you never look.
- **Give the human the monitor command anyway** so they can watch independently.

**Mode (a) has a hard ceiling of ~25 minutes.** Tracked background Bash tasks are reaped on a
30-minute wall-clock-aligned schedule — measured kills at `:13:18` and `:43:18` past the hour,
within 0.6s, across four sessions on two days (the phase has drifted before: two 2026-07-04
kills sat at `:01:59`/`:31:59`, same 30-minute period). Because the launch time is arbitrary
relative to that boundary, a driver's life is uniformly 0–30 minutes, which reads as "randomly
terminating" but is not random. This is harness-side and reaches the CLI as readily as the
remote/mobile control — nothing in the engine sends SIGTERM. **If the batch could run longer
than ~25 minutes, do not use mode (a)** — use (c) or (b).

**(c) Detached — on request, and the right default for any long batch.** Escapes the reaper by
putting the driver in its own session and process group, so the group-kill cannot reach it.
macOS has no `setsid` binary, so fork + `os.setsid()` in Python. Substitute RUN/PROJECT; it
returns immediately, leaving the driver running with `PPID 1`:

```
python3 -c "
import os,sys,subprocess
if os.fork(): sys.exit(0)
os.setsid()
log=open('runs/RUN/driver.log','a')
subprocess.run(['uv','run','orchestrator','--root','runs','--shared-root','--run','RUN',
                '--project','PROJECT','run-headless','--wait'],
               stdout=log,stderr=log,stdin=subprocess.DEVNULL)
"
```

The trade is that you get **no exit notification** — nothing is tracking the process — so you
must tell the human that, and verify liveness yourself rather than waiting to be told:

- Confirm detachment right after launch: `pgrep -f "run-headless"`, then
  `ps -o pid,ppid,pgid -p <pid>` — `PPID` must be `1` and `PGID` must differ from the session
  shell's. If `PPID` is not 1 it did not detach and the reaper still owns it.
- Poll `status` when you want to know where it is; `driver.alive` + `heartbeat_age_s` (#323)
  are authoritative, and `runs/<run>/driver.log` holds the stdout the notification would have
  carried.
- Killing it is now manual: `kill <pid>` from the `driver.jsonl` start record.

**(b) Handed to the human to run in the foreground.** Prefer this when the human said they
want to run it themselves, when the batch is long enough to outlive the session, or when it
is a live run against a real product repo. Give the command as a **single clean line, in its
own code block, with no trailing `#` comments** — interactive zsh has `interactive_comments`
off and will choke. Tell them the driver owns the run for its whole duration and to leave
that terminal alone.

In every mode, say these two things:

1. **Monitor from a second terminal** — `dashboard --watch`, or `watch --activity`. The
   driver also narrates itself to stderr and to `runs/<run>/driver.jsonl` (#323): heartbeats
   carrying tick, utilization, dispatch limit, and — while it sleeps — the wait reason. A
   long silence there is a stalled or dead driver, not a quiet one.
2. **What it will do outwardly** — how many PRs it may open, against which repo. For a live
   run against a real product repo, the human picks the issues and approves *before* any
   write or PR (AGENTS.md hard checkpoint), and mode (b) is the right choice.

## 3. Read the result (on the driver's exit notification, or when the human reports it done)
```
uv run orchestrator --root "$ROOT" --shared-root --run "$RUN" --project "$PROJECT" status
```
Gate on:
- `run_state` is `completed` / `failed`.
- `lane_audit.clean == true` — every recorded call `headless:Codex`, zero unattributed.
  **This lane is newly exercised**; if it reports false, check for an attribution bug in the
  headless path before concluding the run misbehaved.
- `events_audit` — `dispatched == recorded`. A non-zero `outstanding` alongside
  `"clean": true` means orphaned leases, not a healthy run (see REFERENCE.md).
- `driver` — `alive`, `heartbeat_age_s`, `last_state`, `exit_reason` (#323). Mid-run this is
  how you tell a driver sleeping out a capacity stall (`last_state:
  "waiting_on_capacity"`, fresh heartbeat) from one that is gone (`alive: false`); after a
  driver dies without an `exited` record, its last heartbeat bounds the time of death.

Then per AGENTS.md: merge the PRs in dependency order, verify each issue actually closed,
clean worktrees/branches/checkpoint tags — but **never** `rm -rf runs/<RUN>/`. Run
`trunk-gate` over the merged trunk. Offer `triage-followups` for the issues the run auto-filed.

## Recovery, resume, and lane details
If the run is stuck, a dispatch was killed, or `run-headless` returns immediately without
doing anything, read `REFERENCE.md` in this skill directory. Don't inline that material
here — it is only needed when something has already gone wrong.
