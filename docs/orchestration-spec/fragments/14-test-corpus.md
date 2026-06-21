# Fragment: Test Corpus
Source paths: `.claude/scripts/implement-issue-test/*.bats` (20 files),
              `.claude/scripts/tests/*.sh` (6 files),
              `tests/shell/**/*.bats` (8 files),
              `.claude/scripts/test-*.sh` (7 loose scripts)

---

## 1. Test Corpus Overview

Total test files: **41** across four zones.  
Estimated test-case count: **~450** (bats `@test` blocks ≈ 350 counted; sh-based assertions ≈ 100).  
Frameworks in use:

| Zone | Framework | Runner |
|---|---|---|
| `implement-issue-test/*.bats` | BATS | `bats` (run via `run-tests.sh`) |
| `.claude/scripts/tests/*.sh` | Raw bash (`assert_eq`/`assert_contains` inline) | `bash <file>` |
| `tests/shell/**/*.bats` | BATS | `.claude/scripts/test-shell.sh` → `bats` |
| `.claude/scripts/test-*.sh` (loose) | Raw bash | `bash <file>` |

---

## 2. Behavioral-Contract Table

### Zone A — `implement-issue-test/*.bats` (20 files, BATS)

Sourced via `implement-issue-test/helpers/test-helper.bash`.  
The helper calls `source_orchestrator_functions()` which sources
`lib/orchestrator-common.sh` + extracts `main()` from `implement-orchestrator.sh`.

---

#### A-01 `test-argument-parsing.bats` (13 cases)
**Unit under test:** `implement-orchestrator.sh` CLI argument parser

| # | Key invariant | File:line |
|---|---|---|
| 1 | No args → exit 3, message `"--task/--issue and --branch are required"` | :22 |
| 2 | `--issue` only → exit 3 | :28 |
| 3 | `--branch` only → exit 3 | :33 |
| 4 | `--issue` without value → exit 3, `"--issue requires a value"` | :40 |
| 5 | `--branch` without value → exit 3, `"--branch requires a value"` | :45 |
| 6 | `--agent` accepted, prints `"Agent: <value>"` in header | :56 |
| 7 | `--agent` without value → exit 3 | :65 |
| 8 | `--status-file` accepted, prints `"Status file: <value>"` | :71 |
| 9 | `--status-file` without value → exit 3 | :78 |
| 10 | `--help` / `-h` → exit 3, prints `"Usage:"` | :88,96 |
| 11 | Unknown option → exit 3, `"Unknown option: --unknown"` | :106 |
| 12 | Issue number appears as `"Task: #<N>"` in header | :116 |
| 13 | Default agent is `"default"`; default status file is `"status-issue-<N>.json"` | :130,137 |

**Port value:** HIGH — generic CLI contract, no Hey Soo!-specific content.

---

#### A-02 `test-constants.bats` (11 cases)
**Unit under test:** Constants in `lib/orchestrator-common.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | `STAGE_TIMEOUT == 1800` (30 min) | :34 |
| 2 | `MAX_TASK_REVIEW_ATTEMPTS == 3` | :42 |
| 3 | `MAX_QUALITY_ITERATIONS == 5` | :47 |
| 4 | `MAX_PR_REVIEW_ITERATIONS == 3` | :52 |
| 5 | `RATE_LIMIT_BUFFER == 60` | :58 |
| 6 | `RATE_LIMIT_DEFAULT_WAIT == 3600` | :63 |
| 7 | `SCRIPT_DIR` is defined; `SCHEMA_DIR` ends in `/schemas` | :70,74 |
| 8 | Default `STATUS_FILE` is `"status.json"` (verified by source inspection) | :82 |
| 9 | `AGENT` defaults to `""` | :90 |
| 10 | Timeout/retry constants declared `readonly` in lib | :102 |
| 11 | Script uses `set -uo pipefail` and NOT `set -e` | :140,147 |

**Port value:** HIGH — all numeric caps are generic engine tunables.

---

#### A-03 `test-change-scope.bats` (5 cases)
**Unit under test:** `lib/detect-change-scope.sh` → `detect_change_scope()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Renamed test file → `scope=test_only`, `changed_tests` has new name | :39 |
| 2 | Deleted test file → `scope=test_only`, `changed_tests` empty | :55 |
| 3 | Renamed source file → `scope=source_only`, `changed_tests` empty | :70 |
| 4 | Invalid diff base → `scope=error`, non-empty `.message` | :85 |
| 5 | First commit (initial) uses empty-tree fallback | :100 |

Output: JSON with `.scope`, `.changed_tests[]`, `.message`.  
**Port value:** HIGH — pure git analysis, generic. Path patterns (`lambda/`, `frontend/`) are Hey Soo!-specific but the routing logic is generic.

---

#### A-04 `test-comment-helpers.bats` (18 cases)
**Unit under test:** `comment_issue()` and `comment_pr()` in orchestrator-common.sh

| # | Key invariant | File:line |
|---|---|---|
| 1 | `comment_issue` uses `gh issue comment` | :59 |
| 2 | `comment_issue` derives issue number from `TASK_ID` helper (not raw `REPO`) | :66,73 |
| 3 | `comment_issue` does NOT include attribution wrapper `"implement-issue-orchestrator"` | :80 |
| 4 | `comment_pr` uses `gh pr comment` | :98 |
| 5 | `comment_pr` takes `pr_num` as first arg | :105 |
| 6 | `comment_pr` uses `$REPO` variable | :113 |
| 7 | `comment_pr` includes attribution `"implement-issue-orchestrator"` | :120 |
| 8 | Both functions log to `$LOG_FILE` | :127,219 |
| 9 | Both format title as `## $title` | :238,246 |
| 10 | `gh` failure on `comment_issue` → exit 0 (non-fatal) | :174 |
| 11 | `gh` failure on `comment_pr` → exit 0 (non-fatal) | :189 |
| 12 | `main()` calls `comment_issue "Starting Automated Processing"` | :258 |
| 13 | `main()` calls `comment_issue "Evaluation..."` after evaluate stage | :265 |
| 14 | `main()` calls `comment_issue "Implementation Plan"` | :272 |
| 15 | `main()` calls `comment_issue "Task List"` | :279 |
| 16 | `main()` calls `comment_pr "$pr_number" "Post-Completion Notes"` | :286 |
| 17 | `run_quality_loop` calls `comment_issue` | :293 |

**Port value:** MEDIUM — structure is generic (issue/PR comment); exact comment titles are Hey Soo!-specific.  
**§8:** Attribution text (`"implement-issue-orchestrator"`) is Hey Soo!-specific and should be parameterised.

---

#### A-05 `test-api-contract-review.bats` (2 cases)
**Unit under test:** `evaluate_api_contract_review_scope()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Frontend (`frontend/src/lib/api/`, `frontend/src/types/`) + backend (`lambda/suggest/`) changes → `warranted=true`, service list includes `"suggest"` | :31 |
| 2 | Frontend UI components only → `warranted=false` | :59 |

Output JSON: `.warranted`, `.shared_services[]`, `.frontend_contract_files[]`, `.backend_contract_files[]`.  
**§8:** Path patterns (`lambda/`, `frontend/src/lib/api/`, `frontend/src/types/`) are Hey Soo!-specific. The detection shape is generic.

---

#### A-06 `test-build-incremental-test-prompt.bats` (5 cases)
**Unit under test:** `lib/build-incremental-test-prompt.sh` → `build_incremental_test_prompt()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Mixed unit+E2E → both `"## Unit Tests (pytest)"` and `"## E2E Tests (Playwright)"` sections | :18 |
| 2 | pytest-only → only unit section; no E2E section | :33 |
| 3 | playwright-only → only E2E section; command is `bash .claude/scripts/e2e-smoke.sh <files>` | :45 |
| 4 | Empty file list → no test sections, keeps summary text | :56 |
| 5 | Invalid JSON → no test sections, no crash | :68 |

Pytest command pattern: `uv run pytest <files> -v`.  
**§8:** `uv run pytest` and `bash .claude/scripts/e2e-smoke.sh` are Hey Soo!-specific toolchain references. The section-routing logic (split by file extension) is generic.

---

#### A-07 `test-e2e-policy.bats` (5 cases)
**Unit under test:** `evaluate_e2e_policy()` + `merge_e2e_policy_review_finding()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | `frontend/src/pages/` change without spec → `warranted=true`, `has_e2e_specs=false` | :49 |
| 2 | `lambda/` only change → `warranted=false` | :65 |
| 3 | API client + spec → `warranted=true`, `has_e2e_specs=true` | :80 |
| 4 | `merge_e2e_policy_review_finding`: warranted but no specs → overrides review to `changes_requested`, adds issue with `category=e2e_policy` | :98 |
| 5 | `merge_e2e_policy_review_finding`: warranted + specs present → leaves review approved, issues empty | :110 |

**§8:** `frontend/src/pages/`, `frontend/src/lib/api/`, `tests/e2e/` paths are Hey Soo!-specific. The policy enforcement pattern (warrant + merge) is generic.

---

#### A-08 `test-force-push-remediation.bats` (8 cases: 6 unit, 2 integration)
**Unit under test:** `detect_force_push_remediation()` in `lib/orchestrator-common.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Count decreased AND clean tree → returns 0 (true) | :56 |
| 2 | Count unchanged → returns 1 | :70 |
| 3 | Reset with dirty tree → returns 1 | :81 |
| 4 | Count increased → returns 1 | :95 |
| 5 | Empty worktree path → returns 1 | :107 |
| 6 | Nonexistent worktree → returns 1 | :113 |
| 7 | Integration: force-push → `review_attempts` net-zero | :127 |
| 8 | Integration: normal fix → `review_attempts` incremented by 1 | :154 |

**Port value:** HIGH — generic remediation counter logic. No Hey Soo! specificity.

---

#### A-09 `test-infra-failure.bats` (~45 cases)
**Unit under test:** `run_tests_direct()` and `reset_test_infrastructure()` in orchestrator-common.sh

Key invariants by group:

| Group | Invariant | File:line |
|---|---|---|
| Infra flag (E2E) | exit=1 with 0 failures → `e2e_infrastructure_failure=true`, `infrastructure_failure=true` | :87,99 |
| Infra flag (E2E) | exit=1 WITH failures parsed → `e2e_infrastructure_failure=false` | :121 |
| Infra flag (unit) | exit=1 with 0 failures → `unit_infrastructure_failure=true` | :152 |
| Combined | Both crash → both flags true | :190 |
| Crash reasons | `EADDRINUSE` → `"port_conflict"` (case-insensitive) | :242,411 |
| Crash reasons | `"browser has been closed"` / `"disconnected"` / `"crashed"` → `"browser_crash"` | :253,263,274 |
| Crash reasons | `"Timed out waiting"` / `"Timed out"` → `"server_timeout"` | :286,297 |
| Crash reasons | `"out of memory"` / `"OOM"` / `"ENOMEM"` → `"oom"` | :308,319,330 |
| Crash reasons | `"SIGTERM"` / `"SIGKILL"` / `"signal"` → `"killed"` | :341,352,363 |
| Crash reasons | Unrecognised → `"unknown"` | :374 |
| Crash reasons | No infra failure → `crash_reason=null` | :385 |
| JSON schema | Required fields: `infrastructure_failure`, `e2e_infrastructure_failure`, `unit_infrastructure_failure`, `crash_reason` | :511 |
| `reset_test_infrastructure` | Returns 0 for all crash reasons | :554 |
| `reset_test_infrastructure` | Cleans `tests/e2e/test-results/` on `browser_crash` | :581 |
| `reset_test_infrastructure` | Checks ports 5173–5179 via `lsof` for `port_conflict` | :598 |
| `reset_test_infrastructure` | Does NOT call `lsof` for non-port reasons | :621 |
| Constant | `MAX_CONSECUTIVE_INFRA_FAILURES == 3` | :78 |
| Structural | `orchestrator-common.sh` sources `reset-infra.sh` | :645 |
| Structural | Tracks `consecutive_infra_failures`; halts at `MAX_CONSECUTIVE_INFRA_FAILURES` | :655,660 |

**Port value:** HIGH — infra failure detection is entirely generic (exit code + output parsing).  
**§8:** Port numbers 5173–5179 (Vite dev server range) are Hey Soo!-specific.

---

#### A-10 `test-integration.bats` (~45 cases)
**Unit under test:** `main()` function structure + `run_quality_loop()` + `run_test_loop()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | All 10 stages present via `set_stage_started`: setup, research, evaluate, plan, implement, quality_loop, docs, pr, pr_review, complete | :51 |
| 2 | Setup stage uses `run_setup_stage` (no LLM schema) | :68 |
| 3 | Correct schemas for implement/PR/research/evaluate/plan | :76 |
| 4 | Plan stage extracts `implementation_budget` | :137 |
| 5 | Implementation loops through tasks (`task_count`, `for ((i=0;...`) | :163 |
| 6 | Implementation respects `MAX_TASK_REVIEW_ATTEMPTS` | :178 |
| 7 | Implementation uses `get_subtask_implementation_timeout` + `run_stage_with_timeout` | :185 |
| 8 | PR review uses `spec-reviewer` and `code-reviewer` agents | :205,212 |
| 9 | PR review respects `MAX_PR_REVIEW_ITERATIONS` | :219 |
| 10 | PR review enforces E2E policy gate | :234 |
| 11 | PR review includes API contract review gate | :243 |
| 12 | Completion: `set_final_state "completed"`, copies status to log dir, `exit 0` | :256,263,270 |
| 13 | Setup failure → `log_error`, `set_final_state "error"`, `exit 1` | :281 |
| 14 | `run_quality_loop` exits 2 on max iterations | :300 |
| 15 | Log dir structure: `$LOG_BASE/stages/`, `$LOG_BASE/context/` | :311 |
| 16 | After PR review fixes → `git push origin` | :331 |
| 17 | Agent: `python-backend-developer` for fixes; `code-simplifier` in quality loop; `phpdoc-writer` for docs | :343,350,365 |
| 18 | Task failure tracked: `status=failed`, `review_attempts` count | :376 |
| 19 | Failed task does not block subsequent tasks | :397 |
| 20 | PR review iteration counter increments via `increment_pr_review_iteration` | :440 |

**§8:** Agent names (`python-backend-developer`, `phpdoc-writer`, `code-simplifier`) and schema names are Hey Soo!-specific. All stage sequencing logic is generic engine contract.

---

#### A-11 `test-json-parsing.bats` (~55 cases)
**Unit under test:** JSON extraction safety in `run_stage()`, `detect_rate_limit()`, `extract_wait_time()`, `log()`, `log_error()`

Key invariants:

| Group | Invariant | File:line |
|---|---|---|
| echo bug | `echo "$var"` mangles JSON starting with `-n` or `-e` | :55,79 |
| Fix | `printf '%s' "$var"` is safe for all echo problem cases | :212 |
| Fix | here-strings are safe | :245 |
| log pollution | `log()` using `tee` to stdout pollutes `$()` captures | :864 |
| log fix | `log()` must write to file and stderr separately (no `tee` to stdout) | :896 |
| log_error | `log_error()` using `tee ... >&2` does NOT pollute stdout | :1043 |
| `run_stage` | Must return clean JSON (no log lines mixed in) | :260 |
| `detect_rate_limit` | Must not crash on malformed JSON | :309 |
| `extract_wait_time` | Must return default (3600) when no time found | :338 |
| Structural | `log()` must NOT pipe to `tee`; must `>>$LOG_FILE` and `>&2` | :1141 |

**Port value:** CRITICAL — entire file encodes the `printf '%s'` vs `echo` contract that the Python port MUST replicate in subprocess/output handling. The tee/pollution patterns become irrelevant in Python but the JSON-safety invariant is preserved.

---

#### A-12 `test-micro-mode.bats` (~45 cases)
**Unit under test:** `init_status()` with `EXECUTION_MODE=micro`, `skip_stage()`, `normalize_task_id()`, `parse_task_mode_overrides()` (from `ralph-loop.sh`)

| Group | Key invariant | File:line |
|---|---|---|
| `init_status micro` | Sets `execution_mode=micro` in JSON | :68 |
| `init_status micro` | Marks 6 stages as skipped: research, evaluate, plan, quality_loop, test_loop, docs | :76–122 |
| `init_status micro` | Keeps setup, implement, pr, pr_review, complete as pending | :124–140 |
| `init_status micro` | `stages_skipped` count == 6; `stages_executed` count == 5 | :148,163 |
| micro vs lite | micro skips `test_loop`; lite does NOT | :180 |
| `skip_stage micro` | Skips: research, evaluate, plan, quality_loop, test_loop, docs (exit 0) | :201–229 |
| `skip_stage micro` | Does NOT skip: implement, setup, pr, complete, pr_review (exit 1) | :237–261 |
| `skip_stage full` | Skips nothing | :267 |
| `normalize_task_id` | Strips `:micro`, `:lite`, `:full` suffixes | :287–313 |
| `normalize_task_id` | Roadmap-style `5.1.2:micro` → `5.1.2` (no `#` prefix) | :329 |
| `parse_task_mode_overrides` | Parses comma-separated `ID:mode` list into `TASK_MODE_OVERRIDES` | :340–408 |

**Port value:** HIGH — execution mode / stage-skip matrix is a generic engine contract.  
**§8:** The specific 6-stage skip list for micro mode is Hey Soo!-specific (docs stage, research stage, etc.).

---

#### A-13 `test-quality-loop.bats` (~20 cases)
**Unit under test:** `run_quality_loop()` in orchestrator-common.sh

| # | Key invariant | File:line |
|---|---|---|
| 1 | `MAX_QUALITY_ITERATIONS == 5` | :45 |
| 2 | Approved on first iteration → `quality_iterations=1` | :54 |
| 3 | `review_verdict`, `approved`, `changes_requested` present in function | :110 |
| 4 | Max iterations → `set_final_state "max_iterations_quality"`, `exit 2` | :125 |
| 5 | Stage prefix used for log names: `simplify-${stage_prefix}`, `review-${stage_prefix}` | :143 |
| 6 | Stage sequence: simplify comes before review | :156 |
| 7 | Schema used: `implement-issue-simplify.json` | :182 |
| 8 | Returns 0 on approval | :174 |
| 9 | Retry behavior: `changes_requested` → second iteration → approved | :202 |
| 10 | Skip simplify after a fix iteration (issue #43) | :317 |
| 11 | Auto-approve when fix produces no new commit (no-op fix) | :389 |
| 12 | Per-iteration timing tracked (`iter_start_seconds`, `iter_elapsed`) | :443 |

**Port value:** HIGH — entire quality loop contract is generic (iterate simplify → review → fix cycle).

---

#### A-14 `test-rate-limit.bats` (17 cases)
**Unit under test:** `detect_rate_limit()`, `extract_wait_time()`, `handle_rate_limit()` in orchestrator-common.sh

| # | Key invariant | File:line |
|---|---|---|
| 1 | `structured_output.status=rate_limit` → detected (exit 0) | :34 |
| 2 | Text `"rate limit"` in result → detected | :50 |
| 3 | `"429"` → detected | :56 |
| 4 | `"too many requests"` → detected | :62 |
| 5 | `"quota exceeded"` → detected | :68 |
| 6 | Normal error text → not detected (exit 1) | :74 |
| 7 | Detection is case-insensitive | :87 |
| 8 | `Retry-After: 300` → `extract_wait_time` returns 300 | :96 |
| 9 | `"wait 15 minutes"` → returns 900 | :102 |
| 10 | `"Wait 30 min"` → returns 1800 | :108 |
| 11 | No time found → returns `RATE_LIMIT_DEFAULT_WAIT=3600` | :114 |
| 12 | Retry-After takes precedence over minutes pattern | :120 |
| 13 | `RATE_LIMIT_BUFFER == 60` | :130 |
| 14 | `handle_rate_limit` logs `"Rate limit hit"` | :144 |
| 15 | `run_stage` contains `detect_rate_limit` and `handle_rate_limit` | :161,169 |
| 16 | `run_stage` has retry path after rate limit | :177 |

**Port value:** HIGH — entirely generic provider API rate-limit contract.

---

#### A-15 `test-regression-helpers.bats` (7 cases)
**Unit under test:** `lib/regression-helpers.sh` — `compute_regressions()`, `get_inherited_failures()`, `capture_baseline_failures()`, `update_test_loop_metadata()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | `compute_regressions`: excludes failures matching baseline, keeps new | :65 |
| 2 | `get_inherited_failures`: returns only failures present in baseline | :78 |
| 3 | `compute_regressions`: missing baseline file → all failures are regressions | :91 |
| 4 | `get_inherited_failures`: invalid `capture_status` (not `"success"`) → empty list | :100 |
| 5 | `capture_baseline_failures`: writes parsed pytest + playwright failures to JSON | :112 |
| 6 | `capture_baseline_failures`: unresolved base branch → writes `capture_status=error` | :144 |
| 7 | `update_test_loop_metadata`: concurrent writers never produce truncated JSON | :164,194 |

**Port value:** HIGH — the baseline/regression diff contract is entirely generic.  
**§8:** Failure name patterns (`lambda/suggest/tests/`, `react-chat-panel.spec.ts:87`) embed Hey Soo! project layout.

---

#### A-16 `test-select-incremental-test-agent.bats` (5 cases)
**Unit under test:** `lib/select-incremental-test-agent.sh` → `select_incremental_test_agent()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | E2E-only files → agent=`bulletproof-frontend-developer`, family=`e2e_only` | :18 |
| 2 | pytest-only → agent=`python-backend-developer`, family=`python_only` | :27 |
| 3 | Mixed → agent=`python-backend-developer`, family=`mixed` | :36 |
| 4 | Shell-only (.bats) → agent=`python-backend-developer`, family=`shell_only` | :45 |
| 5 | Invalid JSON → agent=`python-backend-developer`, family=`default` (safe default) | :54 |

**§8:** Agent names are Hey Soo!-specific. The routing shape (classify files → select agent) is generic.

---

#### A-17 `test-setup-stage-cache.bats` (3 cases)
**Unit under test:** `install_frontend_dependencies()`, `install_python_dependencies()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | npm install skipped on 2nd call if `package-lock.json` unchanged (hash stored in `.worktree-dep-hash-npm`) | :24 |
| 2 | `uv sync` skipped if `uv.lock` unchanged; re-runs on lock change (per-project) | :45 |
| 3 | Root-level `uv sync` cached by root `uv.lock` hash | :74 |

**§8:** `npm`/`uv` tools and `package-lock.json`/`uv.lock` filenames are Hey Soo!-specific. The hash-based caching contract is generic.

---

#### A-18 `test-stage-runner.bats` (10 cases)
**Unit under test:** `run_stage()`, `next_stage_log()`, `log()`, `log_error()`, `get_subtask_implementation_timeout()`, `run_stage_with_timeout()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Missing schema → exit 1, `"schema not found"` | :52 |
| 2 | Valid schema → creates stage log file | :58 |
| 3 | `next_stage_log` formats counter as `%02d-<name>.log` | :78 |
| 4 | Single-digit counter padded | :95 |
| 5 | `log()` writes to `$LOG_FILE` with ISO 8601 timestamp | :113,119 |
| 6 | `log_error()` writes `"ERROR: <msg>"` to log | :124 |
| 7 | `run_stage` extracts `structured_output` as return value | :133 |
| 8 | Missing `structured_output` → exit 1, `"no structured output"` | :150 |
| 9 | Timeout (exit 124) → exit 1, `"timeout"` message | :165 |
| 10 | `--agent <name>` passed through to `claude` CLI | :208 |
| 11 | `get_subtask_implementation_timeout short` → `SUBTASK_STAGE_TIMEOUT_SHORT` | :180 |
| 12 | `run_stage_with_timeout` overrides and restores `STAGE_TIMEOUT` | :190 |

**Port value:** HIGH — `run_stage` is the core invocation primitive. All contracts must be preserved.

---

#### A-19 `test-status-functions.bats` (28 cases)
**Unit under test:** `init_status()`, `update_stage()`, `set_stage_started/completed()`, `set_tasks()`, `update_task()`, `set_worktree_info()`, `set_final_state()`, `increment_quality_iteration()`, `increment_pr_review_iteration()`

Key invariants (all from `lib/orchestrator-common.sh`):

| Function | Invariant | File:line |
|---|---|---|
| `init_status` | Creates JSON with `state=initializing`, issue as `"#N"`, all stages `"pending"`, empty tasks array | :34,39,44,53,90 |
| `init_status` | Sets `log_dir` field | :83 |
| `update_stage` | Updates `.stages.<name>.status` and `.current_stage` | :101,119 |
| `update_stage` | Supports arbitrary extra field (key/value) | :111 |
| `update_stage` | Updates `.last_update` timestamp | :128 |
| `update_stage` | Concurrent writers never produce invalid JSON | :141 |
| `set_stage_started` | Sets `status=in_progress`, `started_at`, `state=running` | :178,187,196 |
| `set_stage_completed` | Sets `status=completed`, `completed_at` | :205,214 |
| `set_tasks` | Populates tasks array; sets `stages.implement.task_progress="0/N"` | :227,237 |
| `update_task` | Updates task status by id, sets `review_attempts`, sets `current_task` | :251,263,275 |
| `set_worktree_info` | Sets `.worktree` and `.branch` | :291,300 |
| `set_final_state` | Sets `.state` to any terminal state | :313 |
| `increment_quality_iteration` | Increments `.quality_iterations` and `.stages.quality_loop.iteration` | :344,354 |
| `increment_pr_review_iteration` | Increments `.pr_review_iterations` and `.stages.pr_review.iteration` | :365,374 |

**Port value:** CRITICAL — entire status-file API is the central persistence contract.

---

#### A-20 `test-track-test-commit.bats` (4 cases)
**Unit under test:** `lib/track-test-commit.sh` — `claim_worktree_ownership()`, `record_last_tested_commit()`, `get_last_tested_commit()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Second live orchestrator claiming same worktree → exit 1, `"Worktree appears shared"` | :30 |
| 2 | Owned worktree → `record_last_tested_commit` writes SHA; `get_last_tested_commit` returns it | :40 |
| 3 | Unowned worktree → `record_last_tested_commit` blocks, no file written | :48 |
| 4 | Missing ownership marker → `get_last_tested_commit` exits 1, `"No worktree ownership marker found"` | :60 |

**Port value:** HIGH — worktree exclusion contract is generic engine behavior.

---

### Zone B — `.claude/scripts/tests/*.sh` (6 files, raw bash)

---

#### B-01 `test-e2e-override.sh` (6 cases)
**Unit under test:** E2E detection pattern `'^frontend/|\.spec\.ts$'` (mirrors `orchestrator-common.sh`)

| # | Key invariant | File:line |
|---|---|---|
| 1 | `lambda/` only → skip E2E (`false`) | :117 |
| 2 | `frontend/` changes → run E2E (`true`) | :130 |
| 3 | `.spec.ts` changes → run E2E (`true`) | :143 |
| 4 | Mixed `lambda/ + frontend/` → run E2E (`true`) | :155 |
| 5 | `lambda/ + infra/ + docs/` only → skip E2E (`false`) | :169 |
| 6 | Empty branch (no new commits) → skip E2E (`false`) | :183 |

**§8:** The pattern `'^frontend/|\.spec\.ts$'` is Hey Soo!-specific but the contract (grep diff names) is generic.

---

#### B-02 `test-e2e-routing.sh` (4 cases)
**Unit under test:** `e2e-smoke.sh` spec routing (Suite 1 vs Suite 2 via `playwright-react.config.ts`)

| # | Key invariant | File:line |
|---|---|---|
| 1 | Registered Suite 2 spec routes to Suite 2 with `"Suite 1 specs: <none>"` | :67 |
| 2 | Registered Suite 2 spec emits no `WARNING:` | :71 |
| 3 | Unregistered spec that looks like Suite 2 stays in Suite 1 | :93 |
| 4 | Unregistered Suite 2-like spec emits `WARNING: ... playwright-react.config.ts` | :94 |

**§8:** Entirely Hey Soo!-specific (Playwright suite routing). Low port value.

---

#### B-03 `test-port-registry.sh` (4 cases)
**Unit under test:** `lib/port-registry.sh` — `port_registry_cleanup_stale_entries_unlocked()`, `port_registry_claim_port()`, `port_registry_release_claim()`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Stale entries (dead PID) removed; live entries kept | :63 |
| 2 | First claim gets preferred port (5173); second gets next (5174) | :84 |
| 3 | Released port becomes available again | :98 |
| 4 | Registry JSON records all active claimants | :104 |

**§8:** Default port 5173 (Vite dev server) is Hey Soo!-specific. The registry shape (PID-based eviction, claim/release) is generic.

---

#### B-04 `test_codex_prompt_schema.sh` (4 cases)
**Unit under test:** `render_stage_prompt()` in `lib/orchestrator-common.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Codex provider strips `"Use the Skill tool"` directive | :57 |
| 2 | Codex provider inlines schema body with `"Required JSON schema (<file>):"` | :58 |
| 3 | Codex prompt demands `"Use the exact required property names"` | :61 |
| 4 | Claude provider keeps `"Use the Skill tool"` directive; no schema inlining | :67 |

**Port value:** HIGH — prompt adaptation is a core multi-provider contract.

---

#### B-05 `test_cost_summary_provider.sh` (4 cases)
**Unit under test:** `emit_cost_summary()` in `lib/orchestrator-common.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | `provider=codex` runs → label `"**Codex invocations:** N"`, NOT `"Claude invocations"` | :74 |
| 2 | Mixed providers → label `"**Mixed-provider invocations:** N"` | :83 |

Input: `$LOG_BASE/context/stage-costs.jsonl` (JSONL, one record per stage invocation).  
Output: `$LOG_BASE/cost-summary.md`.  
**Port value:** HIGH — cost accounting by provider is a generic multi-provider contract.

---

#### B-06 `test_orchestrator_cleanup.sh` (~40 cases)
**Unit under test:** `orchestrator_cleanup()`, `register_orchestrator_traps()`, `set_normal_exit()`, `monitor-orchestrator.sh --cleanup`

| Group | Key invariant | File:line |
|---|---|---|
| Status update | Signal-caught → `.state=killed` | :185 |
| Status update | Normal exit → status unchanged | :197 |
| Status update | Error exit without signal → status unchanged | :209 |
| Re-entrant guard | Second cleanup call is no-op | :228 |
| Log preservation | Cleanup NEVER removes log directory or files | :265 |
| Log preservation | Status synced to `$LOG_BASE/status.json` | :275 |
| Child PID | Reads PID file, mentions `"tracked child process"` | :307 |
| `set_normal_exit` | Sets `_ORCHESTRATOR_NORMAL_EXIT=1` | :343 |
| Traps | `TERM`, `INT`, `HUP`, `EXIT` all registered | :364 |
| Signal handler | Sets `_ORCHESTRATOR_SIGNAL_CAUGHT=1`, calls `orchestrator_cleanup` | :379 |
| `monitor --cleanup` | Finds stale (running) status files | :399 |
| `monitor --cleanup` | `--dry-run` does NOT modify files | :412 |
| `monitor --cleanup` | Sets `.state=killed`, `.killed_by="monitor --cleanup"` | :426 |
| `monitor --cleanup` | Skips `completed` and already-`killed` status files | :459,475 |
| `ralph_cleanup` | Marks running/retrying tasks as `killed`; completed unchanged | :568 |
| Atomic writes | No `.tmp` file left behind; result is valid JSON | :532 |
| Timestamp | `last_update` updated during cleanup | :548 |

**Port value:** HIGH — cleanup/trap contract is pure generic engine behavior.

---

### Zone C — `tests/shell/**/*.bats` (8 files, BATS)

---

#### C-01 `tests/shell/lib/test-orchestrator-provider.bats` (24 cases)
**Unit under test:** `get_orchestrator_provider()`, `should_use_codex()`, `run_provider_oneshot()`, `run_codex_stage()` in `lib/orchestrator-common.sh`

| Group | Key invariant | File:line |
|---|---|---|
| `get_orchestrator_provider` | Unset/empty → `"claude"` | :108,117 |
| `get_orchestrator_provider` | `"claude"` / `"codex"` accepted | :126,135 |
| `get_orchestrator_provider` | Invalid (`"gpt"`, `"Claude"`) → non-zero exit, error logged | :144,157 |
| `should_use_codex` | Default provider → false for all stages | :169 |
| `should_use_codex` | `ORCHESTRATOR_PROVIDER=codex` → true even for analytical stages | :180 |
| `should_use_codex` | Invalid global provider → false | :206 |
| `should_use_codex` | `TASK_PROVIDER=codex` → only eligible file-patching stages (implement, fix) | :221,236 |
| `should_use_codex` | `TASK_PROVIDER=claude` → never routes to codex | :253 |
| `should_use_codex` | Global codex overrides per-task claude | :264 |
| `run_provider_oneshot` | Default → invokes `claude` CLI with `--json-schema <path>` | :278 |
| `run_provider_oneshot` | `ORCHESTRATOR_PROVIDER=codex` → invokes `codex` CLI with `--output-last-message` | :297 |
| `run_provider_oneshot` | Codex last-message JSON surfaced to caller | :316 |
| `run_provider_oneshot` | Non-zero CLI exit propagated | :334 |
| `run_provider_oneshot` | Invalid provider → rc=2, no CLI invoked | :351 |
| `run_provider_oneshot` | Missing CLI → rc=127 | :368 |
| `run_provider_oneshot` | Timeout arg kills hung claude with exit 124 | :683 |
| `run_codex_stage` | Required keys present → success | :396 |
| `run_codex_stage` | Missing required key → error | :423 |
| `run_codex_stage` | Git HEAD moved (patch committed) → success | :445 |
| `run_codex_stage` | Invalid JSON in last-message → error | :496 |
| `run_codex_stage` | Missing schema file → error | :533 |
| `run_codex_stage` | Records `provider=codex` in stage-costs.jsonl | :418 |
| Ralph dispatch | `ORCHESTRATOR_PROVIDER=codex` routes `run_provider_oneshot` to `codex` CLI | :558 |
| Ralph capacity | `ORCHESTRATOR_PROVIDER=codex` disables capacity probe | :591 |

**Port value:** CRITICAL — multi-provider dispatch contract is the highest-priority generic engine contract.

---

#### C-02 `tests/shell/lib/test-render-stage-prompt.bats` (14 cases)
**Unit under test:** `render_stage_prompt()` in `lib/orchestrator-common.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | `claude` provider → byte-identical pass-through | :51 |
| 2 | Unknown provider → treated as claude (pass-through) | :75 |
| 3 | `codex` provider → appends `"Emit exactly one JSON object on stdout..."` postamble | :90 |
| 4 | Postamble comes after original content | :101 |
| 5 | `codex` strips lines referencing `"Skill"` | :118 |
| 6 | `codex` strips lines referencing `"TodoWrite"` | :133 |
| 7 | `codex` strips lines referencing `"Agent"` (capitalised) | :148 |
| 8 | `codex` strips lines referencing `"run_in_background"` | :163 |
| 9 | All four directive types stripped in one prompt | :178 |
| 10 | `"skill level"` (lowercase, not a tool ref) survives; `"agent configuration"` survives | :196 |
| 11 | Empty prompt + claude → empty output | :219 |
| 12 | Empty prompt + codex → only postamble | :229 |
| 13 | Re-entry (render twice) → non-exploding | :243 |

**Port value:** CRITICAL — prompt rendering contract directly shapes what is sent to the LLM.

---

#### C-03 `tests/shell/lib/test-ralph-resume-decision.bats` (9 cases)
**Unit under test:** `compute_resume_decision()` in `ralph-loop.sh`

| # | Key invariant | File:line |
|---|---|---|
| 1 | No status file → `RESUME_ARG=""`, reason `"no prior status file"` | :44 |
| 2 | `state=error` → `RESUME_ARG="--resume"` (the regression case) | :51 |
| 3 | `state=killed` → `--resume` | :59 |
| 4 | `state=running` (stale) → `--resume` | :67 |
| 5 | `state=completed` → no resume | :75 |
| 6 | `state=already_closed` / `no_changes` → no resume | :83,91 |
| 7 | Empty file → no resume | :101 |
| 8 | Missing `.state` field → no resume | :107 |
| 9 | `$()` wrapping traps mutation — MUST call directly, not in subshell | :115 |

**Port value:** HIGH — resume-from-status logic is generic.  
**Anomaly:** Case 9 is a test that documents a known anti-pattern: wrapping the function in `$()` silently breaks it. This is specific to bash; Python does not have this hazard.

---

#### C-04 `tests/shell/lib/test-detect-change-scope.bats` (2 cases)
**Unit under test:** `detect_change_scope()` — BATS file routing

| # | Key invariant | File:line |
|---|---|---|
| 1 | Shell lib changed → `shell_lib_changed=true`; mirrored bats test enqueued in `changed_tests` | :38 |
| 2 | `.bats` files classified as `scope=test_only` | :54 |

**Port value:** MEDIUM — bats-specific routing rule; maps to a generic "enqueue matching test for changed source" contract.

---

#### C-05 `tests/shell/lib/test-run-tests-direct.bats` (2 cases)
**Unit under test:** `lib/run-tests-direct.sh` → `run_tests_direct()` shell-suite integration

| # | Key invariant | File:line |
|---|---|---|
| 1 | `--files '[".bats"]'` → runs through `test-shell.sh`; returns `result=passed`, shell summary | :55 |
| 2 | `--shell` flag → includes shell suite in combined run | :69 |

**Port value:** MEDIUM — shell-suite integration is Hey Soo!-specific (bats). Generic contract: `run_tests_direct` supports selectable test-suite flags.

---

#### C-06 `tests/shell/test-check-e2e-selectors.bats` (2 cases)
**Unit under test:** `scripts/check-e2e-selectors.mjs` (Node.js script)

| # | Key invariant | File:line |
|---|---|---|
| 1 | Existing `data-testid` and CSS class → exits 0, reports verified count | :27 |
| 2 | Missing `data-testid` or CSS class → exits 1, reports with spec file:line | :61 |

**§8:** Entirely Hey Soo!-specific (React/Playwright selector validation). Low port value for generic engine.

---

#### C-07 `tests/shell/test-check-testid-stability.bats` (2 cases)
**Unit under test:** `scripts/check-testid-stability.mjs`

| # | Key invariant | File:line |
|---|---|---|
| 1 | Mutable `data-testid` + stable companion `data-*` attr → exits 0 | :27 |
| 2 | Mutable `data-testid` with no stable companion → exits 1, reports file:line | :46 |

**§8:** Hey Soo!-specific frontend tooling. Zero port value.

---

#### C-08 `tests/shell/test-deploy-skip-build.bats` (3 cases)
**Unit under test:** `.claude/scripts/deploy.sh` frontend build skip logic

| # | Key invariant | File:line |
|---|---|---|
| 1 | Infra-only HEAD change → skips `npm run build`; runs CDK deploy | :48 |
| 2 | Non-infra change → includes `npm run build` | :61 |
| 3 | `--skip-build` flag: stripped before CDK call, frontend build skipped | :72 |

**§8:** Entirely Hey Soo!-specific (CDK, npm, infra/ pattern). Zero port value for generic engine.

---

### Zone D — `.claude/scripts/test-*.sh` (7 loose scripts, raw bash)

---

#### D-01 `test-capacity-jitter.sh` (15 cases)
**Unit under test:** `calculate_capacity_sleep()`, `capacity_wait_loop()` (inlined stubs)

| # | Key invariant | File:line |
|---|---|---|
| 1 | Pre-stage cap at 3600s | :85 |
| 2 | Post-timeout cap at 1800s | :91 |
| 3 | Floor: minimum 60s + jitter | :101 |
| 4 | Jitter produces variance (≥3 unique values in 10 calls) | :109 |
| 5 | Log message includes context label and jitter info | :128 |
| 6 | No 7200s cap references remain in `orchestrator-common.sh` | :144 |
| 7 | Early exit on capacity recovery | :229 |
| 8 | Full wait when capacity stays exhausted | :256 |
| 9 | First chunk capped at poll_interval (900s) | :278 |
| 10 | Short planned total (<poll_interval) → single chunk | :286 |
| 11 | `CAPACITY_SLEEP_SECS` reflects actual sleep on early exit | :294 |
| 12 | Last chunk is remainder, not full poll_interval | :308 |
| 13 | No bare `sleep "$CAPACITY_SLEEP_SECS"` in common.sh | :322 |
| 14 | Loop log includes context label | :329 |
| 15 | ≥4 `capacity_wait_loop` references in common.sh | :343 |

**Port value:** HIGH — capacity-management polling loop is a generic engine contract. Numeric values (1800, 3600, 900) are tunables.

---

#### D-02 `test-ci.sh`
**Role:** CI entrypoint — runs `test-unit.sh` then `test-shell.sh --tap`.  
**Port value:** LOW for contract mapping; HIGH for CI pipeline integration.

---

#### D-03 `test-counter-final.sh` (10+ cases)
**Unit under test:** Stage counter persistence and resume logic (inlined)

| # | Key invariant | File:line |
|---|---|---|
| 1 | Fresh run: stages numbered `01-N` sequentially | :66 |
| 2 | Resume from `status.json` (`stage_counter` field) continues sequence | :82 |
| 3 | Resume by counting existing `.log` files (fallback) continues sequence | :115 |

**Port value:** HIGH — stage counter resume logic is generic.

---

#### D-04 `test-counter-simulation.sh` (similar to D-03)
Same invariants as D-03 but more detailed scenario walkthrough.  
Verifies exact filenames (`01-extract.log` → `08-docs.log`). **Port value:** MEDIUM (verification detail).

---

#### D-05 `test-shell.sh`
**Role:** Test runner — discovers and runs all `*.bats` under `tests/shell/`.  
**Contract:** Accepts `--tap`, `--verbose`, or specific `.bats` file args.  
**Port value:** INFRASTRUCTURE — not a behavioral contract.

---

#### D-06 `test-stage-counter.sh` (5 cases)
**Unit under test:** `next_stage_log` counter mechanics (inlined)

| # | Key invariant | File:line |
|---|---|---|
| 1 | Fresh start → counter 0 | :19 |
| 2 | Resume with 5 existing logs → counter 5 | :30 |
| 3 | Counter increments: `01-setup.log`, `02-research.log`, `03-evaluate.log` | :53 |
| 4 | `jq '.stage_counter'` persists and restores | :80 |
| 5 | Missing `stage_counter` → falls back to 0 via `// 0` | :96 |

**Port value:** HIGH — stage counter/resume is a generic engine contract.

---

#### D-07 `test-unit.sh`
**Role:** Run Python unit suites (`lambda/suggest`, `lambda/library`, `infra`) via `uv run pytest`.  
**Contract:** `--skip-infra` flag skips infra suite; exit code = combined suite result.  
**Port value:** INFRASTRUCTURE for Python pytest setup; the runner itself is Hey Soo!-specific.

---

## 3. Fixtures (`.claude/scripts/implement-issue-test/fixtures/`)

12 fixture files — SKIMMED, not deep-mapped:

| File | Represents |
|---|---|
| `implement-success.json` | Successful implement stage structured_output |
| `pr-success.json` | Successful PR stage output |
| `rate-limit.json` | Rate-limit error response from claude CLI |
| `review-approved.json` | Review stage → approved |
| `review-changes-requested.json` | Review stage → changes_requested |
| `setup-error.json` | Setup stage error |
| `setup-success.json` | Successful setup output (worktree, branch, tasks) |
| `task-review-improvements.json` | Task review with required improvements |
| `task-review-no-commit-needed.json` | Task review: no commit needed |
| `task-review-passed.json` | Task review passed |
| `test-failed.json` | Test run failure result |
| `test-passed.json` | Test run passed result |

All fixtures encode the `structured_output` JSON schema contract. They are the ground truth for schema field names and values.

---

## 4. Summary Statistics

| Zone | Files | ~Cases |
|---|---|---|
| A — implement-issue-test/*.bats | 20 | **391** (`@test` count) |
| B — scripts/tests/*.sh | 6 | ~65 |
| C — tests/shell/**/*.bats | 8 | **72** (`@test` count) |
| D — loose test-*.sh | 7 | ~50 |
| **Total** | **41** | **~550+** (bats `@test` alone = 463; +B/D raw-bash cases) |

---

## 5. Port Value Classification

### Highest port value — generic engine contracts

1. **A-19 `test-status-functions.bats`** — Status-file API (`init_status`, `update_stage`, `set_tasks`, etc.)
2. **C-01 `test-orchestrator-provider.bats`** — Multi-provider dispatch (`get_orchestrator_provider`, `should_use_codex`, `run_provider_oneshot`)
3. **C-02 `test-render-stage-prompt.bats`** — Prompt rendering contract
4. **A-18 `test-stage-runner.bats`** — `run_stage()` invocation contract
5. **B-06 `test_orchestrator_cleanup.sh`** — Cleanup/trap/signal handling
6. **A-14 `test-rate-limit.bats`** — Rate-limit detect/handle/retry
7. **A-09 `test-infra-failure.bats`** — Infra failure detection + reset
8. **A-13 `test-quality-loop.bats`** — Quality loop iteration contract
9. **A-02 `test-constants.bats`** — All numeric caps/timeouts
10. **A-01 `test-argument-parsing.bats`** — CLI argument contract
11. **A-12 `test-micro-mode.bats`** — Execution mode / stage-skip matrix
12. **D-01 `test-capacity-jitter.sh`** — Capacity polling loop
13. **A-08 `test-force-push-remediation.bats`** — Force-push counter logic
14. **A-20 `test-track-test-commit.bats`** — Worktree ownership exclusion
15. **B-04 `test_codex_prompt_schema.sh`** — Codex prompt adaptation
16. **B-05 `test_cost_summary_provider.sh`** — Cost accounting by provider
17. **A-15 `test-regression-helpers.bats`** — Baseline/regression diff

### Medium port value — generic shape, Hey Soo!-specific paths

- A-03 `test-change-scope.bats` (path patterns)
- A-06 `test-build-incremental-test-prompt.bats` (toolchain refs)
- A-07 `test-e2e-policy.bats` (path patterns)
- A-11 `test-json-parsing.bats` (becomes `subprocess`/string safety in Python)
- C-03 `test-ralph-resume-decision.bats`

### Low port value — Hey Soo!-specific only

- B-02 `test-e2e-routing.sh` (Playwright suite routing)
- C-06 `test-check-e2e-selectors.bats`
- C-07 `test-check-testid-stability.bats`
- C-08 `test-deploy-skip-build.bats`
- D-07 `test-unit.sh` (runner only)

---

## 6. Anomalies

1. **`test-json-parsing.bats` MIXES documentation-only and enforcing tests** (verifier-corrected). Only the explicitly `# DOCUMENTATION-ONLY` cases always-pass (`ECHO_BUG` `:94`,`:112`; `CODECHECK` `:781`,`:799`,`:815` end in bare `true`). Others with the same prefixes are **real behavioral assertions** that call `fail` — e.g. `ECHO_BUG` `:55`/`:79` and `CODECHECK` `:925` (guards against `log` using `tee`) and `:1116`. Do NOT blanket-skip prefixed tests in the pytest port; port the enforcing ones.

2. **`test-json-parsing.bats` log-tee tests duplicate `test-stage-runner.bats`** — both test the `log()` function's stdout behavior. Port as a single test.

3. **`test-counter-final.sh` and `test-counter-simulation.sh`** are near-identical. They test the same counter-resume contract at different granularities. Should be merged into one pytest parameterised test.

4. **`test-e2e-routing.sh`** (B-02) reads from the live `tests/e2e/` directory of the real repo (line `:21`), making it tightly coupled to the live filesystem. It cannot be ported as-is.

5. **`test-ralph-resume-decision.bats` case 9** documents a bash subshell mutation trap that has no Python equivalent. Note it, but do not port the anti-pattern test.

6. **`test-select-incremental-test-agent.bats`** asserts agent name `"bulletproof-frontend-developer"` — a project-specific agent persona. The routing logic (E2E → frontend agent, pytest → backend agent) is generic but names must be parameterised.

7. **`test-api-contract-review.bats`** only has 2 test cases for what appears to be a significant feature (cross-service API boundary detection). Likely undertested.

8. **Stage schemas referenced in tests** (`implement-issue-research.json`, `implement-issue-evaluate.json`, etc.) are enumerated in `test-integration.bats:31-37` but never deeply tested. The schema contracts live in `schemas/` not in these tests.

---

## 7. §8 Generic vs Hey Soo!-Specific Classification

| Component | Generic (port as-is) | Hey Soo!-specific (parameterise or skip) |
|---|---|---|
| CLI arg parsing | All flags and exit codes | Status file default name `status-issue-<N>.json` |
| Status-file schema | All field names, state machine | — |
| Stage sequence | Stage names: setup/research/evaluate/plan/implement/quality_loop/docs/pr/pr_review/complete | — |
| Numeric constants | `MAX_TASK_REVIEW_ATTEMPTS=3`, `MAX_QUALITY_ITERATIONS=5`, `MAX_PR_REVIEW_ITERATIONS=3`, `RATE_LIMIT_DEFAULT_WAIT=3600`, etc. | — |
| Provider dispatch | `get_orchestrator_provider`, `should_use_codex`, `run_provider_oneshot` | — |
| Agent names | — | `python-backend-developer`, `phpdoc-writer`, `code-simplifier`, `bulletproof-frontend-developer`, `spec-reviewer`, `code-reviewer` |
| Path patterns | — | `lambda/`, `frontend/`, `tests/e2e/`, `infra/`, `frontend/src/lib/api/` |
| Toolchain | — | `uv run pytest`, `npm`, `bash .claude/scripts/e2e-smoke.sh`, Playwright, CDK |
| Comment content | Issue/PR comment structure (gh, title, body) | Comment titles: `"Starting Automated Processing"`, `"Post-Completion Notes"`, etc. |
| Port numbers | Port registry shape (claim/release) | Default port 5173 (Vite) |
| Rate limit detection | Text patterns, HTTP 429, Retry-After | — |
| Infra failure | Exit code + output parsing logic | Port range 5173–5179 (Vite) |
| Cleanup/traps | TERM/INT/HUP/EXIT; re-entrant guard; log preservation | — |
| Cost summary | Provider label logic (codex vs claude vs mixed) | JSONL field names if project-specific |
| Prompt rendering | Codex postamble, directive stripping (Skill/TodoWrite/Agent/run_in_background) | — |

---

## DEFERRED.md Seed

```
# DEFERRED — Test Corpus Port Notes (Phase 3a)

Port test CASES (not bash) → pytest fixtures, Phase 3a.

Priority order (highest → lowest generic contract value):
1. Status-file API (A-19)
2. Multi-provider dispatch (C-01)
3. Prompt rendering (C-02, B-04)
4. run_stage invocation (A-18)
5. Cleanup/trap contract (B-06)
6. Rate-limit (A-14)
7. Infra failure detection (A-09)
8. Quality loop (A-13)
9. Constants (A-02)
10. CLI args (A-01)
11. Execution modes/skip matrix (A-12)
12. Capacity polling (D-01)
13. Force-push remediation (A-08)
14. Worktree ownership (A-20)
15. Cost summary (B-05)
16. Regression helpers (A-15)
17. Stage counter resume (D-03, D-06)

Do NOT port:
- B-02 test-e2e-routing.sh (live filesystem dependency)
- C-06 check-e2e-selectors (Node.js tooling, Hey Soo!-only)
- C-07 check-testid-stability (Hey Soo!-only)
- C-08 deploy-skip-build (CDK/npm, Hey Soo!-only)
- D-07 test-unit.sh (Python pytest runner)

Parameterise (don't hard-code):
- Agent names (python-backend-developer, etc.)
- Path patterns (lambda/, frontend/, tests/e2e/)
- Port 5173 (Vite default)
- Comment titles ("Starting Automated Processing", etc.)
- Stage names that are project-specific (docs → phpdoc-writer)

Known bash-only anti-patterns (no Python equivalent):
- C-03 case 9: $() subshell mutation trap for RESUME_ARG
- A-11: echo/-n/-e flag mangling (use printf/subprocess.run in Python)
- A-11: tee stdout pollution (Python subprocess capture is clean)
```
