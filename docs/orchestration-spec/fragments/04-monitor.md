# Fragment: .claude/scripts/monitor-orchestrator.sh
Source commit: <uncommitted working tree at /Users/craigperler/Development/heysoo>   Mapped lines: 1–2254 (full file)

> The observability / monitor tool. Read-only over the orchestrator's state: it
> watches `status-ralph.json`, per-task status files, and log trees; renders a
> live dashboard; detects stale/crashed/stalled/rate-limited runs; and sends
> email + macOS notifications. It also owns a `--cleanup` mode that mutates state
> (the only write path). This fragment is a **D5 decision input**: see the
> dedicated D5 section for what observability would come *for free* from an
> in-session `/workflows` live-progress view vs. what must be ported.

---

## 1. Role & entry points — who invokes it, with what argv

Standalone CLI, run by a human (never invoked by the orchestrator itself). Shebang `#!/usr/bin/env bash`, `set -uo pipefail` (`monitor-orchestrator.sh:1,22`).

Five operating modes, dispatched in `MAIN` (`:2178–2254`):
- **`--cleanup` / `--dry-run`** → `run_cleanup` then exit (`:2183–2186`). The only state-mutating mode.
- **`--file <path>`** → reads the file; if `is_ralph_status` (has `.dependency_graph`) → `watch_ralph`, else `watch_status` (`:2189–2200`).
- **`--task <id>`** → `match_task_filter` resolves a status file, then `watch_status` (`:2203–2215`). Note: a ralph file matched here would still go to `watch_status`, not `watch_ralph`.
- **`--list`** → one-shot dump of live processes + on-disk status files, then exit (`:2218–2250`).
- **no args (default)** → `watch_unified` (`:2253`) — the unified ralph+direct dashboard.

Usage/help banner at `:46–67`. argv parsed at `:69–116`.

## 2. Inputs — every flag, env var, file read

**Flags** (`:69–116`):
| Flag | Var | Default | Effect |
|---|---|---|---|
| `--task <id>` | `TASK_FILTER` | "" | Monitor one task; `#` stripped; matches `status-issue-<id>.json`/`status-task-<id>.json`/logs (`:327–384`) |
| `--file <path>` | `STATUS_FILE_ARG` | "" | Monitor an explicit status file |
| `--list` | `LIST_MODE` | false | One-shot listing |
| `--interval <sec>` | `WATCH_INTERVAL` | 5 | Refresh/poll sleep seconds |
| `--email <addr>` | `EMAIL` | `$ORCHESTRATOR_EMAIL` | Enable Mail.app email alerts |
| `--stall-threshold <s>` | `STALL_THRESHOLD` | 1800 | Seconds of no-progress before a stall alert (help text wrongly says 900 at `:56,13`) |
| `--cleanup` | `CLEANUP_MODE` | false | Stale-state cleanup |
| `--dry-run` | `DRY_RUN` | false | Implies `--cleanup` (`:118–122`); shows actions without applying |
| `--help`/`-h` | — | — | Print usage, exit 0 |

**Env vars:** `ORCHESTRATOR_EMAIL` (seeds `EMAIL`, `:41`). Implicitly relies on `git` for `PROJECT_ROOT` (`:29`), and macOS `osascript`/Mail.app for notifications.

**Derived paths** (`:29–31`): `PROJECT_ROOT=$(git rev-parse --show-toplevel || pwd)`; `LOGS_DIR=$PROJECT_ROOT/logs/implement-roadmap-task`; `SCRIPTS_DIR=$PROJECT_ROOT/.claude/scripts`.

**Files READ** (full enumeration — this is the read-side contract):
1. **`status-ralph.json`** (CWD) — ralph scheduler aggregate. Keys read: `.dependency_graph` (detection `:1246`, render `:1374,1742`); `.state` (`:1411,1461`); `.progress.{total,completed,running,ready,blocked,retrying,permanently_failed,cascade_blocked}` (`:1290`); `.config.branch` (`:1292`); `.last_update` (`:1294`); `.tasks` map keyed by task-id, per-task `.tasks[id].{state,pid,attempt,max_retries,mode,status_file,depends_on,unmet_deps,last_error,retry_after}` (`:1307–1346,1591–1664,1978–1990`).
2. **Per-task status files** — `status-issue-<n>.json`, `status-task-<id>.json`, `status-ralph-issue-*.json`, and `status.json`. Found in CWD (`:288`), `SCRIPTS_DIR` (`:297`), and `LOGS_DIR/*/status.json` (`:307`). Fields read enumerated in §2-fields below.
3. **Process command lines** via `ps aux` / `ps -p <pid> -o args=` — to discover running orchestrators and extract `--status-file`/`--task`/`--resume` (`:201–255`).
4. **`$PROJECT_ROOT/$log_dir/orchestrator.log`** — incremental tail scanned for rate-limit/timeout strings (`:456–497`).
5. **`$PROJECT_ROOT/$log_dir/stages/*.log`** — newest-by-mtime stage log; `tail -20` for alert context (`:435–446`) and byte-size for zero-output detection (`:513–522`).
6. **`$PROJECT_ROOT/$log_dir/status.json`** — archived copy, read after the live per-task file is deleted on completion (`:864,1099`).
7. **`/tmp/.claude_usage_cache`** — API capacity %, written by `~/.claude/fetch-usage.sh` which the monitor *invokes* (`:579–586`); lines 1 (5h %) and 3 (reset time) read.
8. **GitHub** via `gh issue view <n> --json title` — enrich completion alerts (`:1791`).

**Per-task status-file fields read** (feeds the status-field registry):
`.issue` (`:375,742,833,1089,1673`), `.state` (`:245,311,743,872,959,1156,1411,1443,…`), `.current_stage` (`:744,1259,1869,2012`), `.last_update` (`:745,1294,2013`), `.execution_mode` (`:765,877,934,1328,1692,1798`), `.log_dir` (`:432,453,510,862,1097,1553,2016,2104`), `.branch` (`:797,2015`), `.worktree` (`:798,2014`), `.tasks[]` array with `.id/.status/.description/.review_attempts` (`:405,422,681–691,782,949`), `.stages` map with per-stage `.status/.task_progress/.iteration/.started_at/.completed_at` and `.stages.pr.pr_number` (`:403,419,656–698,936,1255–1272`), `.current_stage`/`.current_task`/`.substage`/`.substage_detail` (`:407,945,1265`), `.quality_iterations`/`.test_iterations`/`.pr_review_iterations` (`:692–694,789–790`), `.stages_skipped`/`.stages_executed` (`:648–654`), `.capacity_resets_at` (`:1849`), `.paused` via `.state == "paused"` (`:1846`). Cleanup additionally writes `.killed_by` (`:2093`).

## 3. Outputs — files written, exit codes, side effects

**Files written:**
- `/tmp/heysoo-monitor-alerts.log` — durable append-only alert journal (`:178–191`); every `alert()` call appends `[iso] ALERT: <subject>` + body + email-status line. (Persisted because the TUI `clear`s the screen.)
- **`--cleanup` only** (the sole non-`/tmp` writes):
  - Rewrites stale status file in place: `jq '.state="killed" | .killed_by="monitor --cleanup" | .last_update=(now|todate)'` via `tmp`+`mv` (`:2092–2095`).
  - Copies updated file to `$PROJECT_ROOT/$log_dir/status.json` archive (`:2106`).
- `watch_status` / `watch_unified` **delete** task-scoped status files: `rm -f "$file"` on completion (`:910`), and `_unified_cleanup` SIGINT trap `rm -f`s terminal/STALE status files (`:1448,1463,1481`).

**Exit codes:** 0 on normal completion / help / list / cleanup (`:66,2185,2199,2214,2249,2253`); 1 on missing flag value (`:72,77,86,91,96`), missing `--file` (`:2191`), no match for `--task` (`:2211`).

**Side effects (git / gh / network):**
- `git rev-parse` (`:29`); cleanup runs `git worktree remove --force` (`:2119`), `rm -rf` worktree fallback (`:2121`), `git worktree prune` (`:2122,2162`), `git show-ref` (`:2137`), `git log main..branch` (`:2140`), `git branch -D` (`:2144`).
- `gh issue view` to fetch titles (`:1791`).
- **Mail.app** outgoing message via `osascript` AppleScript (`:133–158`); **macOS notification** via `osascript -e 'display notification'` (`:164`).
- `kill -0 <pid>` liveness probes (`:1313,1598,1981`); `ps aux`/`ps -p` (`:204,214`).
- Shells out to `~/.claude/fetch-usage.sh` (`:579`).

## 4. Control flow — state machine, loops, caps, exit conditions

**No bounded state machine of its own** — it is a polling observer. Two structural loop shapes:

**(A) Single-file watch (`watch_status` `:854–1021`):** `while true` polling every `WATCH_INTERVAL`s (`:1020`). Reads file each tick; only re-renders when content changed (`:921`). Exit conditions: file gone with no archive (`:915`); terminal state detected (`state ∉ {running,initializing}` and non-empty → completion alert + `break` `:961–994`); `running` but no live process → crash alert + `break` (`:996–1010`). Every tick (regardless of render) runs the four proactive checks `_check_failures`/`_scan_orch_log`/`_check_zero_output_log`/`_check_stall` (`:1015–1018`).

**(B) Unified dashboard (`watch_unified` `:1494–1937`):** `while true` (`:1573`), `clear`+full redraw every tick. Each cycle: (a) collect ralph tasks from `status-ralph.json` (`:1585–1665`), (b) collect direct `status-issue-*`/`status-task-*` files (`:1668–1714`), classify each into icon/active/finished, render task lines + dependency graph (`:1716–1754`), then per-task alerting (`:1757–1924`). Exit when `active_count==0 && finished_count>0` → calls `_unified_cleanup` which `exit 0`s (`:1929–1933`). SIGINT → `_unified_cleanup` trap (`:1495,1432–1492`).

**Per-task liveness logic (the "stale running" detector):** central rule appears 3×:
- `is_process_alive(file)` (`:711–729`): cross-references `find_orchestrator_processes` PIDs against each PID's resolved `--status-file`; used for **direct** tasks — `running` + no matching process → relabel `STALE (no process)` (`:748,996,1677,1955`).
- For **ralph** tasks, uses the per-task `.pid` field directly: `kill -0 $pid` fails → STALE, *but* first re-checks the per-task status file (race guard: orchestrator exits before ralph-loop updates `status-ralph.json`) (`:1312–1316,1597–1611,1980`).

**`_check_stall` state machine (`:553–609`):** keeps `_ALERT_PREV_SNAPSHOT` + `_ALERT_LAST_PROGRESS`. `_snapshot_status` (`:397–412`) builds a `|`-joined string of every stage status + task status + `current_stage`/`current_task`/`last_update`. Snapshot changed → reset progress clock; unchanged for `≥STALL_THRESHOLD` → fire once (`_ALERT_STALL` latch). Before firing it consults capacity: `≥90%` → softer "Capacity Wait" alert instead of "Stall" (`:588–605`).

**Multi-task alert-state plumbing:** single-file globals `_ALERT_*` (`:391–395`) are swapped in/out of parallel per-task arrays in `watch_multi` (`:1204–1223`) and `watch_unified` (`:1875–1922`) — a manual save/restore because bash lacks per-key alert state. `watch_unified` adds a 600s alert cooldown reset on stage change (`:1866–1873,1893–1914`) and a first-cycle suppression (`:1571,1787,1865`).

## 5. External invocations — verbatim

- `ps aux | grep -E 'implement-orchestrator\.sh|implement-roadmap-task-orchestrator\.sh|implement-issue-orchestrator\.sh' | grep -v grep | grep -v monitor-orchestrator | awk '{print $2}'` (`:204–207`).
- `ps -p "$pid" -o args=` (`:214`).
- `gh issue view "$issue_num" --json title -q .title` (`:1791`).
- `osascript -e "display notification \"$message\" with title \"$title\""` (`:164`).
- Mail.app via embedded python3 heredoc → `osascript -e <applescript>` (`:133–158`).
- `bash ~/.claude/fetch-usage.sh` (`:579`).
- `git rev-parse --show-toplevel` (`:29`); cleanup git suite (`:2119–2162`, see §3).
- Many `jq -r` field extractions and two embedded `python3 -c` snippets (`_snapshot_status` `:399`, `_get_failed_stages` `:416`, `_build_completion_summary` `:642`).

**No `claude`/`codex` invocations** — the monitor never calls a model. (Pure observability + notification.)

## 6. Constants & tunables

- `WATCH_INTERVAL=5`s default (`:40`).
- `STALL_THRESHOLD=1800`s = 30 min (`:42`; comment/help disagree — see §9).
- `zero_output_threshold=300`s = 5 min, log `< 100` bytes (`:507,527`).
- Capacity-wait gate: 5h usage `≥ 90%` (`:588`).
- Alert cooldown in unified mode: `600`s = 10 min (`:1893`).
- `tail -20` stage-log context (`:443`); `tail -5` alert body (`:174`); `.last_error[:120]` truncation (`:1345`); description `[:60]` (`:688`).
- Default `max_retries` fallback `3`, `attempt` fallback `1` (`:1593,1592`).
- Alert log path `/tmp/heysoo-monitor-alerts.log` (`:178`); usage cache `/tmp/.claude_usage_cache` (`:581`).
- Hardcoded PR URL base `https://github.com/cperler/heysoo/pull/` (`:701`).
- Stage canonical order pinned in `_render_stage_line` (`:1255`): `setup,research,evaluate,plan,implement,quality_loop,test_loop,docs,pr,pr_review,complete`.

## 7. Failure handling

The monitor has **no retry/backoff/circuit-breaker of its own** — it *reports on* the orchestrator's. Its "failure handling" is alert classification (each alert fired at most once via a latch/seen-set):
- **Failures:** `_check_failures` (`:611–638`) scans stages/tasks for `status ∈ {failed,error}`, dedupes via `_ALERT_FAILURES` pipe-set, attaches stage-log tail.
- **Rate limit:** `_scan_orch_log` greps new orchestrator.log lines for `rate.limit|all models rate-limited|waiting.*before retrying` (`:469`) → "Rate Limit" alert. Force-push detection: **none found** (no grep for force-push/`push --force`); the only push-related signal is generic rate-limit/timeout log scanning.
- **Stage timeout:** grep `timed out after` (`:483`).
- **Zero-output:** newest stage log `< 100` bytes for `≥300`s → "Zero Output" alert (proxy for a silent rate-limit/hang) (`:504–551`).
- **Stall:** snapshot-unchanged for `≥STALL_THRESHOLD`; capacity-aware downgrade to "Capacity Wait" at `≥90%` (`:553–609`).
- **Crash / stale:** `running` + dead process → "Crashed" / "Stale" alert (`:996,1188,1819`).
- **Paused:** `.state=="paused"` → one "Paused: waiting for capacity" alert, suppresses stall/zero-output (`:1846–1862`).
- **Completion:** terminal state → completion/failure alert with `_build_completion_summary` (`:640–703`).
- Email send failure is logged but non-fatal (`:182–192`); all alert side-channels best-effort (`2>/dev/null`).

## 8. Coupling — generic vs Hey Soo!-specific

| Item | Verdict | Generic shape |
|---|---|---|
| File-watch / poll-and-diff loop | **Generic** | Reusable: watch a status file, re-render on change. |
| Stale-running detection (status says running, process dead) | **Generic, valuable** | Liveness oracle: `(state, pid?) → alive?`. Independent of language. |
| `status-ralph.json` schema (`dependency_graph`, `progress.*`, `tasks[id].*`) | **Schema-coupled** to ralph engine | Must track whatever the engine's status schema becomes; this is the read-side contract. |
| Stage canonical order list (`:1255`) | **Pipeline-coupled** | The 11-stage list duplicates the orchestrator's pipeline — must be config-driven, not hardcoded. |
| `execution_mode ∈ {full,lite,micro}` badges | **Pipeline-coupled** | Should read whatever modes the engine defines. |
| `LOGS_DIR=logs/implement-roadmap-task`, `status-issue-*`/`status-task-*` naming, `status-ralph-issue-*` | **Hey Soo!-specific paths/naming** | Parameterize log root + status-file glob conventions. |
| Process-name grep (`implement-orchestrator.sh` etc.) | **Hey Soo!-specific** | Configurable process matcher, or replace with a PID registry the engine writes. |
| Mail.app + `osascript` notifications | **Platform-specific (macOS)** | Pluggable notifier (email/desktop/none); the alert *content* is generic, the *transport* is not. |
| `~/.claude/fetch-usage.sh` + `/tmp/.claude_usage_cache` capacity probe | **Claude-subscription-specific** | Capacity oracle should be an adapter (the engine already throttles on capacity elsewhere). |
| `gh issue view` title enrichment, `github.com/cperler/heysoo` PR URL | **Hey Soo!/GitHub-specific** | Optional issue-tracker adapter; URL from config. |
| `--cleanup` (worktrees, branches, status files) | **Mostly generic, git-coupled** | Reusable given the engine's worktree/branch conventions; `base="main"` hardcoded (`:2138`). |
| Failure-classification regexes (rate-limit/timeout strings) | **Engine-log-format-coupled** | Must match whatever the engine logs; ideally the engine emits structured events, not greppable prose. |

## 9. Anomalies — suspected bugs, dead code, contradictions

1. **Stall-threshold default inconsistency — THREE conflicting values** (verifier-corrected). Code default `STALL_THRESHOLD=1800`s (30 min, `:42`); usage banner says `default: 900` (`:56`); header example shows `--stall-threshold 600  # … 10min` (`:13`, and 600 ≠ 10 min anyway). Documentation bug across three numbers.
2. **`--task` matching a ralph file goes to `watch_status`, not `watch_ralph`.** Task-filter mode unconditionally calls `watch_status` (`:2213`) with no `is_ralph_status` branch (unlike `--file` at `:2194`). A ralph status file resolved by `--task` would be misrendered as a per-task file.
3. **`watch_multi` is dead code.** Defined (`:1030–1237`) and carries `# shellcheck disable=SC2329` (acknowledging it's unreferenced) — no call site reaches it; default path is `watch_unified` and the other modes use `watch_status`/`watch_ralph`. Multi-task coverage is fully handled by `watch_unified`.
4. **Hardcoded PR URL `cperler/heysoo`** (`:701`) — wrong for any other repo; should derive from `gh repo view` or config.
5. **`base="main"` hardcoded in cleanup** (`:2138`) — branch-emptiness check assumes `main` is the base; wrong for repos branching off other bases.
6. **Capacity probe path coupling.** `_check_stall` calls `~/.claude/fetch-usage.sh` and reads `/tmp/.claude_usage_cache` by line number (`:584–586`) — brittle positional parse, Claude-subscription-only, silently no-ops elsewhere.
7. **No force-push / push-failure detection** despite the task's hypothesis — the only push-adjacent signal is the generic rate-limit/timeout log grep. (Recorded as DISPUTED below.)
8. **vs `docs/orchestration-template.md`:** the design doc's §2 observability list ("stage index, per-stage logs, stream replay, learnings, convergence, retrospectives") describes the *orchestrator's* outputs; this monitor is **not** mentioned in the source inventory (§8 of that doc lists ralph/common/implement/batch/lib/schemas but **omits `monitor-orchestrator.sh` entirely**) — so the monitor is an undocumented component relative to the plan. Its capacity-throttle awareness mirrors the doc's "capacity throttle" but is a *read-only mirror*, not the throttle itself.

---

## D5 DECISION INPUT — port vs. free-from-`/workflows`

**What the monitor provides, split by whether an in-session interactive `/workflows` live-progress view gives it for free:**

**FREE from `/workflows` (in-session) — do NOT port:**
- **Live per-stage/per-task progress rendering** (`print_status_summary`, `_render_stage_line`, stage pipeline bar, current-stage/substage detail). In an interactive workflow the supervisor *already holds* live task state in context and the `/workflows` view renders agent/sub-step progress natively. The whole "poll a JSON file every 5s and diff it" machinery (`watch_status`/`watch_unified`/`_snapshot_status`) exists **only because** headless bash had no other window into the run. Eliminated by the paradigm.
- **Stale-"running"/crash detection** (`is_process_alive`, `kill -0` on `.tasks[].pid`). These detect a dead *OS process* whose status file still says running — a failure mode of detached headless processes. In-session subagents can't "crash silently behind a stale file": their completion/failure returns to the supervisor synchronously. **Largely obviated** by the execution-mode flip (per `docs/orchestration-template.md` §4 friction-point 1, the supervisor *is* the liveness signal).
- **Completion/terminal-state detection + summary** (`_build_completion_summary`). The workflow's `agent()` return *is* the completion event; no need to detect it by watching a file disappear and reading an archive copy.
- **Multi-task dashboard aggregation** (`watch_unified` ralph+direct merge). The supervisor already enumerates its dispatched tasks.

**MUST be ported (or re-homed) — NOT free:**
- **Out-of-session / cross-session observability.** The single biggest value: watching a *headless* ralph run, or a run started in another session, or after the supervisor session died. `docs/orchestration-template.md` §4 friction-point 1 keeps file-based status precisely so "any new session (or bash ralph on credit) resumes from `status-ralph.json`" — a monitor that reads those files from *outside* the running session stays necessary for the headless×claude fallback lane and for resume-after-supervisor-death.
- **Proactive notifications (email + desktop) on a schedule.** Stall, zero-output, rate-limit, capacity-wait, paused, completion alerts pushed to a human who is *away from the terminal*. `/workflows` shows progress only to someone *looking at the session*. For "kick off a batch before bed" this is the actual product — and it's exactly the unattended slice the design doc says the credit buys. Must be ported as an engine-emitted event → notifier adapter (decouple transport from macOS/Mail.app).
- **`--cleanup`** (stale status files → `killed`, orphan worktree/branch reclamation). State hygiene independent of any view; port as an engine maintenance command.
- **Capacity-aware alert downgrade** (stall vs "at capacity"). Useful signal regardless of lane, but should consume the engine's existing capacity oracle rather than re-shelling `fetch-usage.sh`.
- **Durable alert journal** (`/tmp/heysoo-monitor-alerts.log`) — minor, but the "screen gets cleared so persist alerts" rationale survives.

**Net D5 read:** the monitor is ~60–70% *scaffolding for headless's blindness* (poll/diff/liveness/dashboard) that the interactive `/workflows` view renders moot, and ~30–40% *genuinely independent observability* (out-of-session watching, away-from-keyboard push notifications, cleanup) that the in-session view does **not** provide and that must be ported as small engine-emitted-event + notifier-adapter pieces. The notification + cleanup core is the keeper; the polling/rendering/liveness engine is the throwaway.

DISPUTED:
- The task brief hypothesizes **force-push / rate-limit detection**. Rate-limit detection is confirmed (`:469`). **Force-push detection was NOT found** anywhere in the file — no grep for force-push/`push --force`/`--force-with-lease`. If it exists it lives in the orchestrator's own log output, not in the monitor.
- The brief cites `dependency_graph` detection at `:1243`; the actual `jq -e '.dependency_graph'` test is at **`:1246`** (the comment header is at `:1243`).
- Whether stale-running detection is *fully* free in-session is mode-dependent: free for interactive×claude, but a headless×claude fallback run still produces detached processes whose liveness the monitor (or an equivalent) must check — so it is "free for the default lane, ported for the fallback lane," not unconditionally free.
