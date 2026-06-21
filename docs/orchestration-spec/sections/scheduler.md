# AS-BUILT SECTION: SCHEDULER SUBSYSTEM

Sources:
- `RL` = `/Users/craigperler/Development/heysoo/.claude/scripts/ralph-loop.sh` (lines 1–2299, working tree 2026-06-20)
- `OC` = `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`
- `BO` = `/Users/craigperler/Development/heysoo/.claude/scripts/batch-orchestrator.sh` (lines 1–842, working tree 2026-06-20)

Synthesized from fragments `01-ralph-loop.md` and `05-batch-orchestrator.md`.

---

## Part 1 — ralph-loop.sh as-built

### 1.1 Role and entry points

Ralph Wiggum smart scheduler — dependency-aware concurrent task orchestration with
retry-with-learnings, dynamic unlocking, capacity throttle, and a persistent work queue
(`RL:3–6`). It sits ABOVE the per-task orchestrator, fanning out N concurrent invocations
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

### 1.2 Flags and constants

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

| Constant | Value | Line |
|----------|-------|------|
| `MAX_RETRIES` | 3 | `RL:37` |
| `MAX_CONCURRENT` | 3 | `RL:38` |
| `COOLDOWN` | 120 s | `RL:39` |
| `POLL_INTERVAL` | 30 s | `RL:40` |
| `CIRCUIT_BREAKER_IDENTICAL` | 3 | `RL:41` |
| `IDLE_TIMEOUT` | 300 s | `RL:42` |
| `QUEUE_IDLE_POLL` | 15 s | `RL:43` |
| Launch-gate capacity threshold | `>= 80%` 5-h util | `RL:79` |
| Per-task OC throttle (contrast) | `>= 90%` | noted at `RL:77` |
| `BASELINE_UNIT_TIMEOUT` | 1200 s | `RL:727` |
| `BASELINE_E2E_TIMEOUT` | 1800 s | `RL:740` |
| Learnings-summary timeout | 180 s | `RL:1520` |
| Dependency-analysis skip threshold | `task_count <= 2` | `RL:1079` |
| Cleanup grace before SIGKILL | `sleep 1` | `RL:2130` |
| Lock file | `/tmp/heysoo-ralph.lock` | `RL:34` |

### 1.3 DAG / dependency analysis

`run_dependency_analysis` (`RL:1070–1214`) sends a large structured prompt (`RL:1108–1165`,
three sections: Dependency Analysis, Deploy Dependency Detection, Execution Mode
Classification with micro/lite/full criteria) through `run_provider_oneshot`, writing
`dependency-analysis.json` (`RL:1211`). Skips analysis for `task_count <= 2` (`RL:1079`),
routing directly to independent-mode fallback — meaning a genuine 2-task dependency is
silently dropped (correctness hole; `RL:1127` warns about the very merge-conflict risk
this creates).

Per-task initial state: `ready` if `depends_on` empty, else `blocked` (`RL:897`).

`reevaluate_blocked_tasks` (`RL:985–1011`): for each `blocked` task, if ALL deps are now
`completed` → mark `ready` and zero `unmet_deps`; else update `unmet_deps`. This is what
**unlocks a cascade-blocked task** — specifically: a task blocked because its dependency
failed is promoted to `cascade_blocked` (not merely `blocked`) and is NOT unlocked by this
function (see cascade-blocking below).

**What unlocks a cascade-blocked task:** nothing, automatically. `cascade_blocked` is a
terminal-equivalence state at runtime. It can be recovered only by `--resume`, which resets
`cascade_blocked` → `blocked` for re-evaluation (`RL:1736`), then calls
`reevaluate_blocked_tasks`. A simple `blocked` task (dependency not yet complete but not
permanently failed) is unlocked when `reevaluate_blocked_tasks` finds all its `depends_on`
entries in `completed` state — i.e., when its direct dependencies finish successfully.

### 1.4 MAX_CONCURRENT dispatch

`run_loop` (`RL:1768–2024`) — `while true` body:

1. **Queue poll** (queue mode): `dequeue_batch` → `ingest_batch`; on ingest failure `requeue_batch_entry` (re-prepend) (`RL:1777–1790`).
2. **Exit/idle check**: if `!tasks_remaining` → in queue mode idle-wait, exit when `elapsed >= IDLE_TIMEOUT` (`RL:1793–1812`), polling every `QUEUE_IDLE_POLL=15`s.
3. **Retry-ready promotion**: retrying tasks whose `retry_after` elapsed → `ready` (`RL:1829–1835`).
4. **Capacity check + LAUNCH** (`RL:1840–1883`): see capacity throttle (§1.7).
5. `sleep POLL_INTERVAL` (=30 s) (`RL:1886`).
6. **Poll running tasks** (`RL:1888–1952`): per task switch on completion result — 0=mark completed + `reevaluate_blocked_tasks` (unlock dependents); 2=fail path (extract_learnings → circuit-breaker → retry-or-permanent-fail).
7. **Exit conditions** (`RL:1956–2022`): `active==0 && blocked==0` → done; `active==0 && blocked>0` → (queue mode: drain queue / idle-wait, else) cascade-block all remaining blocked tasks and break.

**MAX_CONCURRENT dispatch loop** (`RL:1869–1883`): iterate `get_ready_tasks`; per task,
re-read `running_count` and launch only while `running_count < MAX_CONCURRENT`, else `break`.
Codex-provider tasks bypass the capacity gate (`RL:1874`).

Child orchestrator spawn (`RL:1408–1413`):
```
"$SCRIPT_DIR/implement-roadmap-task-orchestrator.sh" \
    --task "$task_id" --branch "$BRANCH" --status-file "$task_status_file" \
    ${extra_args[@]+"${extra_args[@]}"}   >> "$task_log" 2>&1 &
```
`extra_args` may include: `--agent`, `--micro`/`--lite`, `--provider <p>` (when task
provider ≠ claude, `RL:1387–1389`), `--learnings-file <file>` (`RL:1393`), `--baseline-file
<file>` (`RL:1398`), `--resume` (`RL:1406`).

### 1.5 Retry-with-learnings (APPEND, not prepend)

On each task failure (`RL:1923–1950`):

1. `extract_learnings` (`RL:1461–1558`) **APPENDS** a `## Attempt N` block to
   `$LOG_BASE/learnings-issue-NNN.md` (`RL:1465, 1473–1498`): state, failed stage, completed
   stages, last-10-lines of task log, then a best-effort AI `Analysis:` paragraph from
   `run_provider_oneshot` (`RL:1515–1534`). It also computes an `error_sig` =
   `"<stage>:<first 100 chars of 3rd-from-last log line>"` and pushes it onto
   `.error_signatures[]` (`RL:1538–1555`).
   **NOTE:** learnings are APPENDED (newest at bottom), NOT prepended. The file is later
   passed to the child via `--learnings-file` (`RL:1392–1394`).
2. **Circuit breaker** (§1.6 below).
3. Else if `attempt >= MAX_RETRIES` → `mark_permanently_failed` (`RL:1945–1946`).
4. Else `schedule_retry` (`RL:1622–1637`): sets `retry_after = now + COOLDOWN`, state →
   `retrying`, pid → null. `is_retry_ready` (`RL:1640–1656`) promotes back to `ready` once
   cooldown elapses.

### 1.6 Circuit breaker — identical error signatures

`check_circuit_breaker` (`RL:1561–1581`): inspects the LAST 3 (`.[-3:]`) error signatures
in `.error_signatures[]`. **Trips when `total >= CIRCUIT_BREAKER_IDENTICAL (3)` AND all 3
are identical (`unique_count == 1`).** Trip → `mark_permanently_failed` (`RL:1942–1944`).

Error signature format: `"<stage>:<first 100 chars of 3rd-from-last log line>"`
(`RL:1544–1545`). This is brittle: timestamps or absolute paths in that line make genuinely
identical failures look distinct, so the breaker frequently never trips and tasks burn the
full `MAX_RETRIES` budget instead. Fewer than 3 attempts can never trip it.

### 1.7 Cascade-blocking

`cascade_block` (`RL:1593–1620`): on permanent failure, every task in state
`pending|blocked|ready` whose `depends_on` **directly** contains the failed id →
`cascade_blocked`.

**VERIFIED BUG:** The comment at `RL:1596` claims "directly or transitively" but the code
only checks direct dependents (`RL:1600–1619`). A grandchild (depends on a child of the
failed task) is NOT cascade-blocked at failure time. It survives as `blocked` and is only
caught by the terminal "only blocked remain" sweep (`RL:2014`). The deploy-kick logic
(`RL:1261–1291`) DOES walk transitively — inconsistent.
Canonical verifier-confirmed citation: `ralph-loop.sh:1596/1609`.

Terminal-state cascade: when only blocked tasks remain with nothing active (and not in queue
mode), ALL remaining blocked tasks are cascade-blocked and the loop breaks (`RL:2014–2021`).

### 1.8 Queue ingestion

`ralph-queue.json` (`RL:33`) — JSON array of `{tasks, branch, enqueued_at}` (`RL:451–455`).
`--enqueue` atomically appends a batch entry and exits; no lock is needed because it is
append-only (`RL:2181–2188`). `dequeue_batch` pops the head; on ingest failure
`requeue_batch_entry` re-prepends it (`RL:1777–1790`). Idle wait: polls every 15 s for up
to `IDLE_TIMEOUT` (300 s) before exiting.

### 1.9 Capacity throttle — launch gate at >= 80%

`check_ralph_capacity` (`RL:60–84`): before launching, refresh `/tmp/.claude_usage_cache`
via `~/.claude/fetch-usage.sh`, read 5-h utilization (line 1). If `>= 80%` AND
`current_running > 0` → set `RALPH_CAPACITY_THROTTLED=true`, status → `throttled`,
`capacity_ok=false`, hold non-codex launches.

The gate is bypassed when `current_running == 0` (intentional deadlock-avoidance: the first
launch of each idle period always proceeds even at >80% util, `RL:1858–1859`). Codex-provider
tasks bypass entirely (`RL:1844, 1874`). The aggregate throttle (80%) is more aggressive
than the per-task OC throttle (90%, `RL:77`).

### 1.10 Cost-attribution gap (confirmed bug)

Both scheduler model calls — dependency analysis (`RL:1172`) and learnings summary
(`RL:1515`) — go through `run_provider_oneshot` (`OC:242`), which hardcodes
`claude-sonnet-4-6` (`OC:275`) and **bypasses `run_stage`/`record_stage_invocation`** (the
cost ledger; `record_stage_invocation` is defined at `OC:2653` and called only from
`run_claude_streaming`/`run_codex_streaming` at `OC:2454, 2639`).

Therefore the spend on every dependency-analysis call and every per-failure learnings-summary
call is **never written to `stage-costs.jsonl`** and never appears in the cost summary. For a
large batch with many tasks and many retries this is a systematic, unbounded under-count of
real Sonnet spend. Port requirement: route one-shot calls through cost recording.

Canonical verifier-confirmed citations: `run_provider_oneshot` @ `OC:242` / `RL:1172` /
`RL:1515`.

### 1.11 Crash recovery

`load_resume_state` (`RL:1662–1740`):
- Stale `running` → `ready` + `extract_learnings`
- `permanently_failed` → reset to `ready`, attempt 0, clear signatures (assumes "user fixed something"; foot-gun for automated/cron resume)
- `cascade_blocked` → `blocked` for re-evaluation
- Then calls `reevaluate_blocked_tasks`

---

## Part 2 — batch-orchestrator.sh as-built

### 2.1 Role

Legacy serial batch driver for GitHub issues. Each issue runs two stages: an implement stage
(delegated to `implement-issue-orchestrator.sh` → `implement-orchestrator.sh`, `BO:546–606`)
and a `process-pr` stage (a direct `claude -p /process-pr` call, `BO:670–674`).

No parallelism. No dependency tracking. No DAG. No retry-with-learnings.

### 2.2 Entry points and flags

- `./batch-orchestrator.sh --manifest <path>` (`BO:78–82`)
- `./batch-orchestrator.sh --issues "123,124,125" --branch "test"` (`BO:83–92`)
- `./batch-orchestrator.sh --manifest <path> --agent <name>` (`BO:93–97`)

Flags: `--manifest`, `--issues` (CSV), `--branch`, `--agent` (implement stage only;
process-pr is hardcoded to `--agent code-reviewer`, `BO:673`). No env vars read at this
layer (`ORCHESTRATOR_PROVIDER`, capacity envs: all absent).

### 2.3 Constants

| Constant | Value | Line |
|----------|-------|------|
| `ISSUE_TIMEOUT` | 10800 s (180 min) | `BO:42` |
| `MAX_CONSECUTIVE_FAILURES` | 3 | `BO:43` |
| `RATE_LIMIT_BUFFER` | 60 s | `BO:44` |
| `RATE_LIMIT_DEFAULT_WAIT` | 3600 s (1 h) | `BO:45` |

No concurrency constant (effectively 1). No model pins — model selection is entirely
delegated downstream.

### 2.4 Control flow

Top-level: parse args → load schema → `acquire_lock` → set traps → mkdir logs →
`init_status` → **serial main loop** (`BO:787–811`) → final state → summary → exit.

Main loop: one iteration per issue, strictly serial. No `&` fan-out, no `MAX_CONCURRENT`.

1. Idempotency check: if issue `.status == completed`, skip (`BO:789–794`).
2. `process_issue "$issue"` (`BO:796`). On success: `consecutive_failures=0`.
3. On failure: `consecutive_failures++`, `exit_code=1`.
4. **Circuit breaker**: `consecutive_failures >= 3` → `state=circuit_breaker`, exit 2 (`BO:804–808`).

Per-issue: implement stage (delegate, `wait`, parse result) → process-pr stage (run
`run_claude_streaming`; one rate-limit retry via `--resume $session_id` after a computed
sleep; timeout 124 → failed). No second rate-limit wait; no retry loop.

### 2.5 Circuit breaker — consecutive failures

Global consecutive-failure counter, NOT per-task identical-error. Any 3 consecutive issue
failures abort the batch. Reset to 0 on any success. Different mechanism from ralph's
`CIRCUIT_BREAKER_IDENTICAL`.

### 2.6 Cascade and rate-limit handling

**Cascade:** NONE. No dependency edges → nothing to cascade.

**Rate-limit:** reactive detect + single sleep/resume (`BO:685–716`). If the resumed call
also hits a rate limit, it falls into `error|rate_limit|*` → `failed`. No parking state.
Strictly worse than ralph's proactive capacity throttle.

---

## Part 3 — D5 VERDICT: batch-orchestrator.sh is SUBSUMED

**VERDICT: batch-orchestrator.sh is SUBSUMED by ralph-loop.sh. Disposition: spec-only-confirm-then-drop. Do NOT port.**

Ralph is a strict superset on every orchestration axis:

| Capability | batch-orchestrator.sh | ralph-loop.sh | Subsumed? |
|---|---|---|---|
| Process a fixed list of issues | `--issues` CSV / `--manifest` (`BO:114,128`) | `--tasks` CSV + runtime queue (`RL:171, 488–507`) | YES |
| Parallel execution | **NONE — strictly serial** (`BO:787`) | YES — `MAX_CONCURRENT=3`, backgrounded launches (`RL:38,1413,1878–1882`) | YES |
| Dependency tracking | **NONE** | DAG `depends_on`, blocked→unlock, cascade-block (`RL:985–1011,1593–1620`) | YES |
| Retry-with-learnings | **NONE** | Yes (`RL:1461–1558`, attempt/error-signatures) | YES (ralph adds it) |
| Circuit breaker | Global consecutive-3 (`BO:804–808`) | Per-task identical-error-3 (`RL:41,1561–1581`) | FUNCTIONALLY YES (ralph mechanism better) |
| Rate-limit handling | Reactive detect + single sleep (`BO:685–716`) | Proactive capacity throttle + non-failing `throttled`/`paused` states (`RL:58–84,1838–1867`) | YES (ralph strictly better) |
| Resume | Skip `completed` on re-run (`BO:789–794`) | Full `--resume` of `status-ralph.json` (`RL:1662–1740`) | YES (ralph richer) |
| Provider routing (codex) | **NONE** | Per-task `:codex`/`:provider` tags (`RL:348–376,1384–1389`) | YES (ralph adds it) |
| Lock + signal cleanup | PID lock + traps (`BO:143–239`) | PID+statusfile lock + traps (`RL:800–871,2154–2171`) | YES |

**The only two batch behaviors not literally in ralph:**

1. **Per-stage agent role split**: batch lets `--agent` pick the implement agent while
   pinning `code-reviewer` for process-pr (`BO:565–568, 673`). Ralph passes one global
   `--agent` through (`RL:247–251,1373–1375`). Not unique value — in the target spec the
   PR-finalize agent is a project-config adapter value; this collapses into config.

2. **Flat per-issue PR ledger shape in `status.json`**: batch records
   `number/status/stage/pr/follow_ups` per issue (`BO:268–278`). Ralph's per-task status
   file already produces `follow_up_issues` (from `process-pr.json`); shape difference only,
   no capability is lost.

**Doc inaccuracy — `docs/orchestration-template.md:397`:**
The §8 inventory table labels batch-orchestrator.sh "Batch-mode parallel execution **with
dependency tracking**." The source has NEITHER (serial `for` loop `BO:787`; zero
`MAX_CONCURRENT`; zero `depends_on` machinery across 842 lines). Those properties belong to
ralph-loop.sh. The design-doc line is inaccurate and must be corrected; do not propagate
the error into the rebuild spec.

**Phantom flag — `--agent unique-features` mandate:**
The mapping-task brief referenced an "`--agent unique-features` mandate." No such flag or
concept exists in batch-orchestrator.sh. `--agent` is a single optional implement-stage
agent selector (`BO:93–97, 565–568`); the only fixed agent is the hardcoded `code-reviewer`
for process-pr (`BO:673`). Do NOT encode a phantom "unique-features" mandate in the target spec.

---

## Summary of verifier-confirmed facts

1. **Cascade blocks only DIRECT dependents** (`RL:1600–1619`), despite the comment at
   `RL:1596` claiming "directly or transitively." Grandchild tasks (depends on a direct
   dependent of the failed task) are NOT cascade-blocked at failure time; they survive as
   `blocked` until the terminal sweep at `RL:2014`. Bug. Deploy-kick logic walks
   transitively (`RL:1261–1291`) — inconsistent.

2. **Cost-attribution gap**: `run_provider_oneshot` at `OC:242` (invoked for dependency
   analysis `RL:1172` and learnings summary `RL:1515`) bypasses `record_stage_invocation`
   (only called at `OC:2454` / `OC:2639`). Scheduler's own Sonnet spend is never written to
   `stage-costs.jsonl`. Systematic, unbounded under-count for large batches with many
   retries.

3. **Circuit breaker trips on 3 identical error signatures in the last 3 attempts**
   (`RL:1564, 1576`), using signature `"<stage>:<first 100 chars of 3rd-from-last log line>"`
   (`RL:1544–1545`). Brittle: variable content in that line (timestamps, paths) makes
   identical failures look distinct.

4. **Launch gate threshold is >= 80% 5-h utilization** (`RL:79`), bypassed when
   `current_running == 0` (deadlock-avoidance, `RL:1858–1859`) and bypassed entirely for
   codex-provider tasks (`RL:1874`).

5. **Dependency analysis is skipped for `task_count <= 2`** (`RL:1079`), routing silently
   to independent mode — a correctness hole for genuine 2-task dependencies.
