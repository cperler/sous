# Fragment: ralph-loop.sh (the batch scheduler)
Source commit: <uncommitted working tree, read 2026-06-20>   Mapped lines: 1–2299 (full file)
Cross-refs into `lib/orchestrator-common.sh` cited where the scheduler reaches into shared engine.

All paths absolute. `RL` = `/Users/craigperler/Development/heysoo/.claude/scripts/ralph-loop.sh`.
`OC` = `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`.

---

## 1. Role & entry points — who invokes it, with what argv

Ralph Wiggum smart scheduler — dependency-aware concurrent task orchestration with
retry-with-learnings, dynamic unlocking, capacity throttle, and a persistent work queue
(`RL:3–6`). It sits ABOVE the per-task orchestrator: it fans out N concurrent invocations
of `implement-roadmap-task-orchestrator.sh` (`RL:1408`), one child process per task.

Entry points (`RL:8–11, 102–161`):
- `ralph-loop.sh --tasks "5.1.1, 5.1.2, 54, 65" --branch main [--max-retries N] [--max-concurrent N] [--cooldown S] [--sequential] [--skip-analysis] [--agent NAME] [--micro|--lite|--full] [--force] [--queue|--no-queue] [--idle-timeout N]`
- `ralph-loop.sh --resume [options]` — resume from `status-ralph.json`
- `ralph-loop.sh --enqueue "114, 94" --branch main` — atomically append a batch to `ralph-queue.json` and exit (no lock needed) (`RL:2181–2188`)

`main()` is guarded so the file can be `source`d for unit tests without executing
(`RL:2297–2299`); arg parsing is likewise guarded (`RL:168, 261`).

Task-ID grammar: bare numbers → GitHub issue `#NNN` (`RL:341–345`); dotted IDs (`5.1.2`)
treated as roadmap tasks. Per-task suffix tags: `82:lite`, `136:full`, `421:micro` (mode);
`82:codex`, `83:claude` (provider); combinable `84:codex:lite` (`RL:108, 147–148, 348–376`).

## 2. Inputs — flags, env vars, files read

Flags (parse loop `RL:170–259`):
| Flag | Var | Default | Effect |
|------|-----|---------|--------|
| `--tasks` | `TASKS` | "" | comma task list (`RL:171`) |
| `--branch` | `BRANCH` | "" | base branch, required for new runs (`RL:176`) |
| `--resume` | `RESUME_MODE` | false | resume from status file (`RL:181`) |
| `--max-retries` | `MAX_RETRIES` | 3 | per-task retry cap (`RL:37,185`) |
| `--max-concurrent` | `MAX_CONCURRENT` | 3 | parallel orchestrators (`RL:38,190`) |
| `--cooldown` | `COOLDOWN` | 120 | seconds between retries (`RL:39,195`) |
| `--sequential` | `SEQUENTIAL` | false | sets MAX_CONCURRENT=1 (`RL:200–203`) |
| `--skip-analysis` | `SKIP_ANALYSIS` | false | all tasks independent (`RL:205`) |
| `--queue`/`--no-queue` | `QUEUE_MODE` | true | queue ingestion on/off (`RL:96,209–214`) |
| `--enqueue` | `ENQUEUE_TASKS` | "" | append-and-exit path (`RL:217`) |
| `--idle-timeout` | `IDLE_TIMEOUT` | 300 | seconds to wait for queue when empty, 0=exit now (`RL:42,222`) |
| `--micro` | `FORCE_MODE`="micro" | "" | also forces MAX_CONCURRENT=1+SEQUENTIAL (`RL:227–234`) |
| `--lite`/`--full` | `FORCE_MODE` | "" | global mode (`RL:235–242`) |
| `--force` | `FORCE_LOCK` | false | override instance lock (`RL:243`) |
| `--agent` | `AGENT` | "" | passed through to orchestrator (`RL:247`) |

Env vars READ:
- `ORCHESTRATOR_PROVIDER` (default `claude`) — `RL:1167, 1844`. Codex mode skips capacity probe.
- `BASELINE_UNIT_TIMEOUT` (default 1200) — `RL:727`
- `BASELINE_E2E_TIMEOUT` (default 1800) — `RL:740`
- `TASK_PROVIDER` — read in `OC` (`should_use_codex`, `OC:206`), not directly in RL.
- `CLAUDECODE` — UNset before child claude calls (`OC:282`).

Files read: `status-ralph.json` (`RL:32`), `ralph-queue.json` (`RL:33`), `/tmp/heysoo-ralph.lock`
(`RL:34`), `/tmp/.claude_usage_cache` (`RL:61`), per-task `status-ralph-issue-NNN.json`,
per-task logs, `$LOG_BASE/shared-baseline.json`, schemas in `$SCRIPT_DIR/schemas/`.
Also invokes `~/.claude/fetch-usage.sh` to refresh the usage cache (`RL:64`).

## 3. Outputs — files written, exit codes, side effects

Files WRITTEN:
- `status-ralph.json` — master state. Top-level: `state`, `tasks{}`, `dependency_graph{}`,
  `config{max_retries,max_concurrent,cooldown,branch,queue_mode,idle_timeout,queue_file}`,
  `progress{total,completed,running,blocked,ready,retrying,permanently_failed,cascade_blocked}`,
  `log_dir`, `last_update` (`RL:881–935`). Per-task value fields: `state, attempt, max_retries,
  depends_on, reason, mode, mode_reason, unmet_deps, pid, status_file, last_error, retry_after,
  completed_at, error_signatures[]` (`RL:894–911`). Transient throttle fields `state="throttled"`
  + `throttle_reason="aggregate_capacity"` (`RL:1852`); `state="killed"` on signal (`RL:2143–2144`).
- `ralph-queue.json` — JSON array of `{tasks, branch, enqueued_at}` (`RL:451–455`).
- `logs/ralph-YYYYMMDD-HHMMSS/` (`RL:673`): `ralph.log`, `dependency-analysis.json` (`RL:1211`),
  `shared-baseline.json` (`RL:705,776–787`), `learnings-issue-NNN.md`, `task-issue-NNN.log`,
  `summary.json` (`RL:2079–2092`).

Exit codes: `0` all completed; `1` some failed (`completed_with_errors`); `2` all remaining
permanently-failed/cascade-blocked; `3` config/arg error (`RL:13–18, 2289–2293`).

Side effects: spawns background child orchestrator processes (`RL:1413` `&`); kills child
process GROUPS on signal (`kill -- -PID`, then `-9`, `RL:2124,2135`); writes/removes lock file;
runs unit/E2E test scripts on the base branch for baseline (`RL:730,744`).

## 4. Control flow — state machine + loop structure (every claim cited)

Per-task state machine (status field `.tasks[id].state`):
`blocked` → `ready` → `running` → {`completed` | failed→(`retrying`→`ready` | `permanently_failed`)};
plus `cascade_blocked` (dependent of a permanent failure) and `killed` (signal).
- Initial state: `ready` if `depends_on` empty else `blocked` (`RL:897`).
- `reevaluate_blocked_tasks` (`RL:985–1011`): for each `blocked`, if all deps now `completed`
  → mark `ready` and zero `unmet_deps`; else update `unmet_deps`.
- `running` set in `launch_orchestrator` (`RL:1367`); attempt incremented there (`RL:1369`).
- `check_task_completion` (`RL:1422–1458`): returns 1 (running, incl. `paused`→still running
  `RL:1448–1450`), 0 (success: state `completed`/`already_closed`/`no_changes` OR exit 0),
  2 (failed).

`main()` flow (`RL:2177–2294`): enqueue early-exit → (resume: load_resume_state | new: parse
overrides → normalize → dependency analysis → kick deploy-deps → init_ralph_status) → micro
sequential guard (`RL:2262–2269`) → capture_shared_baseline → init_queue_file → `run_loop` →
`print_summary` → exit.

`run_loop` (`RL:1768–2024`) — `while true`:
1. **Queue poll** (queue mode): `dequeue_batch` → `ingest_batch`; on ingest failure
   `requeue_batch_entry` (re-prepend) (`RL:1777–1790`).
2. **Exit/idle check**: if `!tasks_remaining` → in queue mode idle-wait, exit when
   `elapsed >= IDLE_TIMEOUT` (`RL:1793–1812`), polling every `QUEUE_IDLE_POLL=15`s.
3. **Retry-ready promotion**: retrying tasks whose `retry_after` elapsed → `ready` (`RL:1829–1835`).
4. **Capacity check** then **LAUNCH** (see §7 / §dispatch below) (`RL:1840–1883`).
5. `sleep POLL_INTERVAL` (=30s) (`RL:1886`).
6. **Poll running tasks** (`RL:1888–1952`): per task switch on completion result —
   0=mark completed + `reevaluate_blocked_tasks` (unlock dependents); 2=fail path
   (extract_learnings → circuit-breaker → retry-or-permanent-fail).
7. **Exit conditions** (`RL:1956–2022`): `active==0 && blocked==0` → done; `active==0 &&
   blocked>0` → (queue mode: drain queue / idle-wait, else) cascade-block all remaining
   blocked tasks and break.

**MAX_CONCURRENT dispatch loop** (`RL:1869–1883`): iterate `get_ready_tasks`; per task,
re-read `running_count` and launch only while `running_count < MAX_CONCURRENT`, else `break`.
Codex-provider tasks bypass the capacity gate (`RL:1874`).

## 5. External invocations — verbatim

**Child orchestrator** (`RL:1408–1413`):
```
"$SCRIPT_DIR/implement-roadmap-task-orchestrator.sh" \
    --task "$task_id" --branch "$BRANCH" --status-file "$task_status_file" \
    ${extra_args[@]+"${extra_args[@]}"}   >> "$task_log" 2>&1 &
```
`extra_args` may add: `--agent`, `--micro`/`--lite`, `--provider <p>` (when task provider≠claude,
`RL:1387–1389`), `--learnings-file <file>` (`RL:1393`), `--baseline-file <file>` (`RL:1398`),
`--resume` (`RL:1406`).

**Dependency analysis** (`RL:1172`):
```
run_provider_oneshot "$prompt" "$SCHEMA_DIR/ralph-dependency-analysis.json" output
```
**Learnings summary** (`RL:1515–1520`):
```
run_provider_oneshot "<learnings prompt>" "$SCHEMA_DIR/ralph-learnings-summary.json" raw_summary 180
```
Both dispatch through `OC:run_provider_oneshot` (`OC:242–329`). On the **claude** path that
function hardcodes (`OC:273–278`):
```
claude -p "$prompt" --model "claude-sonnet-4-6" --dangerously-skip-permissions \
    --output-format json [--json-schema "$schema_path"]   (wrapped in env -u CLAUDECODE; timeout via gtimeout --kill-after=10)
```
On the **codex** path (`OC:309–316`): `codex exec --skip-git-repo-check --full-auto [--add-dir
<git-common-dir>] --color never --json --output-last-message <tmp> "$prompt"`.

**Capacity / baseline / git** (in RL): `bash ~/.claude/fetch-usage.sh` (`RL:64`);
`timeout <s> bash .claude/scripts/test-unit.sh` (`RL:730`); `timeout <s> bash
.claude/scripts/e2e-smoke.sh` (`RL:744`); `git -C "$repo_root" ls-files / rev-parse`
(`RL:741,774`). All status mutation is `jq` write-to-`.tmp`-then-`mv` (atomic), e.g. `RL:947`.

## 6. Constants & tunables

| Const | Value | Line |
|-------|-------|------|
| `MAX_RETRIES` | 3 | `RL:37` |
| `MAX_CONCURRENT` | 3 | `RL:38` |
| `COOLDOWN` | 120 s | `RL:39` |
| `POLL_INTERVAL` | 30 s | `RL:40` |
| `CIRCUIT_BREAKER_IDENTICAL` | 3 | `RL:41` |
| `IDLE_TIMEOUT` | 300 s | `RL:42` |
| `QUEUE_IDLE_POLL` | 15 s | `RL:43` |
| capacity throttle threshold | `>= 80%` 5-h util | `RL:79` |
| per-task capacity threshold (OC, for contrast) | `>= 90%` | noted at `RL:77` |
| `BASELINE_UNIT_TIMEOUT` | 1200 s | `RL:727` |
| `BASELINE_E2E_TIMEOUT` | 1800 s | `RL:740` |
| learnings-summary timeout | 180 s | `RL:1520` |
| dependency-analysis skip threshold | `task_count <= 2` | `RL:1079` |
| cleanup grace before SIGKILL | `sleep 1` | `RL:2130` |
| Lock file | `/tmp/heysoo-ralph.lock` | `RL:34` |

Model pins (cross-ref, NOT set in RL): `OC:MODEL_CHAIN=("claude-opus-4-7" "claude-sonnet-4-6"
"claude-haiku-4-5-20251001")` (`OC:49`); `MODEL_FALLBACK_DELAY=10` (`OC:50`); one-shot calls
hardcode `claude-sonnet-4-6` (`OC:275`).

## 7. Failure handling — retries, circuit breaker, cascade, capacity

**Retry-with-learnings** (failure path `RL:1923–1950`):
1. `extract_learnings` (`RL:1461–1558`) APPENDS a `## Attempt N` block to
   `$LOG_BASE/learnings-issue-NNN.md` (`RL:1465, 1473–1498`): state, failed stage, completed
   stages, last-10-lines of task log, then a best-effort AI `Analysis:` paragraph from
   `run_provider_oneshot` (`RL:1515–1534`). It also computes an `error_sig` = `"<stage>:<first
   100 chars of 3rd-from-last log line>"` and pushes it onto `.error_signatures[]` (`RL:1538–1555`).
   NOTE: learnings are APPENDED (newest at bottom), NOT prepended; the file is later passed to
   the child via `--learnings-file` (`RL:1392–1394`).
2. **Circuit breaker** `check_circuit_breaker` (`RL:1561–1581`): inspects the LAST 3
   (`.[-3:]`) error signatures; trips when `total >= CIRCUIT_BREAKER_IDENTICAL (3)` AND all 3
   are identical (`unique_count == 1`). Trip → `mark_permanently_failed` (`RL:1942–1944`).
3. Else if `attempt >= MAX_RETRIES` → `mark_permanently_failed` (`RL:1945–1946`).
4. Else `schedule_retry` (`RL:1622–1637`): sets `retry_after = now + COOLDOWN`, state→`retrying`,
   pid→null. `is_retry_ready` (`RL:1640–1656`) promotes back to `ready` once cooldown elapses.

**Cascade-blocking** `cascade_block` (`RL:1593–1620`): on permanent failure, every task in
state `pending|blocked|ready` whose `depends_on` contains the failed id → `cascade_blocked`.
Also a terminal-state cascade: when only blocked tasks remain with nothing active (and not in
queue mode), ALL remaining blocked tasks are cascade-blocked and the loop breaks (`RL:2014–2021`;
queue-mode equivalent at `RL:2001–2008`).

**Capacity throttle** (`RL:1838–1867`, helper `check_ralph_capacity` `RL:60–84`): before
launching, refresh `/tmp/.claude_usage_cache` via fetch-usage.sh, read 5-h util (line 1). If
`>= 80%` AND `current_running > 0` → set `RALPH_CAPACITY_THROTTLED=true`, status→`throttled`,
`capacity_ok=false`, hold non-codex launches. Lifted when util drops; codex-provider tasks and
codex global mode bypass entirely (`RL:1844, 1874`). Aggregate throttle is proactive (80%) vs
the per-task OC throttle (90%, `RL:77`).

**Crash recovery** `load_resume_state` (`RL:1662–1740`): stale `running`→`ready`+extract_learnings;
`permanently_failed`→reset to `ready`, attempt 0, clear signatures; `cascade_blocked`→`blocked`
for re-eval; then `reevaluate_blocked_tasks`.

---

## ROUTING TABLE (required) — per call → provider × model+fallback × execution surface

Two provider levers (documented `OC:130–143`): **(A) `ORCHESTRATOR_PROVIDER`** global env
(`claude`|`codex`, default claude — `OC:173–189`) routes EVERY stage; **(B) per-task `:codex`
tag** → `TASK_PROVIDER`, narrows codex to file-patching stages only via `CODEX_ELIGIBLE_STAGES`
allowlist (`OC:145–166, 191–208`). The model fallback chain `MODEL_CHAIN` is Opus→Sonnet→Haiku
(`OC:49`) and applies ONLY inside `run_stage`-routed stage calls — NOT to the scheduler's own
one-shot calls.

| Call (where) | Provider selection | Model | Fallback chain | Execution surface |
|---|---|---|---|---|
| Dependency analysis (`RL:1172`) | `run_provider_oneshot` → `get_orchestrator_provider` (global env only; ignores per-task tags) | **HARDCODED `claude-sonnet-4-6`** on claude path (`OC:275`); codex default model on codex path | **NONE** — no MODEL_CHAIN tiering, no Opus/Haiku fallback | one-shot `claude -p`/`codex exec`; on non-zero rc Ralph falls back to `run_dependency_analysis_fallback` (independent mode) (`RL:1174–1179`) |
| Learnings summary (`RL:1515`) | same — `run_provider_oneshot`, global env only | **HARDCODED `claude-sonnet-4-6`** (`OC:275`) | NONE; 180s timeout, rc 124 → "(timed out)" string (`RL:1524`) | one-shot, best-effort/decorative |
| Child task orchestrator (`RL:1408`) | per-task `.provider` from status (claude|codex), passed as `--provider` only if ≠claude (`RL:1384–1389`) | decided INSIDE child by `run_stage`/`select_model_for_stage` (`OC:53–69`): Haiku=mechanical, Opus=reasoning, Sonnet=eval, default Opus | full `MODEL_CHAIN` Opus→Sonnet→Haiku with `MODEL_FALLBACK_DELAY=10` (`OC:49–50, 2150, 3213`) | background process; full pipeline; mode flag `--micro`/`--lite` narrows surface |

**COST-ATTRIBUTION GAP (flag):** Both scheduler model calls (`RL:1172` dependency analysis,
`RL:1515` learnings summary) go through `run_provider_oneshot` (`OC:242`), which hardcodes
`claude-sonnet-4-6` (`OC:275`) and **BYPASSES `run_stage`/`record_stage_invocation`** (the
cost ledger; `record_stage_invocation` defined `OC:2653`, only called from the `run_stage`
streaming paths at `OC:2454, 2639`). Therefore the spend on EVERY dependency-analysis call and
EVERY per-failure learnings-summary call is **never written to `stage-costs.jsonl`** and never
appears in the cost summary. For a large batch with many tasks and many retries this is a
systematic, unbounded under-count of real Sonnet spend attributed to the orchestration system.

**PROMPT CAPTURE — learnings-summary prompt verbatim** (`RL:1515–1520`):
```
Analyze this failed task attempt and give a 2-3 sentence summary of what went wrong and what to try differently next time. Be specific and actionable.

Task: $task_id (attempt $attempt)

Error context:
$error_context
```
(`$error_context` = `tail -50 "$task_log"`, `RL:1503`.) The dependency-analysis prompt is the
large heredoc at `RL:1108–1165` (3 sections: Dependency Analysis, Deploy Dependency Detection,
Execution Mode Classification with the micro/lite/full criteria).

---

## 8. Coupling — generic scheduler vs Hey Soo!-specific

| Item | Generic? | Hey Soo!-specific bits / generic shape |
|---|---|---|
| DAG scheduler, MAX_CONCURRENT dispatch, retry-with-learnings, circuit breaker, cascade, queue ingestion (`RL:1768–2024`) | **Generic** | Pure orchestration. Port as-is. Generic shape: a `Scheduler` over a `Task{id, deps, state, attempt, mode, provider}` graph with pluggable `launch(task)`, `is_done(task)→{running,ok,fail}`. |
| Child command `implement-roadmap-task-orchestrator.sh` (`RL:1408`) | **Specific** | The unit of work. Generic shape: injected `executor` callable / command template; Ralph should not know the child's flags. |
| Task-ID normalization `#NNN` ↔ GitHub issue (`RL:327–346`) | **Specific** (GitHub issues / roadmap dotted IDs) | Generic: opaque task-id + optional `:mode`/`:provider` tag parser. |
| Dependency-analysis prompt (`RL:1108–1165`) | **Specific** | Mentions `docs/decisions/` ADRs, CloudFront/API Gateway/Lambda E2E deploy semantics, roadmap. Generic: prompt + JSON schema are config, not hardcoded. |
| Deploy-dependency "kick" gate (`RL:1240–1310`) | **Specific** | Encodes "E2E runs against deployed infra" assumption. Generic: an optional dep-edge predicate that removes tasks+transitive dependents. |
| Capacity throttle via `~/.claude/fetch-usage.sh` + `/tmp/.claude_usage_cache` 5-h util (`RL:60–84`) | **Specific** (Claude usage API) | Generic: a `capacity_available()→bool` probe, provider-agnostic; codex already short-circuits it. |
| Baseline test capture (`test-unit.sh`, `e2e-smoke.sh`, `.spec.ts`) (`RL:704–794`) | **Specific** | Project test harness + Playwright assumption. Generic: optional pre-batch baseline hook. |
| Lock at `/tmp/heysoo-ralph.lock` (`RL:34`) | mostly generic | Name is project-specific; mechanism (PID liveness + status-file PID check) is generic. |
| `status-ralph.json` / `ralph-queue.json` schema | **Generic** | File-based state is the durable contract; port faithfully. |

## 9. Anomalies — bugs, dead code, contradictions with docs/orchestration-template.md

1. **Cost-attribution gap (confirmed, matches the doc's thesis).** `run_provider_oneshot`
   bypasses the ledger (see ROUTING TABLE). `docs/orchestration-template.md:219` warns the
   "headless lane the cost ledger never attributed there" — this is the same class of bug for
   the scheduler's own analysis/learnings calls. Should be called out as a port requirement:
   route one-shot calls through cost recording.

2. **Stale pricing / model pins (contradiction with reality, noted in the doc).**
   `docs/orchestration-template.md:83–84` says Opus is priced $15/$75 in the ledger but real
   Opus is $5/$25 (~3× overstate), and stages pin specific model snapshots. Independently,
   `MODEL_CHAIN` pins `claude-opus-4-7` (`OC:49`) while one-shots pin `claude-sonnet-4-6`
   (`OC:275`) — model strings are hardcoded in three places and will rot.

3. **Cascade only checks DIRECT dependents, not transitive.** `cascade_block` (`RL:1600–1619`)
   only blocks tasks whose `depends_on` directly contains the failed id; a grandchild
   (depends on a child of the failed task) is NOT cascade-blocked at failure time. It will
   instead survive as `blocked` and only be caught by the terminal "only blocked remain" sweep
   (`RL:2014`). The deploy-kick logic (`RL:1261–1291`) DOES walk transitively — inconsistent.
   Comment at `RL:1596` claims "directly or transitively" but the code does not. **Likely bug.**

4. **Circuit breaker can mask legitimate progress / hide variety.** It trips only on 3
   IDENTICAL signatures of the LAST 3 (`RL:1564, 1576`). The signature is `stage + 100 chars of
   the 3rd-from-last log line` (`RL:1544–1545`) — brittle: timestamps/paths in that line make
   "identical" failures look distinct, so the breaker often never trips and tasks burn all
   MAX_RETRIES instead. Conversely fewer than 3 attempts can never trip it.

5. **`run_dependency_analysis` skips analysis for `<=2` tasks** (`RL:1079`) routing them to the
   independent-mode fallback — so a genuine 2-task dependency is silently dropped, risking the
   merge-conflict the prompt explicitly warns about (`RL:1127`). Intentional cost trade-off but
   a correctness hole.

6. **Resume resets `permanently_failed` → retry with attempt 0 unconditionally** (`RL:1711–1716`)
   assuming "user fixed something." If nothing was fixed, resume re-burns the full retry budget.
   Documented intent (`RL:1712`) but a foot-gun for automated/cron resume (the doc's §"unattended
   queue mode" goal, `template.md:231`).

7. **Dead/duplicated reads.** `running_count` computed at `RL:1823` then immediately recomputed
   at `RL:1842` and again per-iteration at `RL:1877`; `current_running` (`RL:1841`) duplicates it.
   Harmless but noisy. `completed_ids` in `reevaluate_blocked_tasks` (`RL:988`) is assigned and
   never used (marked `# shellcheck disable=SC2034`).

8. **Throttle bypass while running.** Capacity throttle only fires when `current_running > 0`
   (`RL:1845`); with 0 running it always allows a launch even at >80% util (`RL:1858–1859`
   logs "bypassed — 0 running tasks, safe to launch"). Intentional (avoid deadlock) but means
   the first launch of each idle period ignores capacity.

---

### DISPUTED / unverifiable
- **Source commit SHA:** repo had no commits at read time (working-tree state); SHA unknown.
- **`record_stage_invocation` non-invocation by one-shots:** verified by grep — it is called
  only at `OC:2454` and `OC:2639` (inside `run_claude_streaming`/`run_codex_streaming`), and
  `run_provider_oneshot` (`OC:242–329`) contains no call to it. Confirmed, not disputed.
- **Whether the dependency-analysis schema is enforced on the codex path:** `OC:221–223`
  states the schema is "included in context but not strictly enforced" for codex — so codex
  output parsing is best-effort (`RL:1183–1200` tries 3 parse shapes). Behavior depends on the
  external `codex` CLI; unverifiable from source alone.
