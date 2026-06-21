# Fragment: lib/orchestrator-common.sh (2a half — dispatch / routing / cost / capacity / cleanup / setup)
Source commit: <uncommitted working tree at /Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh>   Mapped lines: 24–127, 129–365, 393–547, 1512–1661, 1793–1847, 1849–1975 (e2e), 1977–2234, 2240–2645, 2647–2846, 2848–3287

> Scope: the 39 defs owned by 2a (38 top-level + 1 nested `_orchestrator_signal_handler`). The 2b state-mutation helpers (`update_stage`, `set_substage`, `detect_convergence`, `sync_status_to_log`, `status_file_update`, `release_worktree_ownership`, the quality/test loops) are documented only at the SEAM (§8). Absolute paths below all refer to `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh` unless noted.

---

## 1. Role & entry points — who invokes it, with what argv

This file is a **sourced library**, never executed (`:1–5` — guarded by `_ORCHESTRATOR_COMMON_LOADED`; sources `lib/status-file-helpers.sh` at `:8`). The 2a functions are the **execution engine** the per-task pipeline (`implement-orchestrator.sh`) and the batch scheduler (`ralph-loop.sh`) call into. Entry points by caller:

- **`run_stage <stage_name> <prompt> <schema_file> [agent]`** (`:2848`) — THE central dispatch. Called once per pipeline stage by `implement-orchestrator.sh` and by the in-file loops (`triage_and_dispatch_e2e_fixes :1891`, the quality/test loops in 2b). Always invoked inside `$(...)` command substitution (note `:2855` — the stage counter is file-based *because* run_stage runs in a subshell, so in-memory state cannot persist).
- **`run_stage_with_timeout <secs> <run_stage args…>`** (`:89`) — thin wrapper that temporarily overrides `STAGE_TIMEOUT`/`STAGE_TIMEOUT_OVERRIDE`/`STAGE_TIMEOUT_FINAL_OVERRIDE`, calls `run_stage "$@"`, then restores. Used for short wiring-only subtasks.
- **`run_provider_oneshot <prompt> <schema_path> <out_var> [timeout]`** (`:242`) — a SEPARATE one-shot dispatcher for analytical JSON calls (dependency analysis, learnings summarization) used by Ralph. **Bypasses run_stage entirely** (see §9 / cost-attribution gap).
- **`run_setup_stage` / `run_setup_stage_micro`** (`:497`, `:525`) — pure-shell setup (no LLM), called once at pipeline start to create/reuse a worktree and install deps.
- **`register_orchestrator_traps`** (`:1639`) — called once after STATUS_FILE/worktree init; installs the TERM/INT/HUP/EXIT traps. `orchestrator_cleanup` (`:1512`) is the trap body.
- **`triage_and_dispatch_e2e_fixes`** (`:1863`) — called from the test loop (2b) when E2E regressions are detected.
- Helpers (`get_stage_model :55`, `should_use_codex :191`, `record_stage_invocation :2653`, `emit_cost_summary :2704`, `check_capacity :2024`, etc.) are internal to the above.

---

## 2. Inputs — flags, env vars, files read

**Env vars (consumed by 2a):**

| Var | Default | Effect | Cite |
|---|---|---|---|
| `ORCHESTRATOR_PROVIDER` | `claude` | Global provider; `codex` routes EVERY stage (incl. analytical) through codex | `:174`, `:194–201` |
| `TASK_PROVIDER` | `claude` | Per-task `:codex` tag; routes only file-patching (eligible) stages to codex | `:206` |
| `STAGE_TIMEOUT` / `STAGE_TIMEOUT_OVERRIDE` / `STAGE_TIMEOUT_FINAL_OVERRIDE` | `1800` / unset / unset | First-attempt and retry timeouts; overrides win | `:30`, `:2885–2886` |
| `WORKTREE` (`worktree`) | — | Codex `--cd` workdir; cleanup target | `:2480`, `:1514` |
| `BRANCH` (`branch`), `BASE_BRANCH` | — / `main` | Branch cleanup; empty-branch deletion vs keep | `:1515`, `:1610` |
| `EXECUTION_MODE` | — | `micro` ⇒ skip worktree removal (SAFETY: WORKTREE = repo root) | `:1598` |
| `LOG_BASE` | — | Root for `stages/`, `context/stage-costs.jsonl`, `cost-summary.md`, `status.json` archive | `:2662`, `:2706`, `:1553` |
| `STATUS_FILE` | — | status-ralph.json; updated to paused/running/killed | `:1540`, `:2894` |
| `LEARNINGS_FILE` | — | If non-empty, prepended to every stage prompt (retry-with-learnings) | `:2919–2928` |
| `SCHEMA_DIR` | — | Schema lookup dir | `:2863`, `:2597`, `:2835` |
| `STAGE_COUNTER_FILE`, `STAGE_INDEX` | — | File-based stage counter + index table | `:2856`, `:2952` |
| `_ORCHESTRATOR_PID_FILE` | — | File-based child-PID tracker (survives subshell barrier) | `:2433`, `:1561` |
| `GITHUB_REPO` | `cperler/heysoo` | **Hardcoded Hey Soo! default** (`:1668`) | `:1668` |
| `CODEX_ELIGIBLE_STAGES` | (8 globs) | implement-/fix-* allowlist for the `:codex` tag | `:145–154` |
| `MODEL_CHAIN` | Opus→Sonnet→Haiku | Fallback tier order | `:49` |
| `TIMEOUT_CMD` | `timeout`/`gtimeout` | Detected at load; hard-exit if absent | `:15–22` |

**Files read:** `$SCHEMA_DIR/<schema>.json` (`:2870`); `$LEARNINGS_FILE` (`:2921`); `/tmp/.claude_usage_cache` (`:2025`, lines 1=util,3=reset); `$_ORCHESTRATOR_PID_FILE` (`:1561`); lock files for dep-hash (`uv.lock`, `package-lock.json` at `:438/:460`). Runs `bash ~/.claude/fetch-usage.sh` to refresh the cache (`:2028`).

---

## 3. Outputs — files written, exit codes, side effects

**Files written:**
- `$LOG_BASE/stages/NN-<stage>.log` — 3-line format: `=== <label> output (model: M) ===` / `<result JSON>` / `=== exit code: N ===` (`:2458–2460` claude; `:2642–2644` codex). NN = zero-padded file-based counter (`:2856–2860`).
- `…NN-<stage>.stream.jsonl` (claude stream replay, `:2410/:2428`); `…NN-<stage>.stderr` (`:2884`); `…NN-<stage>.codex-last-message.json` + `.codex-events.jsonl` (codex, `:2478–2479`); `…NN-<stage>.md` (extracted markdown, `:2351`).
- **`$LOG_BASE/context/stage-costs.jsonl`** — one JSONL row per invocation. Fields (`:2690–2693`): `label, model, provider, start_epoch, duration_seconds, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, exit_code`. Tokens pulled from `.usage.{input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens}` (`:2673–2676`).
- **`$LOG_BASE/cost-summary.md`** — aggregated markdown (`:2785`, format `:2769–2785`).
- `$STAGE_INDEX` — appends `| NN | stage | agent | provider | model | HH:MM:SS | NN-stage.log |` (`:2953–2955`).
- `$LOG_BASE/status.json` — cleanup syncs STATUS_FILE here on signal (`:1553`).
- Dep-hash files `.worktree-dep-hash-npm` / `.worktree-dep-hash-uv` (`:439/:461`, written `:450/:476`).

**Exit codes:** `run_stage` → 0 (structured/ inferred output printed to stdout), 1 (schema missing `:2866`, timeout-exhausted `:3173/:3196`, empty output `:3205`, no structured output `:3259`). `run_provider_oneshot` → 0 ok / 2 invalid provider / 124 timeout / 127 CLI missing / underlying rc (`:239–241`). `get_orchestrator_provider` → 1 on invalid value (`:185`).

**Side effects:** git worktree add/remove/prune (`:511`, `:1602–1606`), branch -D (`:1616`), `npm install`/`uv sync` (`:448/:469`), `gh issue/pr comment` (2b helpers it calls — `comment_issue`), kills processes (`cleanup_stale_processes :1805…`, child PIDs `:1572–1583`), network via `claude -p` / `codex exec` / `fetch-usage.sh`.

---

## 4. Control flow

### `run_stage` state machine (`:2848–3287`)
1. **Counter+log setup** (`:2855–2860`): increment file counter, derive `NN-stage.log`.
2. **Schema validation** (`:2863–2870`): missing ⇒ emit error JSON, return 1.
3. **Model selection** (`:2874–2882`): `current_model = get_stage_model(stage)`; find `model_index` in MODEL_CHAIN.
4. **Proactive capacity check** (`:2891–2911`): if **not codex** AND `check_capacity` (≥90%): set status paused, try `try_capacity_model_fallback`; on fail `capacity_wait_loop 3600 "pre-stage"`; restore running. (`capacity_fallback_used` short-circuits the main run.)
5. **Learnings prepend** (`:2919–2928`).
6. **Provider decision** (`:2939–2948`): `should_use_codex` ⇒ `_row_provider=codex`; `render_stage_prompt` rewrites the prompt for the provider; append stage-index row; update status `current_model`/`current_provider`.
7. **Execution** (`:2965–2988`): if codex eligible, `run_codex_stage`; success requires `_rc_exit_code==0 && structured_output.status=="success"` (`:2973`) else **fall through to Claude** (reset `_rc_output`/`_rc_exit_code`, `:2980–2981`). Otherwise `run_claude_streaming`.
8. **Markdown extraction** (`:2993`).
9. **Timeout branch (exit 124)** (`:2997–3199`): split on `output_bytes < 100`:
   - **Near-empty** (`:3020`): dump stderr, kill stale procs; `check_capacity` ⇒ capacity path (fallback models → `capacity_wait_loop 1800 "post-timeout"` → capacity-resume retry; on still-fail return 1, `:3081–3089`). Else **logic-hang retry loop** (`:3106–3168`): `while timeout_retry < MAX_STAGE_RETRIES(2)`, sleep `RETRY_COOLDOWN(120)`, cleanup, re-check capacity (fallback or `capacity_wait_loop 1800 "retry-loop"`, **decrements retry counter so capacity waits don't burn attempts** `:3151`), then `run_claude_streaming`; success = `exit!=124 && bytes>=100`. Exhausted ⇒ return 1 (`:3172`).
   - **Substantial output** (`:3177–3198`): if `structured_output.status=="success"` treat as completed; else error, return 1.
10. **Empty-output guard** (`:3202–3206`): `<100` bytes ⇒ return 1.
11. **Rate-limit branch** (`:3209–3238`): `detect_rate_limit` ⇒ walk `MODEL_CHAIN[model_index+1..]`, `sleep MODEL_FALLBACK_DELAY(10)`, retry each; if all limited, `handle_rate_limit` (long sleep) then one retry with `current_model`.
12. **Structured-output extraction** (`:3243–3287`): prefer `.structured_output`; else **infer** status from prose `.result` via keyword regexes (`fail|error|changes.requested…` negated by `fixed|resolved|passing…`, `:3272–3278`) → synthesize `{status,result,summary,comments}`.

### `run_codex_stage` success heuristic (`:2469–2645`)
- Snapshot `pre_head` (`:2499`); run `codex exec`; capture `post_head` (`:2573`). `has_new_work=true` if HEAD moved OR working tree dirty vs pre_head (`:2576–2588`).
- `_schema_valid`: load `$SCHEMA_DIR/$schema_file`, check **only top-level `required[]` keys are present** (`has($k)`) in last-message — NOT full schema conformance (`:2595–2615`).
- `derived_status="success"` iff `exit==0 && no codex_error && (has_new_work || _schema_valid)` (`:2620–2624`). Union: patching stages pass on git-moved; analytical stages pass on required-keys-present.

### Capacity poll loop (`capacity_wait_loop :2095–2129`)
Computes planned sleep via `calculate_capacity_sleep`; sleeps in `poll_interval=900s` chunks; re-checks `check_capacity` each chunk and **exits early** when capacity recovers (`:2114–2118`).

---

## 5. External invocations (VERBATIM)

**Claude streaming** (`run_claude_streaming :2421–2428`):
```
env -u CLAUDECODE "$TIMEOUT_CMD" --kill-after=10 "$timeout_val" claude -p "$prompt" \
    ${agent_args[@]+"${agent_args[@]}"} \   # --agent <name> when agent set
    --model "$current_model" \
    --dangerously-skip-permissions \
    --verbose \
    --output-format stream-json \
    --json-schema "$schema" \               # compacted schema JSON (jq -c), not the path
    2>"$stderr_log" | tee "$stream_file" > /dev/null
```

**Claude one-shot** (`run_provider_oneshot :273–282`) — **HARDCODES `claude-sonnet-4-6`**, NOT tiered:
```
claude -p "$prompt" --model "claude-sonnet-4-6" --dangerously-skip-permissions --output-format json [--json-schema "$schema_path"]
```
(wrapped in `env -u CLAUDECODE` + optional `$TIMEOUT_CMD --kill-after=10 <s>`.)

**Codex stage** (`run_codex_stage :2514–2523`):
```
env "$TIMEOUT_CMD" --kill-after=10 "$timeout_val" codex exec \
    --cd "$workdir" --full-auto ${_stage_codex_extra} \   # --add-dir <git-common-dir>
    --skip-git-repo-check --color never --json \
    --output-last-message "$last_msg_file" "$prompt" \
    2>"$stderr_log" >"$events_file"
```
**Codex one-shot** (`run_provider_oneshot :309–316`): `codex exec --skip-git-repo-check --full-auto [--add-dir <git-common-dir>] --color never --json --output-last-message <tmp> "$prompt"`.

**git/gh:** `git worktree add/remove --force/prune` (`:511,:1602–1606`), `git rev-parse HEAD` / `--git-common-dir` (`:2499,:2507`), `git diff --name-only` (`:2578`), `git log --oneline base..br` (`:1613`); 2b `gh issue/pr comment` via `comment_issue`. **Capacity:** `bash ~/.claude/fetch-usage.sh` (`:2028`).

---

## 6. Constants & tunables

| Const | Value | Cite |
|---|---|---|
| `STAGE_TIMEOUT_INITIAL` / `_FINAL` | 1800s / 1800s | `:25,:28` |
| `STAGE_TIMEOUT_FALLBACK` | 1200s (declared, unused) | `:27` |
| `SUBTASK_STAGE_TIMEOUT_SHORT` | 900s | `:29` |
| `MAX_STAGE_RETRIES` | 2 | `:31` |
| `RETRY_COOLDOWN` | 120s | `:32` |
| `MODEL_FALLBACK_DELAY` | 10s | `:50` |
| `RATE_LIMIT_BUFFER` / `_DEFAULT_WAIT` | 60s / 3600s | `:42,:43` |
| `MAX_DISPATCHABLE_E2E_FAILURES` | 20 | `:44` |
| capacity util threshold | `>= 90%` | `:2042` |
| capacity sleep clamp | min 60s, max `max_cap`, +jitter `RANDOM%301` (0–300s) | `:2073–2078` |
| `capacity_wait_loop` poll | 900s chunks | `:2098` |
| **`MODEL_CHAIN`** | `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001` | `:49` |
| **Pricing (emit_cost_summary)** | Opus $15/$75, Sonnet $3/$15, Haiku $1/$5 per MTok; cache read 10%, write 125% of input | `:2726–2735` |

---

## 7. Failure handling

- **Codex → Claude fall-through:** codex non-success ⇒ retry on Claude (`:2975–2987`). Per-stage, automatic.
- **Model fallback chain (rate-limit):** Opus→Sonnet→Haiku, `sleep MODEL_FALLBACK_DELAY` between (`:3213–3228`); if all limited, `handle_rate_limit` long sleep + single retry on stage model (`:3231–3237`).
- **Capacity fallback (`try_capacity_model_fallback :2142–2170`):** walk `MODEL_CHAIN[mi+1..]`, sleep 10s, run; success = `exit!=124 && bytes>=100 && !detect_rate_limit` (`:2160`). Exhaustion ⇒ caller sleeps.
- **Timeout retries:** `MAX_STAGE_RETRIES=2`, cooldown 120s, SAME model (model-switch only helps 429s, not hangs `:3109–3111`).
- **No circuit breaker in 2a** — the identical-failure-signature circuit breaker (`check_failure_signature_plateau`, `MAX_IDENTICAL_FAILURE_SIGNATURE=2`) lives in 2b's `run_test_loop` (`:3611`). 2a only owns the *constants* (`:39`).
- **Re-entrant/normal-exit guards** in `orchestrator_cleanup` (`:1518,:1524`).

---

## ROUTING TABLE (required) — two-axis × execution surface

**Axis A — provider selection** (`should_use_codex :191`):
- `ORCHESTRATOR_PROVIDER=codex` ⇒ **every** stage → codex (`:194–201`).
- else `TASK_PROVIDER=codex` (`:codex` tag) ⇒ codex **only if `is_codex_eligible_stage`** (`:206–207`) where eligible = `CODEX_ELIGIBLE_STAGES` globs: `implement-task-*, fix-task-*, fix-pr-review-*, fix-review-*, fix-unit-*, fix-e2e-*, fix-test-quality-*, fix-test-*` (`:145–154`).
- else claude.

**Axis B — model + fallback** (`get_stage_model :55`):
| Stage glob | Model | Cite |
|---|---|---|
| `setup` | `claude-haiku-4-5-20251001` | `:59–60` |
| `research`,`plan`,`implement-task-*`,`fix-*` (task/pr-review/review/unit/e2e/test-quality/test) | `claude-opus-4-7` | `:62–63` |
| `evaluate`,`task-review-*`,`spec-review-*`,`code-review-*`,`simplify-*`,`review-*`,`quality-*`,`test-*`,`docs`,`pr`,`complete` | `claude-sonnet-4-6` | `:65–66` |
| `*` (default/unknown) | `claude-opus-4-7` | `:68–69` |

Fallback chain on rate-limit/capacity: **Opus → Sonnet → Haiku** (`MODEL_CHAIN :49`), entered at `model_index+1`.

**Execution surface (cell → runner):**
| Provider | Runner | Cite |
|---|---|---|
| claude (pipeline) | `run_claude_streaming` (stream-json, tiered model, --json-schema, --agent) | `:2399` |
| codex (eligible) | `run_codex_stage` (`codex exec --json --output-last-message`; success = git-moved ∪ required-keys) | `:2469` |
| **claude one-shot (Ralph)** | `run_provider_oneshot` — **hardcodes `claude-sonnet-4-6`** (`:275`), `--output-format json`, NO stream file, **NO `record_stage_invocation`** | `:242` |
| codex one-shot (Ralph) | `run_provider_oneshot` codex branch | `:284` |

**COST-ATTRIBUTION GAP:** `run_provider_oneshot` (`:242`) BYPASSES `run_stage` and never calls `record_stage_invocation` (contrast `run_claude_streaming :2454`, `run_codex_stage :2639`). Every Ralph one-shot (dependency analysis, learnings summarization) is a real billable `claude -p` / `codex exec` call that **NEVER lands in `stage-costs.jsonl`** and is invisible to `emit_cost_summary`. This is exactly the "hidden, unattributed headless call" risk from `docs/orchestration-template.md` §4.

---

## PROMPT CAPTURE — `render_stage_prompt` (`:2810`, internal to `run_stage :2848`)

Called at `run_stage :2948`: `prompt=$(render_stage_prompt "$stage_name" "$_row_provider" "$prompt" "$schema_file")`. The prompt fed in has already had **learnings prepended** at `:2922–2927`:
```
# Learnings from previous attempts
${learnings}

---

${prompt}
```
`render_stage_prompt` behavior:
- **claude (or unknown provider):** byte-identical pass-through (`:2816–2820`).
- **codex:** (1) strip lines matching Claude-only tool tokens via `grep -Ev '(^|[^A-Za-z0-9_])(Skill|TodoWrite|Agent|run_in_background)([^A-Za-z0-9_]|$)'` (`:2831–2832`); (2) append compacted schema block when `$SCHEMA_DIR/$schema_file` exists (`:2834–2841`):
  ```
  \n\nRequired JSON schema (<schema_file>):\n<jq -c schema>
  ```
  (3) append postamble verbatim (`:2844`): `Emit exactly one JSON object on stdout that matches the required JSON schema above. Use the exact required property names. No prose, no tool calls, no markdown fences.`
- `stage_name` arg is accepted but **unused** today (`:2807–2808`).

The E2E fix prompts are assembled inline in `triage_and_dispatch_e2e_fixes` (heredoc-style string at `:1877–1888` and `:1952–1963`) — both embed `$TASK_ID`, worktree, branch, and the failure list.

---

## COST LEDGER — full (`record_stage_invocation :2653`, `emit_cost_summary :2704`)

`record_stage_invocation` appends one JSONL row to `$LOG_BASE/context/stage-costs.jsonl` after **every** claude streaming call (`:2454`) and codex call (`:2639`) — including retries/fallbacks (so token totals double-count work across retries by design). Fields exhaustively at `:2690–2693` (listed §3). No-op if `LOG_BASE` unset/missing (`:2662`). Tokens from provider `.usage` (`:2673–2676`); default 0 when absent (codex rows are typically all-zero tokens since codex output carries no `.usage`).

`emit_cost_summary` (`:2704`): slurps the JSONL, computes per-entry cost (`:2730–2735`):
`cost = (in*price_in + out*price_out + cache_read*price_in*0.1 + cache_write*price_in*1.25) / 1e6`.
Writes `$LOG_BASE/cost-summary.md` (`:2785`): total invocations, wall minutes, est USD, and a per-model breakdown table; logs a boxed summary (`:2787–2790`). Provider label is title-cased from distinct providers (`:2739–2746`).

**STALE PRICING (flag):** `model_price` (`:2726–2729`) prices **Opus at $15 / $75 per MTok**. Per `docs/orchestration-template.md` §2.3 / §5, current Opus is **$5 / $25** — the report **overstates Opus ~3×**. Sonnet $3/$15 and Haiku $1/$5 are encoded; default-bucket also uses Opus $15/$75. The MODEL_CHAIN/`get_stage_model` pins (`claude-opus-4-7`, `:49,:63`) are also stale vs CLAUDE.md (`4-6`) and current 4.8 — pricing keyed on substring `test("opus")` so it would still apply, but the named pin is wrong.

---

## CAPACITY MATH — exact formula (`calculate_capacity_sleep :2060`)

```
now_epoch   = date +%s
reset_epoch = parse(CAPACITY_RESET_AT, BSD `date -j -f`, fallback GNU `date -d`, fallback now+300)   (:2067-2069)
sleep_secs  = reset_epoch - now_epoch + 60                                                            (:2070)
clamp:  sleep_secs < 60   -> 60
        sleep_secs > max_cap -> max_cap   (max_cap default 3600)                                      (:2073-2074)
jitter      = RANDOM % 301                 # 0..300s                                                  (:2077)
sleep_secs += jitter                                                                                  (:2078)
```
`check_capacity` (`:2024`): refresh `/tmp/.claude_usage_cache` via `fetch-usage.sh`; read line 1 = 5h utilization %, line 3 = reset ts; **at capacity iff util ≥ 90** (`:2042`), sets `CAPACITY_RESET_AT`. Cache missing/empty ⇒ assume HAS capacity (return 1, `:2031,:2038`).

**Codex success heuristic** (recap, `:2620–2624`): `success = exit0 ∧ no_error ∧ (git_HEAD_moved ∨ working_tree_dirty ∨ schema_required_keys_present)`.

---

## CIRCUIT BREAKER / FALLBACK (`try_capacity_model_fallback :2142`, `detect_rate_limit :1977`, `handle_rate_limit :2223`)

- **`detect_rate_limit`** (`:1977`): checks `.structured_output.status` first (`success`⇒not limited, `rate_limit`⇒limited); if `is_error==false` and `.result` present ⇒ not limited (`:1988`); else for errors, regex on `.result`: `rate.limit|429|too many requests|quota.exceeded|hit your limit` (`:2013`).
- **`extract_wait_time`** (`:2172`): parse order — `retry.after N` → `wait N min` → `resets Npm/am` (capped 4h) → default `RATE_LIMIT_DEFAULT_WAIT=3600`.
- **`handle_rate_limit`** (`:2223`): `wait = extract_wait_time + RATE_LIMIT_BUFFER(60)`, log resume time, `sleep`.
- **`try_capacity_model_fallback`** (`:2142`): on capacity, try cheaper MODEL_CHAIN tiers via `run_claude_streaming` instead of sleeping; success criterion `:2160` = `exit!=124 ∧ bytes≥100 ∧ !detect_rate_limit`; sets `CAPACITY_FALLBACK_MODEL`.
- True identical-signature **circuit breaker is 2b** (`check_failure_signature_plateau`, `MAX_IDENTICAL_FAILURE_SIGNATURE=2`).

---

## 8. Coupling — generic vs Hey Soo!-specific

**Generic (extract ~as-is):** `run_stage` skeleton, two-axis routing (`should_use_codex`/`get_orchestrator_provider`), `get_stage_model`, `MODEL_CHAIN` fallback, capacity throttle (`check_capacity`/`calculate_capacity_sleep`/`capacity_wait_loop`/`try_capacity_model_fallback`), rate-limit detection, cost ledger (`record_stage_invocation`/`emit_cost_summary`), codex adapter (`run_codex_stage`/`render_stage_prompt`), traps/cleanup, `hash_file_sha256`/dep-hash. These should take `INSTALL_CMD`/`SCHEMA_DIR`/pricing-table/model-config as injected config (per template §5).

**Hey Soo!-specific (needs adapter):**
- `cleanup_stale_processes` (`:1795`) — hardcoded Vite/Playwright/chrome-headless-shell/esbuild process patterns. Generic shape: a `STALE_PROCESS_PATTERNS` list.
- `install_frontend_dependencies` / `install_python_dependencies` (`:436,:480`) — `npm install` + `uv sync` over `lambda/*/` + root. Generic: `INSTALL_CMDS[]`.
- `REPO="${GITHUB_REPO:-cperler/heysoo}"` (`:1668`) hardcoded.
- `check_capacity` reads `~/.claude/fetch-usage.sh` and `/tmp/.claude_usage_cache` (Claude-subscription-specific capacity probe).

**SEAM — calls 2a makes into 2b functions:**
| 2a caller | 2b callee | Cite |
|---|---|---|
| `run_stage` proactive/timeout capacity | `status_file_update`, `sync_status_to_log` | `:2894,:3055,:3088` |
| `run_provider_oneshot`, setup, codex, get_orchestrator_provider | `log`, `log_error` | `:267,:185,:2492` |
| `orchestrator_cleanup` | `release_worktree_ownership`, `status_file_update` (via helpers lib) | `:1525,:1589,:1542` |
| `triage_and_dispatch_e2e_fixes` | `set_substage`, `run_stage`, `comment_issue`, `run_tests_direct`, `compute_regressions`, `classify_failures` | `:1875,:1891,:1893,:1916,:1931,:1934` |
| `run_claude_streaming`/`run_codex_stage` | (none into 2b except `record_stage_invocation` which is 2a) | — |

**Hey Soo!-coupled e2e functions (flag):** `triage_and_dispatch_e2e_fixes` (`:1863`, 2a) hard-references `tests/e2e/`, Playwright, `bulletproof-frontend-developer` agent, `--skip-infra`, `infra/` diff detection. The two 2b e2e-policy functions (`evaluate_e2e_policy :1201`, `merge_e2e_policy_review_finding :1277`) are tagged in the inventory but owned by 2b.

---

## 9. Anomalies (vs docs/orchestration-template.md)

1. **Unattributed one-shot calls (COST GAP).** `run_provider_oneshot` (`:242`) bills real tokens but never calls `record_stage_invocation` → invisible to the ledger/summary. Directly the §4 "hidden headless call" hazard. **(highest priority)**
2. **Stale Opus pricing.** `$15/$75` at `:2726` vs current `$5/$25` → ~3× overstatement (matches doc §2.3, §5).
3. **Stale model pins.** `claude-opus-4-7` (`:49,:63`) / `claude-sonnet-4-6` vs CLAUDE.md `4-6` and current 4.8. `run_provider_oneshot` independently hardcodes `claude-sonnet-4-6` (`:275`) — a *second* place a model is pinned, easy to drift.
4. **Thin codex success heuristic.** `_schema_valid` checks only top-level `required[]` key *presence* via `has()`, not types/nesting/full conformance (`:2602–2611`); patching success is "HEAD moved or tree dirty" — a no-op commit or unrelated dirty file would read as success (doc §2.4).
5. **`STAGE_TIMEOUT_FALLBACK` (1200s, `:27`) is declared but never read** — `run_stage` retries use `STAGE_TIMEOUT_FINAL` (1800) and `retry_stage_timeout`. Dead constant.
6. **Token double-counting by design.** Every retry/fallback emits its own cost row (`:2454` fires on each `run_claude_streaming`), so `emit_cost_summary` sums gross attempts, not net successful work — fine for "what did this cost" but not "what did the successful path cost."
7. **`check_capacity` runs `fetch-usage.sh` synchronously in foreground inside the proactive pre-stage check** (`:2028` via `:2891`) — adds latency to every non-codex stage and silently assumes capacity if the script/cache is absent (`:2031`).
8. **Codex one-shot in `run_provider_oneshot` does not pass `--cd`** (`:309–316`) unlike `run_codex_stage` (`:2515`), so it runs in the orchestrator's CWD, not WORKTREE — possible wrong-directory analysis when worktrees are active.

---

## DISPUTED / needs cross-fragment confirmation
- The **exact set of `run_provider_oneshot` callers** (dependency analysis, learnings summarization) lives in `ralph-loop.sh` — not in this file. Confirm in the ralph fragment whether those callers separately record cost (they likely don't).
- `STATUS_FILE` state vocabulary (`paused`/`running`/`killed`) and `status_file_update`/`release_worktree_ownership` definitions live in `lib/status-file-helpers.sh` (sourced `:8`) — confirm field names there.
- Whether `STAGE_TIMEOUT_FALLBACK` is referenced by `implement-orchestrator.sh` or `ralph-loop.sh` (I only confirmed it's unused *within this file*).
- Current 4.8 Opus pricing ($5/$25) is taken from `docs/orchestration-template.md`; not independently re-verified against a live pricing source.
