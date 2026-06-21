# Monitor Orchestrator

Source commit: uncommitted working tree at `/Users/craigperler/Development/heysoo`
File: `.claude/scripts/monitor-orchestrator.sh` (2254 lines)

---

## What monitor-orchestrator does

Standalone CLI run by a human (never invoked by the orchestrator itself). Five modes:

| Mode | Trigger | Behavior |
|------|---------|----------|
| **Unified dashboard** (default) | no args | `watch_unified`: poll every `WATCH_INTERVAL`s (default 5s), full redraw; aggregates ralph tasks + direct tasks; exits when `active_count==0 && finished_count>0` |
| **Single-file watch** | `--file <path>` or `--task <id>` | `watch_status`: poll on change only; exits on terminal state or crash detection |
| **Ralph watch** | `--file` pointing to a ralph status file | `watch_ralph` (differs from `watch_status` only when `--file` is used; `--task` always goes to `watch_status` — see anomalies) |
| **List** | `--list` | One-shot dump of live processes + on-disk status files, then exit |
| **Cleanup** | `--cleanup` / `--dry-run` | `run_cleanup`: the only state-mutating mode; rewrites stale status files (`state="killed"`), removes orphan worktrees and branches via `git worktree remove`, `git branch -D` |

### Poll / diff / dashboard

`_snapshot_status` builds a `|`-joined string of every stage status + task status +
`current_stage`/`current_task`/`last_update`. The unified loop re-renders on every tick
(`clear` + full redraw); the single-file loop re-renders only when the snapshot changes.
No model is ever called — the monitor is pure observability.

### Liveness / stale / crash detection

Two mechanisms:

- **Direct tasks:** `is_process_alive(file)` cross-references `find_orchestrator_processes`
  PIDs (from `ps aux` grep on `implement-orchestrator.sh` etc.) against each PID's
  `--status-file` argument. `running` + no matching process → STALE.
- **Ralph tasks:** uses per-task `.pid` field directly; `kill -0 $pid` fails → STALE,
  with a race guard that re-checks the per-task status file before alerting.

### Stall detection

`_check_stall` (`:553–609`): compares successive snapshots; snapshot unchanged for
`≥STALL_THRESHOLD` → fire stall alert once. Capacity-aware: if 5h API usage `≥90%`
(read from `/tmp/.claude_usage_cache` via `~/.claude/fetch-usage.sh`), downgrades to a
softer "Capacity Wait" alert instead.

**Stall threshold has three conflicting values:**
- Code default `STALL_THRESHOLD=1800`s (30 min) — `:42`
- Usage banner says `default: 900` — `:56`
- Header example shows `--stall-threshold 600` with the comment `# 10min` — `:13` (600s ≠ 10 min)

Documentation bug; 1800s is the operative value.

### Alerts

Each alert fires at most once via a latch / seen-set:

| Alert | Trigger |
|-------|---------|
| **Rate Limit** | `_scan_orch_log` greps new `orchestrator.log` lines for `rate.limit\|all models rate-limited\|waiting.*before retrying` (`:469`) |
| **Stage Timeout** | grep `timed out after` (`:483`) |
| **Zero Output** | newest stage log `< 100` bytes for `≥300`s — proxy for silent hang (`:504–551`) |
| **Stall** | snapshot unchanged for `≥STALL_THRESHOLD` (`:553–609`) |
| **Capacity Wait** | Stall + API usage `≥90%` — softer variant (`:588–605`) |
| **Crashed / Stale** | `running` + dead process (`:996,1188,1819`) |
| **Paused** | `.state=="paused"` — suppresses stall/zero-output (`:1846–1862`) |
| **Completion / Failure** | terminal state → `_build_completion_summary` with stage-log tail (`:640–703`) |

Transports: macOS `osascript` desktop notification (`:164`); Mail.app via embedded
AppleScript (`:133–158`); durable append-only journal at `/tmp/heysoo-monitor-alerts.log`
(screen gets cleared between redraws, so alerts must persist elsewhere).

**Force-push detection does NOT exist in monitor** — confirmed by search. The only
push-adjacent signal is the generic rate-limit/timeout log grep. An earlier hypothesis
that the monitor detected force-pushes is refuted.

### `--cleanup`

Rewrites stale status files in place (`jq '.state="killed" | .killed_by="monitor --cleanup"'`
via tmp+mv), copies to the log-dir archive, removes orphan git worktrees and branches
(`git worktree remove --force`, `git branch -D`), runs `git worktree prune`.
`base="main"` is hardcoded (`:2138`) — wrong for non-main base branches.

---

## D5 Verdict

**~60–70% is made free by in-session `/workflows` for the interactive × claude default lane;
~30–40% must be ported.**

### Free from `/workflows` — do NOT port

- **Poll / diff / dashboard rendering** (`watch_status`, `watch_unified`, `_snapshot_status`,
  `_render_stage_line`). This whole machinery exists only because headless bash had no other
  window into the run. An in-session supervisor already holds live task state; `/workflows`
  renders agent/sub-step progress natively.
- **Stale-running / crash detection** (`is_process_alive`, `kill -0`). Detects dead OS
  processes whose status file still says running — a failure mode of detached headless
  processes. In-session subagents cannot crash silently: their completion returns to the
  supervisor synchronously.
- **Completion / terminal-state detection**. The workflow's `agent()` return is the
  completion event; no need to detect it by watching a file disappear.
- **Multi-task dashboard aggregation** (`watch_unified` ralph+direct merge). The supervisor
  already enumerates its dispatched tasks.

### Must be ported — NOT free from in-session view

- **Out-of-session / cross-session observability.** The single biggest value: watching a
  headless ralph run, a run started in another session, or a run after the supervisor
  session died. File-based status (`status-ralph.json`) is kept precisely for resume-after-
  supervisor-death; a monitor that reads those files from outside the running session stays
  necessary for the headless × claude fallback lane.
- **Away-from-keyboard push notifications** (stall, zero-output, rate-limit, capacity-wait,
  paused, completion). `/workflows` shows progress only to someone looking at the session.
  For "kick off a batch before bed" this is the core product — the unattended slice the
  design doc says the credit buys. Port as: engine emits structured events → pluggable
  notifier adapter (decouple from macOS / Mail.app transport).
- **`--cleanup`** (stale status files, orphan worktrees/branches). State hygiene independent
  of any view; port as an engine maintenance command.
- **Capacity-aware alert downgrade** (stall vs "Capacity Wait"). Useful regardless of lane;
  should consume the engine's capacity oracle rather than re-shelling `fetch-usage.sh`.
- **Durable alert journal.** Minor, but the rationale (screen clears between redraws) survives.

Note: stale-running detection is free for interactive × claude but must be ported for the
headless × claude fallback lane, where detached processes can still go dark.

---

## Anomalies

1. **`--task` matching a ralph file goes to `watch_status`, not `watch_ralph`** — misrendered
   as a per-task file (`:2213`).
2. **`watch_multi` is dead code** (`:1030–1237`; `# shellcheck disable=SC2329` acknowledges
   this); no call site reaches it.
3. **Hardcoded PR URL `cperler/heysoo`** (`:701`) — wrong for any other repo.
4. **`base="main"` hardcoded in cleanup** (`:2138`).
5. **`monitor-orchestrator.sh` is absent from the design doc's source inventory** (§8 of
   `docs/orchestration-template.md` lists ralph/common/implement/batch/lib/schemas but omits
   monitor entirely) — undocumented component relative to the plan.
