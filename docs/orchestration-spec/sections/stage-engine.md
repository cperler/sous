# As-Built: Stage Engine + Per-Task Pipeline

Synthesized from fragments 02a (engine dispatch/routing/cost/capacity), 02b (state/loops/lifecycle), and 03 (per-task pipeline). All absolute paths below refer to the read-only reference system unless noted:
- Pipeline entry: `/Users/craigperler/Development/heysoo/.claude/scripts/implement-orchestrator.sh`
- Shared engine: `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`

Citations of the form `:NNNN` without a filename refer to `orchestrator-common.sh`; pipeline citations are prefixed `impl-orch:NNNN`.

The system splits into three layers:
1. **Pipeline sequencer** (`implement-orchestrator.sh`) — fixed linear stage order, owns prompt text, owns the per-task review loop and PR-review loop.
2. **Execution core** (2a, `orchestrator-common.sh`) — `run_stage` dispatch, provider/codex routing, model tiering, capacity throttle, cost ledger, traps/cleanup.
3. **State + loops** (2b, `orchestrator-common.sh`) — status lifecycle, convergence/plateau detection, quality loop, test loop, circuit breakers, resume.

---

## 1. Per-Task Pipeline Stage Sequence (from fragment 03)

`implement-orchestrator.sh` is the per-task pipeline entry point: `main "$@"` at `impl-orch:2217`, `main()` defined `impl-orch:358`. It is a **fixed linear sequence of guarded blocks** (NOT a dispatch table). Each block does: `skip_stage`/resume-guard → `set_stage_started` → work → `set_stage_completed`. It never calls `claude`/`codex` directly — it calls `run_stage` / `run_stage_with_timeout` in the engine.

### Stage order as executed in `main()`

| # | Stage | Lines (impl-orch) | full | lite | micro |
|---|-------|-------------------|------|------|-------|
| 0 | **extract** | `:472`–`:514` | yes | yes | yes |
| — | closed-issue early-exit (github-issue only; exit 0 `already_closed`) | `:516`–`:530` | yes | yes | yes |
| 1 | **setup** (pure shell, no LLM — comment `:533` "see issue #227") | `:532`–`:641` | worktree | worktree | branch-only via `run_setup_stage_micro` `:548` |
| — | frontend build (inside setup) | `:589`–`:608` | yes | yes | skip micro `:593` |
| — | baseline capture (inside setup) | `:610`–`:640` | yes | yes | skip micro `:614` |
| 2 | **research** | `:643`–`:674` | yes | skip | skip |
| 3 | **evaluate** (can exit 1 `blocked` `:747`) | `:676`–`:754` | yes | skip | skip |
| 4 | **plan** (skip ⇒ synthetic 1-task list `:778`) | `:756`–`:878` | yes | synthetic | synthetic |
| 5 | **implement** (per-task loop, quality_loop nested inside) | `:880`–`:1377` | review+quality | single-pass | single-pass |
| — | post-impl frontend build validation | `:1379`–`:1459` | yes | yes | skip micro `:1383` |
| 6 | **test_loop** | `:1461`–`:1477` | yes (cap 10) | yes (cap 2, or 3 w/E2E) | skip |
| — | **verify** (`tests/verify/*.sh`, non-fatal) | `:1479`–`:1497` | yes | yes | yes |
| 7 | **docs** | `:1499`–`:1527` | yes | skip | skip |
| — | no-commits early-exit (exit 0 `no_changes`) | `:1529`–`:1540` | yes | yes | yes |
| 8 | **pr** | `:1542`–`:1631` | yes | yes | yes |
| 9 | **pr_review** | `:1633`–`:2007` | spec+code loop (cap 3) | code-only single-pass | code-only single-pass |
| 10 | **complete** (completion gate) | `:2009`–`:2192` | yes | yes | yes |

### Skip lists (mode machinery)

`skip_stage <name>` (`:590`) returns true if `<name>` is in the active mode's list:
- `LITE_SKIP_STAGES=(research evaluate plan quality_loop docs)` (`:582`)
- `MICRO_SKIP_STAGES=(research evaluate plan quality_loop test_loop docs)` (`:585`)

### "11 stages" is really ~12–13 (CANONICAL, DISPUTED vs template)

The template claims a tidy 11-stage list (setup→research→evaluate→plan→implement→quality_loop→test_loop→docs→pr→pr_review→complete). The actual `main()` differs:
- **extract** is a real first stage (`impl-orch:472`) the template omits.
- **verify** is a real, unlisted stage (`impl-orch:1479`).
- **quality_loop** is NOT a standalone top-level block — it runs *inside* implement per-task via `run_quality_loop` (called `impl-orch:1137`, `:1200`, `:1282`) and is only flag-marked completed at `impl-orch:1371`, and only in full mode. `skip_stage "quality_loop"` is in both LITE and MICRO skip lists so quality never runs in lite/micro.

So the executed sequence is closer to **extract → setup → research → evaluate → plan → implement(+quality) → test_loop → verify → docs → pr → pr_review → complete** (~12–13 guarded blocks).

### Implement per-task loop (`impl-orch:911`–`:1345`)
For each task in `tasks_json`:
1. **Dependency gate** (`:922`–`:946`): if any `depends_on` id is in `failed_task_ids`, mark `skipped_due_to_dependency`, add self to `failed_task_ids`, `continue`.
2. **Resume recovery** (`:948`–`:991`): completed→skip; `timed_out`→git-grep for committed work, promote to completed if found.
3. **lite/micro branch** (`:997`–`:1047`): single `run_stage` implement, no review, no quality.
4. **full branch** (`:1048`–`:1338`): review-while-loop, cap `MAX_TASK_REVIEW_ATTEMPTS=3` (`:1062`). Flow: implement → git-aware timeout recovery (`:1106`) → no-op auto-approve (`:1180`) → review (`spec-reviewer`) → if `passed`, run quality loop; else fix (`:1303`) + force-push-remediation detection (`:1309`, decrements attempt).
5. Exhausted attempts → mark `failed`, record in `failed_task_ids` (`:1316`–`:1336`). Does NOT halt — independent subtasks still attempted.
6. **Completion gate** (`impl-orch:1347`–`:1367`, again `:2009`–`:2036`): any task not `completed` → exit 1 `incomplete`.

### docs stage uses `phpdoc-writer` to write PYTHON docstrings (CANONICAL — likely PHP-carryover BUG)
The `docs` stage call (`impl-orch:1517`) passes `--agent phpdoc-writer` for a prompt that says "Write docstrings for all modified Python files" (`impl-orch:1509`–`:1513`), scoped to `^(lambda|infra)/`. A PHP-named agent writing Python docstrings — almost certainly a copy/paste carryover from a PHP project.

### Exit codes (pipeline)
| Code | Meaning |
|------|---------|
| 0 | success / already-closed / no-changes / PR-skipped (`impl-orch:528`,`:1539`,`:1612`,`:2213`) |
| 1 | generic error: setup, plan, blocked eval, PR, incomplete completion gate (`:558`,`:747`,`:842`,`:1366`,`:1619`,`:2035`) |
| 2 | fatal halt: impl timeout/empty-output (no committed work), build fail after 3 fixes, docs fail, max PR-review iters (`:1165`,`:1444`,`:1523`,`:1787`) |
| 3 | argv/usage error |
| 4 | extract stage failed (`:502`) |

---

## 2. `run_stage` Execution Core + Routing + Model Tiering + Capacity + Cost (fragment 02a)

`orchestrator-common.sh` is a **sourced library, never executed** (`:1`–`:5`, guarded by `_ORCHESTRATOR_COMMON_LOADED`; sources `lib/status-file-helpers.sh` at `:8`).

### Entry points
- **`run_stage <stage_name> <prompt> <schema_file> [agent]`** (`:2848`) — THE central dispatch. Called once per pipeline stage and by the in-file quality/test/e2e loops. Always invoked inside `$(...)` command substitution — so the stage counter is **file-based** because run_stage runs in a subshell and in-memory state can't persist (`:2855`).
- **`run_stage_with_timeout <secs> <run_stage args…>`** (`:89`) — thin wrapper overriding `STAGE_TIMEOUT*`, then restoring.
- **`run_provider_oneshot <prompt> <schema_path> <out_var> [timeout]`** (`:242`) — SEPARATE one-shot dispatcher for analytical JSON calls (used by Ralph). **Bypasses run_stage entirely** (see cost gap below).
- **`run_setup_stage` / `run_setup_stage_micro`** (`:497`, `:525`) — pure-shell setup.
- **`register_orchestrator_traps`** (`:1639`); `orchestrator_cleanup` (`:1512`) is the trap body.

### Two-axis routing

**Axis A — provider selection** (`should_use_codex :191`):
- `ORCHESTRATOR_PROVIDER=codex` ⇒ **every** stage → codex (`:194`–`:201`).
- else `TASK_PROVIDER=codex` (the `:codex` tag) ⇒ codex **only if `is_codex_eligible_stage`** (`:206`–`:207`). Eligible = `CODEX_ELIGIBLE_STAGES` globs (`:145`–`:154`): `implement-task-*, fix-task-*, fix-pr-review-*, fix-review-*, fix-unit-*, fix-e2e-*, fix-test-quality-*, fix-test-*`.
- else claude.

`TASK_PROVIDER` is **exported** by the pipeline (`impl-orch:183`) so the engine sees it.

**Axis B — model tiering** (`get_stage_model :55`):
| Stage glob | Model | Cite |
|---|---|---|
| `setup` | `claude-haiku-4-5-20251001` | `:59`–`:60` |
| `research`,`plan`,`implement-task-*`,`fix-*` | `claude-opus-4-7` | `:62`–`:63` |
| `evaluate`,`task-review-*`,`spec-review-*`,`code-review-*`,`simplify-*`,`review-*`,`quality-*`,`test-*`,`docs`,`pr`,`complete` | `claude-sonnet-4-6` | `:65`–`:66` |
| `*` (default) | `claude-opus-4-7` | `:68`–`:69` |

Fallback chain on rate-limit/capacity: **Opus → Sonnet → Haiku** (`MODEL_CHAIN :49`), entered at `model_index+1`.

**Execution surface:**
| Provider | Runner | Cite |
|---|---|---|
| claude (pipeline) | `run_claude_streaming` (stream-json, tiered model, `--json-schema`, `--agent`) | `:2399` |
| codex (eligible) | `run_codex_stage` (`codex exec --json --output-last-message`) | `:2469` |
| claude one-shot (Ralph) | `run_provider_oneshot` — **hardcodes `claude-sonnet-4-6`** (`:275`), `--output-format json`, NO stream file, **NO `record_stage_invocation`** | `:242` |
| codex one-shot (Ralph) | `run_provider_oneshot` codex branch | `:284` |

### `run_stage` state machine (`:2848`–`:3287`)
1. Counter+log setup (`:2855`–`:2860`): increment file counter, derive `NN-stage.log`.
2. Schema validation (`:2863`–`:2870`): missing ⇒ error JSON, return 1.
3. Model selection (`:2874`–`:2882`): `current_model = get_stage_model(stage)`; find `model_index` in MODEL_CHAIN.
4. **Proactive capacity check** (`:2891`–`:2911`): if not codex AND `check_capacity` (≥90%): set status paused, try `try_capacity_model_fallback`; on fail `capacity_wait_loop 3600 "pre-stage"`; restore running.
5. Learnings prepend (`:2919`–`:2928`): `$LEARNINGS_FILE` (if non-empty) prepended to the prompt.
6. Provider decision (`:2939`–`:2948`): `should_use_codex` ⇒ codex; `render_stage_prompt` rewrites prompt; append stage-index row; update status.
7. Execution (`:2965`–`:2988`): codex success requires `_rc_exit_code==0 && structured_output.status=="success"` (`:2973`), else **fall through to Claude** (`:2980`–`:2981`). Otherwise `run_claude_streaming`.
8. Markdown extraction (`:2993`).
9. **Timeout branch (exit 124)** (`:2997`–`:3199`), split on `output_bytes < 100`:
   - **Near-empty** (`:3020`): dump stderr, kill stale procs; if `check_capacity` ⇒ capacity path (fallback models → `capacity_wait_loop 1800 "post-timeout"` → resume retry; still-fail ⇒ return 1, `:3081`–`:3089`). Else **logic-hang retry loop** (`:3106`–`:3168`): `while timeout_retry < MAX_STAGE_RETRIES(2)`, sleep `RETRY_COOLDOWN(120)`, cleanup, re-check capacity (capacity waits **decrement the retry counter so they don't burn attempts** `:3151`), then `run_claude_streaming`; success = `exit!=124 && bytes>=100`. Exhausted ⇒ return 1 (`:3172`). Retries use the **same model** (model-switch only helps 429s, not hangs, `:3109`–`:3111`).
   - **Substantial output** (`:3177`–`:3198`): `status=="success"` ⇒ completed; else error, return 1.
10. Empty-output guard (`:3202`–`:3206`): `<100` bytes ⇒ return 1.
11. **Rate-limit branch** (`:3209`–`:3238`): `detect_rate_limit` ⇒ walk `MODEL_CHAIN[model_index+1..]`, `sleep MODEL_FALLBACK_DELAY(10)`, retry each; if all limited, `handle_rate_limit` (long sleep) then one retry on `current_model`.
12. Structured-output extraction (`:3243`–`:3287`): prefer `.structured_output`; else **infer** status from prose `.result` via keyword regexes (`fail|error|changes.requested…` negated by `fixed|resolved|passing…`, `:3272`–`:3278`).

### Codex success heuristic (CANONICAL — `run_codex_stage :2469`–`:2645`)
- Snapshot `pre_head` (`:2499`); run `codex exec`; capture `post_head` (`:2573`). `has_new_work=true` if HEAD moved OR working tree dirty vs pre_head (`:2576`–`:2588`).
- `_schema_valid`: load schema, check **only that top-level `required[]` keys are PRESENT** (`has($k)`) in the last-message — NOT full schema conformance, not types/nesting (`:2595`–`:2615`).
- **`derived_status="success"` iff `exit==0 ∧ no codex_error ∧ (has_new_work ∨ _schema_valid)`** (`:2620`–`:2624`). I.e. patching stages pass on git-moved-or-dirty; analytical stages pass on required-keys-present. A no-op commit or an unrelated dirty file would read as success.

### Capacity throttle math (CANONICAL)

There are **TWO capacity gates** (verifier-confirmed):
1. **Ralph launch throttle** — `ralph-loop.sh:79` blocks launching new tasks at **utilization ≥ 80%** (batch-level admission control; lives in the Ralph scheduler, outside these two files).
2. **Per-task at-capacity** — `check_capacity` in `orchestrator-common.sh:2042` trips at **utilization ≥ 90%** (per-stage, inside `run_stage`'s proactive check and timeout path).

`check_capacity` (`:2024`): refresh `/tmp/.claude_usage_cache` via `bash ~/.claude/fetch-usage.sh` (`:2028`); read line 1 = 5h utilization %, line 3 = reset ts; **at capacity iff util ≥ 90** (`:2042`), sets `CAPACITY_RESET_AT`. Cache missing/empty ⇒ **assume HAS capacity** (return 1, `:2031`,`:2038`).

Sleep computation (`calculate_capacity_sleep :2060`):
```
now_epoch   = date +%s
reset_epoch = parse(CAPACITY_RESET_AT)  fallback now+300        (:2067-2069)
sleep_secs  = reset_epoch - now_epoch + 60                      (:2070)
clamp:  < 60       -> 60
        > max_cap  -> max_cap   (max_cap default 3600)          (:2073-2074)
jitter      = RANDOM % 301      # 0..300s                       (:2077)
sleep_secs += jitter                                            (:2078)
```

`capacity_wait_loop` (`:2095`–`:2129`): sleeps in `poll_interval=900s` chunks; re-checks `check_capacity` each chunk and **exits early when capacity recovers** (`:2114`–`:2118`).

`try_capacity_model_fallback` (`:2142`–`:2170`): instead of sleeping, walk `MODEL_CHAIN[mi+1..]`, sleep 10s, run via `run_claude_streaming`; success = `exit!=124 ∧ bytes≥100 ∧ !detect_rate_limit` (`:2160`); sets `CAPACITY_FALLBACK_MODEL`.

### Rate-limit detection
- `detect_rate_limit` (`:1977`): checks `.structured_output.status` first (`success`⇒not limited, `rate_limit`⇒limited); if `is_error==false` and `.result` present ⇒ not limited (`:1988`); else regex on `.result`: `rate.limit|429|too many requests|quota.exceeded|hit your limit` (`:2013`).
- `extract_wait_time` (`:2172`): parse order `retry.after N` → `wait N min` → `resets Npm/am` (capped 4h) → default `RATE_LIMIT_DEFAULT_WAIT=3600`.
- `handle_rate_limit` (`:2223`): `wait = extract_wait_time + RATE_LIMIT_BUFFER(60)`, log, sleep.

### Cost ledger
`record_stage_invocation` (`:2653`) appends one JSONL row to `$LOG_BASE/context/stage-costs.jsonl` after **every** claude streaming call (`:2454`) and codex call (`:2639`) — including retries/fallbacks (so token totals **double-count by design**). Fields (`:2690`–`:2693`): `label, model, provider, start_epoch, duration_seconds, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, exit_code`. Tokens from `.usage.*` (`:2673`–`:2676`); default 0 (codex rows are typically all-zero). No-op if `LOG_BASE` unset (`:2662`).

`emit_cost_summary` (`:2704`): per-entry cost (`:2730`–`:2735`):
```
cost = (in*price_in + out*price_out + cache_read*price_in*0.1 + cache_write*price_in*1.25) / 1e6
```
Writes `$LOG_BASE/cost-summary.md` (`:2785`).

**STALE Opus pricing (CANONICAL):** `model_price` (`:2726`) prices **Opus at $15 / $75 per MTok**; the real current Opus is **$5 / $25** — the report **overstates Opus ~3×**. Sonnet $3/$15 and Haiku $1/$5; default bucket also uses Opus $15/$75. Pricing is keyed on substring `test("opus")`, so it still applies despite the stale model pin name.

**STALE model pins (CANONICAL):** `MODEL_CHAIN` and `get_stage_model` pin `claude-opus-4-7` (`:49`,`:63`) and `claude-sonnet-4-6` — stale vs CLAUDE.md (`4-6`) and current 4.8. `run_provider_oneshot` independently hardcodes `claude-sonnet-4-6` (`:275`) — a second place a model is pinned.

**COST-ATTRIBUTION GAP (CANONICAL):** `run_provider_oneshot` (`:242`) BYPASSES `run_stage` and never calls `record_stage_invocation` (contrast `run_claude_streaming :2454`, `run_codex_stage :2639`). Every Ralph one-shot (dependency analysis, learnings summarization) is a real billable `claude -p` / `codex exec` call that **never lands in `stage-costs.jsonl`** and is invisible to `emit_cost_summary` — the "hidden unattributed headless call" hazard.

### Verbatim external invocations
**Claude streaming** (`run_claude_streaming :2421`–`:2428`):
```
env -u CLAUDECODE "$TIMEOUT_CMD" --kill-after=10 "$timeout_val" claude -p "$prompt" \
    ${agent_args[@]+"${agent_args[@]}"} \
    --model "$current_model" --dangerously-skip-permissions --verbose \
    --output-format stream-json --json-schema "$schema" \
    2>"$stderr_log" | tee "$stream_file" > /dev/null
```
**Claude one-shot** (`:273`–`:282`, hardcodes `claude-sonnet-4-6`):
```
claude -p "$prompt" --model "claude-sonnet-4-6" --dangerously-skip-permissions --output-format json [--json-schema "$schema_path"]
```
**Codex stage** (`:2514`–`:2523`):
```
env "$TIMEOUT_CMD" --kill-after=10 "$timeout_val" codex exec \
    --cd "$workdir" --full-auto ${_stage_codex_extra} \
    --skip-git-repo-check --color never --json \
    --output-last-message "$last_msg_file" "$prompt" \
    2>"$stderr_log" >"$events_file"
```
(Note: codex one-shot at `:309`–`:316` does NOT pass `--cd`, so it runs in the orchestrator CWD, not WORKTREE.)

### Engine constants (2a)
| Const | Value | Cite |
|---|---|---|
| `STAGE_TIMEOUT_INITIAL` / `_FINAL` | 1800s / 1800s | `:25`,`:28` |
| `STAGE_TIMEOUT_FALLBACK` | 1200s (declared, **unused** dead constant) | `:27` |
| `SUBTASK_STAGE_TIMEOUT_SHORT` | 900s | `:29` |
| `MAX_STAGE_RETRIES` | 2 | `:31` |
| `RETRY_COOLDOWN` | 120s | `:32` |
| `MODEL_FALLBACK_DELAY` | 10s | `:50` |
| `RATE_LIMIT_BUFFER` / `_DEFAULT_WAIT` | 60s / 3600s | `:42`,`:43` |
| `MAX_DISPATCHABLE_E2E_FAILURES` | 20 | `:44` |
| capacity util threshold (per-task) | `>= 90%` | `:2042` |
| capacity sleep clamp | min 60s, max 3600s, +jitter 0–300s | `:2073`–`:2078` |
| `capacity_wait_loop` poll | 900s chunks | `:2098` |
| `MODEL_CHAIN` | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` | `:49` |
| Pricing (stale) | Opus $15/$75, Sonnet $3/$15, Haiku $1/$5 per MTok; cache read 10%, write 125% of input | `:2726`–`:2735` |

---

## 3. Status Lifecycle + Convergence + Quality/Test Loops + Circuit Breakers (fragment 02b)

2b owns status-file lifecycle, iteration counters, convergence/plateau detection, the quality loop, the test loop, resume, and `gh` commenting. **2b makes no direct `claude`/`codex` calls — all model work goes through 2a `run_stage`** (see §4 seam).

### Status lifecycle
`init_status` (`:620`–`:680`) creates `$STATUS_FILE` (`status-ralph.json`) with top-level fields: `state` (`"initializing"` → `running` → terminal via `set_final_state`), `issue`, `base_branch`, `branch`, `worktree`, `current_stage` (init `"setup"`), `substage`/`substage_detail`, `current_task`, `execution_mode`, `stages_skipped`/`stages_executed` (mode-dependent, `:622`–`:631`), `stages` (one key per stage), `tasks` (init `[]`), `quality_iterations`/`test_iterations`/`pr_review_iterations` (init 0), `last_update`, `log_dir`. `stage_counter` is NOT in init — first written by `set_stage_completed` (`:731`).

Key writers: `update_stage` (`:682`), `set_stage_started` (`:709`, sets `state="running"`), `set_stage_completed` (`:723`), `update_task` (`:737`), `set_tasks` (`:771`, defaults each task to `status:"pending"` to fix infinite-retry-on-resume), `set_worktree_info` (`:783`), `set_substage`/`clear_substage` (`:928`/`:944`), the three `increment_*_iteration` writers (`:912`/`:920`/`:950`), and `set_final_state` (`:793`).

After **every** writer, `sync_status_to_log` (`:1464`) copies `$STATUS_FILE` → `$LOG_BASE/status.json` (self-copy guarded, `:1468`). That mirror is the **resume source of truth**.

`set_final_state` (`:793`) routes terminal failure states (`halted|error|incomplete|blocked|max_iterations_*|failure_signature_plateau|regression_plateau|persistent_infra_failure|tsc_gate_failure`, `:802`) into `emit_failure_retrospective` (`:814`), which writes `retrospective.md`, calls `detect_failure_patterns` (`:870`), and emits a cost summary via 2a `emit_cost_summary` (`:861`). Re-entrant guard `:819`.

### Quality loop (`run_quality_loop :3302`–`:3537`) — cap 5
`MAX_QUALITY_ITERATIONS=5` is a **flat cap** (`:35`, guard `:3347`). `while [[ "$loop_approved" != "true" ]]` (`:3343`); each iteration `increment_quality_iteration` then cap guard: `loop_iteration > 5` → `set_final_state "max_iterations_quality"; exit 2` (`:3347`–`:3351`). Per iteration: SIMPLIFY (skippable) → REVIEW → severity/convergence gates → FIX.

Tiers (`:3307`–`:3324`): `none` → return 0 immediately; `light` → review-only; `full` → simplify first, skip simplify after a fix iteration.

**Early-exit (auto-approve) conditions, in order:**
1. Reviewer verdict `approved` (`:3435`).
2. `review_has_only_minor_issues` — only suggestion-severity issues remain (`:3440`).
3. **Convergence:** from iteration ≥ 3, if `detect_convergence` returns true → auto-approve (`:3450`–`:3458`).
4. Fix produced no commits (HEAD unchanged) → auto-approve (`:3521`–`:3524`).

### Convergence (`detect_convergence :985`–`:1022`) — how it exits a loop early (CANONICAL)
Per issue, a **fingerprint** = `(file // "unknown") + ":" + (description lowercased, whitespace-collapsed)` (`:994`–`:998`). It counts how many current fingerprints already appear in the prior-issues file (`grep -qF`, `:1010`), then appends current fingerprints for the next round (`:1016`). **Returns 0 (true = "converging") only if `total_current > 0 && repeat_count == 0`** — i.e. every current issue is net-new and NO prior issue is being re-flagged (`:1018`). Returns 1 if there are repeats or no issues.

The interpretation: if the reviewer keeps surfacing brand-new issues with zero overlap from prior rounds, the loop is making progress on a moving target and the harness **auto-approves to exit early** rather than burning all iterations. Convergence is wired into the **quality loop** (acts from iteration ≥ 3, `:3450`) and **PR review** (`impl-orch:1946`, iter ≥ 2) — **but NOT the test loop** (CANONICAL): the test loop relies purely on plateau circuit-breakers. This is the half-built version of the design-doc's adaptive-cap goal.

### Test loop (`run_test_loop :3588`–`:4346`) — caps 10 / 3 / 2
`MAX_TEST_ITERATIONS=10` base (`:36`). `effective_max_test_iterations` (`:3674`–`:3682`): **full=10; lite+E2E=3; lite=2** (CANONICAL).

Pre-loop **TSC gate** (`:3690`–`:3755`): if frontend changed, run `npx tsc --noEmit`; up to **2** auto-fix attempts (`:3718`, bare literal not a named const), else `set_final_state "tsc_gate_failure"; exit 2` (`:3746`).

`while [[ "$loop_complete" != "true" ]]` (`:3757`); each iteration `increment_test_iteration`; cap guard `test_iteration > effective_max` → `max_iterations_test; exit 2` (`:3761`–`:3765`).
- **Incremental phase** (`:3819`–`:4009`): run changed tests; infra-failure gate (cap `MAX_CONSECUTIVE_INFRA_FAILURES=3` → `persistent_infra_failure; exit 2`, `:3853`–`:3858`); regression-aware (0 regressions ⇒ pass); on regressions, signature-plateau check then `continue`.
- **Full-suite gate** (`:4011`–`:4263`): infra gate (same cap, `:4100`–`:4105`); 0 regressions ⇒ pass (`:4138`–`:4143`); **regression plateau** — count unchanged across 3 consecutive full-suite runs → `regression_plateau; exit 2` (`:4150`–`:4167`, `MAX_CONSECUTIVE_REGRESSION_PLATEAU=3`); signature-plateau check (`:4170`); on regressions dispatch classified fixes (unit → `python-backend-developer`; e2e → 2a `triage_and_dispatch_e2e_fixes`) then `continue`.
- **Validation** (`:4265`–`:4343`): `passed`/`approved` → `loop_complete=true`; else fix and re-loop.

Regression-aware pass: 0 regressions (all inherited from base) is a pass in both phases (`:3879`–`:3883`, `:4138`–`:4143`); inherited failures are excluded from fix prompts.

### Circuit breaker — how it trips (CANONICAL: `check_failure_signature_plateau :3611`–`:3638`, nested in `run_test_loop`)
Computes `signature = jq '.[].test' | sort | shasum -a 256 | cut -d' ' -f1` over the **regressions** JSON (`:3616`) — a deterministic fingerprint of the *exact set* of failing test NAMES. Compares to `last_failure_signature`: if equal, `consecutive_identical_signature++`; else reset to 1 (`:3618`–`:3624`).

**Trip condition (`:3627`):** when `consecutive_identical_signature >= MAX_IDENTICAL_FAILURE_SIGNATURE` (**= 2**, `:39`) — i.e. the identical failing-test set recurs on **two consecutive runs** — it logs, posts a `comment_issue` ("Failure Signature Plateau"), calls `set_final_state "failure_signature_plateau"`, and `exit 2` (`:3635`–`:3636`). Called after both the incremental phase (`:3895`) and full-suite phase (`:4170`).

This is distinct from the count-based **regression plateau** (`:4150`, catches "same *count*, different tests"); the signature check catches "exact same tests" even when the fix agent swaps which tests fail. Both counters reset on a clean/zero-regression run (`:4005`–`:4007`, `:4172`–`:4176`).

### Other failure handling (2b)
- Quality cap 5 → `max_iterations_quality`, exit 2.
- Test cap 10/3/2 → `max_iterations_test`, exit 2.
- TSC gate 2 attempts → `tsc_gate_failure`, exit 2.
- Infra circuit breaker: 3 consecutive infra failures (either phase) → `persistent_infra_failure`, exit 2; each triggers `reset_test_infrastructure` then `continue`.
- Force-push remediation (`detect_force_push_remediation :1036`): if HEAD commit count dropped AND tree is clean, the "fix" was history cleanup, not real change — caller decrements the review-attempt counter (`impl-orch:1309`) so it isn't penalized.

### Resume (`validate_resume_status :1324`, `load_resume_state :1358`)
Validate: status file must exist; required fields `issue, branch, worktree, current_stage, log_dir` present/non-null (`:1333`–`:1342`); `state != "completed"` (`:1347`). Load restores `TASK_ID, BASE_BRANCH, BRANCH, WORKTREE, LOG_BASE`, the three iteration counters, `STAGE_COUNTER` (+ counter file), resets PID file, reads `RESUME_PR_NUMBER` from `.stages.pr.pr_number` (`:1361`–`:1408`).

### 2b constants
| Const | Value | Cite |
|---|---|---|
| `MAX_QUALITY_ITERATIONS` | 5 (flat) | `:35`, guard `:3347` |
| `MAX_TEST_ITERATIONS` | 10 full / 3 lite+E2E / 2 lite | `:36`, `:3674`–`:3681` |
| `MAX_CONSECUTIVE_INFRA_FAILURES` | 3 | `:3853`/`:4100` |
| `MAX_CONSECUTIVE_REGRESSION_PLATEAU` | 3 | `:4160` |
| `MAX_IDENTICAL_FAILURE_SIGNATURE` | 2 (circuit breaker) | `:39`, trip `:3627` |
| `MAX_TASK_REVIEW_ATTEMPTS` | 3 | `impl-orch:1062` |
| `MAX_PR_REVIEW_ITERATIONS` | 3 | `impl-orch:1783` |
| `MAX_DISPATCHABLE_E2E_FAILURES` | 20 | `:44` |
| TSC-gate auto-fix attempts | 2 (bare literal) | `:3718` |
| Convergence min iteration | 3 (quality, literal `:3450`); 2 (PR review, `impl-orch:1945`) | — |

---

## 4. The 2a ↔ 2b Seam

The pipeline (03) drives both halves; 2b's loops call back into 2a's `run_stage` for every model step.

### 2b → 2a calls
| 2b caller | 2a callee | Cite |
|---|---|---|
| `run_quality_loop` (simplify/review/fix) | `run_stage` | `:3385`/`:3422`/`:3510` |
| `run_test_loop` (tsc-fix/unit-fix/validate/quality-fix) | `run_stage` | `:3728`/`:3979`/`:4238`/`:4300`/`:4336` |
| `run_test_loop` (E2E fix dispatch) | `triage_and_dispatch_e2e_fixes` (2a, `:1863`) | `:3985`,`:4246` |
| `emit_failure_retrospective` | `emit_cost_summary` (2a, `:2704`) | `:861` |
| all status writers | `status_file_update`/`status_file_write` (lib `status-file-helpers.sh`) | — |

### 2a → 2b calls
| 2a caller | 2b callee | Cite |
|---|---|---|
| `run_stage` proactive/timeout capacity | `status_file_update`, `sync_status_to_log` | `:2894`,`:3055`,`:3088` |
| `orchestrator_cleanup` | `release_worktree_ownership`, `status_file_update` | `:1525`,`:1589`,`:1542` |
| `triage_and_dispatch_e2e_fixes` | `set_substage`, `run_stage`, `comment_issue`, `run_tests_direct`, `compute_regressions`, `classify_failures` | `:1875`,`:1891`,`:1893`,`:1916`,`:1931`,`:1934` |

### Seam notes
- `run_stage` runs in a `$(...)` subshell, so all cross-process state (stage counter, child PIDs, status) is **file-based**, not in-memory (`:2855`).
- The circuit breaker (`check_failure_signature_plateau`) and convergence (`detect_convergence`) are 2b; 2a owns only their **constants** (`:39`). 2a has **no circuit breaker** of its own — only the capacity/rate-limit fallback chains.
- The cost ledger is 2a (`record_stage_invocation`/`emit_cost_summary`), but 2b's retrospective triggers `emit_cost_summary` — and Ralph's `run_provider_oneshot` calls bypass the ledger entirely (cost-attribution gap, §2).

---

## Cross-Fragment Conflicts / Open Items (noted, not resolved)
- **Stale model pins** appear in both 2a and 2b (shared header `:49`); fragments agree, no conflict. Current is 4.8; pins say `4-7`/`4-6`.
- **`baseline_failures`** status field is initialized (`:665`) but no 2b writer updates it — DISPUTED; may be written by 2a or `regression-helpers.sh` (not in either mapped fragment).
- **`STAGE_TIMEOUT_FALLBACK` (1200s)** is unused within `orchestrator-common.sh`; not confirmed whether `implement-orchestrator.sh` or `ralph-loop.sh` reference it.
- **Real Opus pricing ($5/$25)** is taken from `docs/orchestration-template.md`, not independently re-verified against a live source.
- **Ralph one-shot callers** (dependency analysis, learnings summarization) live in `ralph-loop.sh` — confirmed to bypass the cost ledger by code path, but the exact caller list is out of these fragments.
