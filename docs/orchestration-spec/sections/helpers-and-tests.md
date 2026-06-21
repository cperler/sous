# Section: LIB HELPERS + TEST CORPUS
Sources: fragments/06-lib-helpers-a.md · fragments/07-lib-helpers-b.md · fragments/14-test-corpus.md
Read: 2026-06-20

---

## 1. Status-File Primitive + Locking

### Library: `lib/status-file-helpers.sh`

**Public symbols:** `status_file_lock_path`, `with_status_file_lock`, `status_file_update`, `status_file_write`.  
Sourced by `regression-helpers.sh` (line 20–23) and by `orchestrator-common.sh`.  
Idempotently-loadable (guard at line 7–8). Requires `$STATUS_FILE` global set by sourcer.

**Locking mechanism — two paths, one interface:**

`with_status_file_lock` (lines 30–43) selects the path by `flock` binary availability:

1. **flock path** (Linux/Mac with util-linux): opens `$STATUS_FILE.lock` on fd 200, runs `flock -x 200` (blocking exclusive lock) in a subshell, executes `"$@"`, subshell exits releasing fd.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–27`

2. **mkdir path** (fallback / macOS without util-linux flock): busy-spins `mkdir <lock_path>.d 2>/dev/null` at **50 ms intervals** with no timeout and no attempt cap until `mkdir` succeeds (atomic directory creation as mutex), executes `"$@"`, `rmdir` the lock dir.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–22`

**Atomicity on writes:** `status_file_update` applies a jq filter on top of existing JSON (read-modify-write); `status_file_write` builds JSON from scratch. Both write to `$STATUS_FILE.tmp.$$` (PID-qualified to avoid collisions). Only if jq exits 0 is `mv` called to replace `$STATUS_FILE`; on jq failure the temp is deleted and `$STATUS_FILE` is left untouched.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:49–60`

**Anomaly A2 — Infinite spin in mkdir lock path:** A crashed process that created the lock dir but failed to `rmdir` it (e.g. SIGKILL between execute and cleanup) will deadlock all subsequent callers permanently until the lock dir is manually removed. No timeout, no attempt cap.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–22`

**Constants:**
| Constant | Value |
|---|---|
| Lock busy-poll interval (mkdir path) | `0.05` seconds |
| Temp file suffix | `.tmp.$$` (PID-qualified) |

**Generic shape:** The entire `with_status_file_lock` / `status_file_update` / `status_file_write` primitive operates on any `$STATUS_FILE` with no project assumptions. Extract as-is.

---

## 2. Failure Classification + Regression Math

### Library: `lib/classify-failures.sh`

**Public symbol:** `classify_failures <failures_json>`.  
Called from the test loop in `orchestrator-common.sh` to bucket a raw failure array into `{e2e:[…], unit:[…]}`. Single jq pipeline; no loop; no files read.

**Output:** compact JSON `{"e2e":[…], "unit":[…]}`. Exit 0 always (jq errors become empty arrays).

**Hey Soo!-specific coupling (A7 / A3 — highest severity):**

The classification regex is hardcoded to two Hey Soo! patterns:
```bash
# e2e bucket: path starts with tests/e2e/ OR filename ends in .spec.ts
((.test // "") | test("^tests/e2e/"; "i")) or
((.test // "") | test("\\.spec\\.ts"; "i"))
```
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/classify-failures.sh:14–16`

The design doc (§5) names `FAILURE_CLASSIFIER` as a project-config adapter key. The current source has no such abstraction: `classify-failures.sh` is a single hardcoded jq expression. This named adapter does not yet exist in the source tree.

**Generic shape:** A `FAILURE_CLASSIFIER` plugin (callable or config) that accepts `failures_json` and returns `{e2e:[…], unit:[…]}`. The e2e/unit taxonomy itself is Hey Soo!-specific; the generic shape should accept a pluggable bucket definition (e.g. a list of named `{name, path_pattern}` buckets) or be replaced by a project-provided classifier.

**Note:** `classify_failures` splits into e2e/unit buckets, but `compute_regressions` and `get_inherited_failures` in `regression-helpers.sh` operate on a flat array. The two files serve separate consumers in `orchestrator-common.sh`. Regressions are reported flat, not by bucket (A3).

---

### Library: `lib/regression-helpers.sh`

**Public functions:** `update_test_loop_metadata`, `capture_baseline_failures`, `compute_regressions`, `get_inherited_failures`.  
Sources `status-file-helpers.sh` on load (lines 20–23). Requires globals `$LOG_BASE`, `$STATUS_FILE`, and injected functions `log()`, `log_error()`, `sync_status_to_log()`.

**Outputs:**
- `$LOG_BASE/context/baseline-failures.json` (written by `capture_baseline_failures` lines 190–201):
  ```json
  {
    "captured_at": "<ISO-8601>",
    "base_branch": "<string>",
    "base_commit": "<short-sha>",
    "capture_status": "success" | "error",
    "failure_names": ["<pytest-id-or-spec-file:line>", …]
  }
  ```
- `$STATUS_FILE` (via `status_file_update`), updated by `update_test_loop_metadata` (lines 36–43):
  ```
  .stages.test_loop.last_run_type      = "incremental" | "full"
  .stages.test_loop.last_tested_commit = "<sha>"
  .stages.test_loop.incremental_files  = [<path>, …]
  .last_update                         = "<ISO-8601>"
  ```
- stdout of `compute_regressions` / `get_inherited_failures` — compact JSON array of failure objects filtered from input.

**`capture_baseline_failures` control flow (6 states):**
1. Resolve base-branch commit: try three candidate refs in order; fail → write error baseline, return 1.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:65–75`
2. Compare worktree HEAD to base commit; if equal, set `needs_checkout=false`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:79–85`
3. If checkout needed: `git stash --include-untracked` → `git checkout --detach <hash>`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:88–104`
4. Run `test-unit.sh --skip-infra` and optionally `e2e-smoke.sh` (if playwright/e2e keyword in task description OR `.spec.ts` files exist on base branch).
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:113–137`
5. Return to original branch: `git checkout -` then **`git stash pop`** (always runs on this path — no stash leak).
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:141–153`
6. Parse failures from captured output strings; write `baseline-failures.json`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:157–205`

**Stash-leak claim RETRACTED (post-verify):** An earlier draft claimed a stash leak when `git checkout -` fails (line 141). This is false: line 146 explicitly runs `[[ "$stashed" == "true" ]] && git -C "$cap_worktree" stash pop 2>/dev/null || true` on the failure path — the stash is always restored. The only real (cosmetic) flaw on this path is that the error baseline hardcodes `base_commit:"unknown"` even though the hash was already resolved.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:141–148`

**External invocations (VERBATIM):**
```bash
git -C "$cap_worktree" rev-parse --verify --quiet "$candidate"   # line 66
git -C "$cap_worktree" rev-parse HEAD                            # line 80
git -C "$cap_worktree" stash --include-untracked                 # line 90
git -C "$cap_worktree" checkout --detach "$base_commit_hash"     # line 97
git -C "$cap_worktree" checkout -                                 # line 141
git -C "$cap_worktree" stash pop                                  # line 152
git -C "$cap_worktree" rev-parse --short HEAD                    # line 108
git -C "$cap_worktree" ls-files '*.spec.ts'                      # line 127
cd "$cap_worktree" && bash .claude/scripts/test-unit.sh --skip-infra  # line 115
cd "$cap_worktree" && bash .claude/scripts/e2e-smoke.sh               # line 134
```

**Regression math:**
- `compute_regressions`: current failures not in `baseline.failure_names[]` → regressions. Missing/error baseline → all current failures treated as regressions.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:221–226`
- `get_inherited_failures`: returns failures present in baseline (inverse). Invalid `capture_status` → returns `[]`.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:247–249`

**Hey Soo!-specific coupling in regression-helpers.sh:**
| Item | Coupling | Generic shape |
|---|---|---|
| `bash .claude/scripts/test-unit.sh --skip-infra` (line 115) | Hey Soo! script path | `$TEST_UNIT_CMD` |
| `bash .claude/scripts/e2e-smoke.sh` (line 134) | Hey Soo! script path | `$TEST_E2E_CMD` |
| E2E detection: grep for `playwright\|e2e` in `extract-output.json` `.task.description` | Reads Hey Soo! extraction schema | `$RUN_E2E` boolean env or capability flag |
| `git ls-files '*.spec.ts'` secondary E2E trigger | `.spec.ts` is Playwright/Vitest | Configurable E2E file glob |
| pytest FAILED line parser | pytest output format | `parse_unit_failures <output>` hook |
| Playwright failure block parser | Playwright list-reporter format | `parse_e2e_failures <output>` hook |

**Anomaly A8:** Lines 72 and 100 both emit `base_commit: "unknown"` even when `base_commit_hash` was successfully resolved. Only lines 143–145 (post-test return failure) correctly populate `base_commit`.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:100–103`

---

## 3. Test Runners, Incremental Selection + Port Registry

### Library: `lib/run-tests-direct.sh`

**Public function:** `run_tests_direct <worktree> [--skip-infra] [--e2e] [--shell] [--files '<json-array>']`

Returns 0 always; pass/fail encoded in JSON stdout.

**Output JSON schema:**
```json
{
  "result": "passed|failed",
  "summary": "<string>",
  "total_tests": <int>,
  "passed_tests": <int>,
  "failed_tests": <int>,
  "failures": [{"test": "<id>", "message": "<msg>"}, ...],
  "infrastructure_failure": <bool>,
  "e2e_infrastructure_failure": <bool>,
  "unit_infrastructure_failure": <bool>,
  "crash_reason": null | "port_conflict" | "browser_crash" | "server_timeout" | "oom" | "killed" | "unknown"
}
```

**Test runner commands (VERBATIM):**
```bash
# Incremental unit tests:
cd "$worktree" && uv run pytest $pytest_files -v
# /run-tests-direct.sh:84

# Incremental E2E tests:
cd "$worktree" && bash .claude/scripts/e2e-smoke.sh $spec_files
# /run-tests-direct.sh:93

# Incremental shell tests:
cd "$worktree" && bash .claude/scripts/test-shell.sh $bats_files
# /run-tests-direct.sh:102

# Full unit tests:
cd "$worktree" && bash .claude/scripts/test-unit.sh [--skip-infra]
# /run-tests-direct.sh:114

# Full shell tests:
cd "$worktree" && bash .claude/scripts/test-shell.sh
# /run-tests-direct.sh:119

# Full E2E tests:
cd "$worktree" && bash .claude/scripts/e2e-smoke.sh
# /run-tests-direct.sh:125
```

**Incremental vs full branch:**
- `--files <json>` non-empty → incremental: jq-filter by extension (`.py` → pytest, `.spec.ts` → e2e-smoke, `.bats` → test-shell).
- No `--files` → full suite (unit always; shell if `--shell`; E2E if `--e2e`).

**Infra failure detection (circuit-breaker signal):**  
Non-zero exit + zero parsed failures → `infrastructure_failure=true` + crash reason.  
E2E crash reasons detected by string matching:
- `EADDRINUSE` → `port_conflict`
- `browser.*closed|disconnected|crashed` → `browser_crash`
- `timeout.*waiting.*server|Timed out` → `server_timeout`
- `out of memory|OOM|ENOMEM` → `oom`
- `SIGTERM|SIGKILL|signal` → `killed`
- else → `unknown`

`/run-tests-direct.sh:184–194`

No retries within `run_tests_direct`. Orchestrator reads `infrastructure_failure` from JSON and decides whether to call `reset_test_infrastructure` and retry.

**Files written per invocation:**
- `$LOG_BASE/stages/<NN>-<stage_name>.log` — combined stdout/stderr
- `$STAGE_INDEX` — one markdown table row appended
- `$STAGE_COUNTER_FILE` — integer incremented by 1

---

### Library: `lib/build-incremental-test-prompt.sh`

**Public function:** `build_incremental_test_prompt <worktree_path> <changed_tests_json> [e2e_block]`

Splits `$changed_tests_json` into `.py` (pytest section) and `.spec.ts` (E2E section) via jq; produces a human-readable prompt for an LLM test agent.

Commands embedded in the prompt text (agent-facing):
```bash
cd $worktree_path && uv run pytest $pytest_files -v
bash .claude/scripts/e2e-smoke.sh $spec_files
```
`/build-incremental-test-prompt.sh:45–61`

**Anomaly:** `.bats` shell tests are silently dropped from LLM-agent prompts — `run_tests_direct` handles bats correctly but the LLM-agent path omits them entirely.
`/build-incremental-test-prompt.sh:28–63`

---

### Library: `lib/select-incremental-test-agent.sh`

**Public function:** `select_incremental_test_agent <changed_tests_json>`

Returns: `{"agent":"<name>","family":"<family>"}`.

Decision tree (by extension detection on `changed_tests_json`):
- E2E only → `agent="bulletproof-frontend-developer"`, `family="e2e_only"`
- Python only → `agent="python-backend-developer"`, `family="python_only"`
- Mixed py+e2e → `agent="python-backend-developer"`, `family="mixed"`
- Shell only (.bats) → `agent="python-backend-developer"`, `family="shell_only"`
- Nothing matched / invalid JSON → `agent="python-backend-developer"`, `family="default"`

`/select-incremental-test-agent.sh:30–39`

**§8 coupling:** Agent names `"bulletproof-frontend-developer"` and `"python-backend-developer"` are Hey Soo!-specific. The routing shape (classify files → select agent) is generic. Generic shape: `FRONTEND_AGENT_ID` / `DEFAULT_AGENT_ID` config keys.

---

### Library: `lib/port-registry.sh`

**Public functions:** `port_registry_claim_port()`, `port_registry_release_claim()`

Data file: `$REPO_ROOT/.claude/port-registry.json` (JSON array; runtime-only state, not persisted between runs).

**Port range:**
| Constant | Value |
|---|---|
| `PORT_REGISTRY_MIN_PORT` | 5173 |
| `PORT_REGISTRY_MAX_PORT` | 5272 |
| Range width | 100 ports |
| `PORT_REGISTRY_LOCK_TIMEOUT_SECONDS` | 30 |
| Lock poll interval | 0.1 s |
| Max lock spin iterations | 300 (= 30s / 0.1s) |

Claim flow: acquire mkdir-lock → cleanup stale entries (PID liveness via `kill -0`) → scan from `preferred_port` modularly for first unclaimed port → write entry `{port, worktree, pid, started}` → release lock → print claimed port.  
Release flow: acquire lock → cleanup stale → filter out entries matching `(worktree, pid)` → write back → release.

Uses atomic `mktemp` + `mv` for registry writes:
```bash
mktemp "${parent_dir}/port-registry.XXXXXX.tmp"  # /port-registry.sh:29
mv "$tmp_file" "$registry_file"                    # /port-registry.sh:31
```

**§8 coupling:** Default port 5173 (Vite dev server) is Hey Soo!-specific. Generic shape: `PORT_REGISTRY_MIN` / `PORT_REGISTRY_MAX` config keys.

**Anomaly:** `reset-infra.sh` port kill range (5173–5179, 7 ports) is narrower than registry range (5173–5272, 100 ports). During a `port_conflict` reset, only the first 7 ports are freed; worktrees using ports 5180+ are not reclaimed.
`/reset-infra.sh:64` vs `/port-registry.sh:7–8`

---

### Library: `lib/detect-change-scope.sh`

**Public function:** `detect_change_scope <worktree_path> [base_ref]`

Default `base_ref`: `HEAD~1`.

**Output JSON:**
```json
{
  "scope": "test_only|source_only|mixed|none|error",
  "changed_tests": ["<relative-path>", …],
  "message": "<error-string or empty>",
  "shell_lib_changed": true|false
}
```

Scope decision matrix:
- `conftest.py` changed → `mixed` (overrides test_only even if no source changes)
- `test_count > 0 && source_count > 0` → `mixed`
- `test_count > 0` → `test_only`
- `source_count > 0` → `source_only`
- else → `none`

Shell library changes: auto-adds corresponding `.bats` file to `changed_tests[]` if it exists on disk.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:103–109`

**§8 coupling — file extension + path patterns:**
| Item | Coupling | Generic shape |
|---|---|---|
| `.spec.ts` as test file | Playwright/Vitest | Configurable test-file glob list |
| `test_*.py` / `*_test.py` | pytest | Configurable test-file glob list |
| `*.test.ts` / `*.test.tsx` | Vitest/Jest | Configurable test-file glob list |
| `*.bats` | Hey Soo! BATS shell tests | Configurable test-file glob list |
| `conftest.py` forces `mixed` | pytest-specific | Runner-agnostic "force-full" file list |
| `.claude/scripts/lib/*.sh` pattern | Hey Soo! shell-lib path | Configurable library-file glob |
| `lib/foo.sh` → `tests/shell/lib/test-foo.bats` mapping | Hey Soo! BATS layout | Configurable lib→test mapping fn |

---

### Library: `lib/track-test-commit.sh`

**Public functions:** `claim_worktree_ownership`, `release_worktree_ownership`, `assert_worktree_owned_by_orchestrator`, `record_last_tested_commit`, `get_last_tested_commit`.

Marker file: `<worktree_path>/.orchestrator-owner` (key=value):
```
pid=<int>
log_base=<path>
claimed_at=<ISO-8601>
```
Commit file: `$log_base/context/last-tested-commit` — plain text, single full SHA, no newline.

**`claim_worktree_ownership` decision tree:**
1. No marker file → write new marker.
2. Marker exists, `log_base` matches → return 0 (already ours).
3. Marker exists, different `log_base`, PID still alive → warn, return 1 (refuse).
4. Marker exists, different `log_base`, PID dead → warn, overwrite (stale cleanup).

`record_last_tested_commit` / `get_last_tested_commit` gate on `assert_worktree_owned_by_orchestrator` (returns 1 if not owner).

Constant: `TRACK_TEST_COMMIT_OWNER_FILE = ".orchestrator-owner"` (readonly).

---

### Library: `lib/reset-infra.sh`

**Public function:** `reset_test_infrastructure <worktree_path> <crash_reason>`

Best-effort only; returns 0 always; all kill/rm operations use `|| true`.

Kill sequence:
1. `pgrep -f 'vite.*--port'` → SIGTERM, then SIGKILL after 1s.
2. `pgrep -f 'playwright.*test'` → SIGTERM.
3. `pgrep -f 'chrome-headless-shell\|chromium.*--headless'` → SIGTERM.
4. If `port_conflict`: `lsof -ti :<port>` for ports 5173–5179 → kill.
5. `rm -rf <worktree>/tests/e2e/test-results/`.
6. Reason-specific sleep: `oom` → 5s; `server_timeout` → 2s; `killed` → 3s; else → 2s.

---

## 4. Project-Config Adapter Surface (§8 Summary)

The following items are the primary coupling surface that must be parameterised in the Python engine. Test commands and classifier regexes are named by the design doc but not yet abstracted in the source:

| Config Key (design shape) | Source value (Hey Soo!) |
|---|---|
| `INSTALL_CMD` / `TEST_UNIT_CMD` | `uv run pytest` |
| `TEST_UNIT_SCRIPT` | `bash .claude/scripts/test-unit.sh` |
| `TEST_E2E_SCRIPT` / `TEST_E2E_CMD` | `bash .claude/scripts/e2e-smoke.sh` |
| `TEST_SHELL_SCRIPT` | `bash .claude/scripts/test-shell.sh` |
| `FAILURE_CLASSIFIER` | hardcoded jq: `^tests/e2e/` or `\.spec\.ts` (Hey Soo! coupling) |
| `E2E_FILE_PATTERN` | `.spec.ts` extension |
| `UNIT_FILE_PATTERN` | `.py` extension |
| `SHELL_FILE_PATTERN` | `.bats` extension |
| `FRONTEND_AGENT_ID` | `"bulletproof-frontend-developer"` |
| `DEFAULT_AGENT_ID` | `"python-backend-developer"` |
| `PORT_REGISTRY_MIN` | `5173` (Vite default) |
| `PORT_REGISTRY_MAX` | `5272` |
| `E2E_ARTIFACTS_DIR` | `tests/e2e/test-results/` |
| `DEV_SERVER_PATTERN` | `'vite.*--port'` |
| `BROWSER_PROCESS_PATTERN` | `'chrome-headless-shell\|chromium.*--headless'` |

---

## 5. Test Corpus — Behavioral Contract (41 files, ~550+ cases)

The test corpus is the behavioral ground truth for the Python port. It is the contract Phase 3a must satisfy.

**Summary statistics:**
| Zone | Files | ~Cases |
|---|---|---|
| A — `implement-issue-test/*.bats` | 20 | 391 (`@test` count) |
| B — `.claude/scripts/tests/*.sh` | 6 | ~65 |
| C — `tests/shell/**/*.bats` | 8 | 72 (`@test` count) |
| D — `.claude/scripts/test-*.sh` (loose) | 7 | ~50 |
| **Total** | **41** | **~550+** (bats `@test` alone = 463; +B/D raw-bash cases) |

**Invocation commands (VERBATIM):**
```bash
uv run pytest -v
bash .claude/scripts/test-unit.sh
bash .claude/scripts/test-shell.sh
```

### Zone A — `implement-issue-test/*.bats` (20 files, BATS)

Sourced via `implement-issue-test/helpers/test-helper.bash`, which calls `source_orchestrator_functions()` → sources `lib/orchestrator-common.sh` + extracts `main()` from `implement-orchestrator.sh`.

| ID | File | Cases | Unit under test | Port value |
|---|---|---|---|---|
| A-01 | `test-argument-parsing.bats` | 13 | `implement-orchestrator.sh` CLI parser | HIGH |
| A-02 | `test-constants.bats` | 11 | Constants in `lib/orchestrator-common.sh` | HIGH |
| A-03 | `test-change-scope.bats` | 5 | `detect_change_scope()` | HIGH (logic), path patterns Hey Soo! |
| A-04 | `test-comment-helpers.bats` | 18 | `comment_issue()` / `comment_pr()` | MEDIUM |
| A-05 | `test-api-contract-review.bats` | 2 | `evaluate_api_contract_review_scope()` | MEDIUM (shape), paths Hey Soo! |
| A-06 | `test-build-incremental-test-prompt.bats` | 5 | `build_incremental_test_prompt()` | MEDIUM (logic), toolchain Hey Soo! |
| A-07 | `test-e2e-policy.bats` | 5 | `evaluate_e2e_policy()` + `merge_e2e_policy_review_finding()` | MEDIUM (logic), paths Hey Soo! |
| A-08 | `test-force-push-remediation.bats` | 8 | `detect_force_push_remediation()` | HIGH |
| A-09 | `test-infra-failure.bats` | ~45 | `run_tests_direct()` + `reset_test_infrastructure()` | HIGH (logic), port 5173–5179 Hey Soo! |
| A-10 | `test-integration.bats` | ~45 | `main()` + `run_quality_loop()` + `run_test_loop()` | HIGH (stage sequence), agent names Hey Soo! |
| A-11 | `test-json-parsing.bats` | ~55 | JSON extraction safety: `run_stage()`, `detect_rate_limit()`, `log()` | CRITICAL (printf/subprocess contract) |
| A-12 | `test-micro-mode.bats` | ~45 | `init_status(EXECUTION_MODE=micro)`, `skip_stage()`, `normalize_task_id()` | HIGH |
| A-13 | `test-quality-loop.bats` | ~20 | `run_quality_loop()` | HIGH |
| A-14 | `test-rate-limit.bats` | 17 | `detect_rate_limit()`, `extract_wait_time()`, `handle_rate_limit()` | HIGH |
| A-15 | `test-regression-helpers.bats` | 7 | `compute_regressions()`, `get_inherited_failures()`, `capture_baseline_failures()`, `update_test_loop_metadata()` | HIGH (logic), failure names Hey Soo! |
| A-16 | `test-select-incremental-test-agent.bats` | 5 | `select_incremental_test_agent()` | MEDIUM (logic), agent names Hey Soo! |
| A-17 | `test-setup-stage-cache.bats` | 3 | `install_frontend_dependencies()`, `install_python_dependencies()` | MEDIUM (pattern), toolchain Hey Soo! |
| A-18 | `test-stage-runner.bats` | 10 | `run_stage()`, `next_stage_log()`, `log()`, `log_error()`, `get_subtask_implementation_timeout()` | HIGH / CRITICAL |
| A-19 | `test-status-functions.bats` | 28 | `init_status()`, `update_stage()`, `set_stage_started/completed()`, `set_tasks()`, `update_task()`, `set_worktree_info()`, `set_final_state()`, `increment_*()` | CRITICAL |
| A-20 | `test-track-test-commit.bats` | 4 | `claim_worktree_ownership()`, `record_last_tested_commit()`, `get_last_tested_commit()` | HIGH |

**Key behavioral invariants from Zone A:**

- **A-01 CLI contract:** No args → exit 3; missing values for named flags → exit 3; default agent = `"default"`; default status file = `"status-issue-<N>.json"`.
- **A-02 Constants:** `STAGE_TIMEOUT=1800`, `MAX_TASK_REVIEW_ATTEMPTS=3`, `MAX_QUALITY_ITERATIONS=5`, `MAX_PR_REVIEW_ITERATIONS=3`, `RATE_LIMIT_BUFFER=60`, `RATE_LIMIT_DEFAULT_WAIT=3600`. Script uses `set -uo pipefail` and NOT `set -e`.
- **A-09 Infra + crash reasons:** Constant `MAX_CONSECUTIVE_INFRA_FAILURES=3`; infra failure = non-zero exit + zero parsed failures.
- **A-11 JSON safety:** `echo "$var"` mangles JSON starting with `-n` or `-e`; `printf '%s' "$var"` is safe; `log()` must NOT pipe to `tee` (stdout pollution). MIXES documentation-only and enforcing tests — do not blanket-skip prefixed tests (see §6 anomaly).
- **A-12 Micro mode:** 6 stages skipped (research, evaluate, plan, quality_loop, test_loop, docs); 5 stages active; `stages_skipped=6`, `stages_executed=5`. Micro skips `test_loop`; lite does NOT.
- **A-14 Rate limit:** `status=rate_limit` OR text `"rate limit"` / `"429"` / `"too many requests"` / `"quota exceeded"` detected; case-insensitive; `Retry-After: N` takes precedence over minutes pattern.
- **A-18 Stage runner:** Missing schema → exit 1 `"schema not found"`; missing `structured_output` → exit 1 `"no structured output"`; timeout (exit 124) → exit 1 `"timeout"`; counter padded `%02d`; `--agent <name>` passed through to `claude` CLI.
- **A-19 Status-file API:** `init_status` → `state=initializing`, issue as `"#N"`, all stages `"pending"`, empty tasks array. Concurrent writers never produce invalid JSON. All field names enumerated as CRITICAL contract.

---

### Zone B — `.claude/scripts/tests/*.sh` (6 files, raw bash)

| ID | File | Cases | Unit under test | Port value |
|---|---|---|---|---|
| B-01 | `test-e2e-override.sh` | 6 | E2E detection pattern `'^frontend/\|\.spec\.ts$'` | MEDIUM (logic), patterns Hey Soo! |
| B-02 | `test-e2e-routing.sh` | 4 | `e2e-smoke.sh` Playwright suite routing | LOW (Hey Soo! only); live filesystem dep, cannot port as-is |
| B-03 | `test-port-registry.sh` | 4 | `port_registry_claim_port()`, `port_registry_release_claim()` | MEDIUM (logic), port 5173 Hey Soo! |
| B-04 | `test_codex_prompt_schema.sh` | 4 | `render_stage_prompt()` — codex vs claude adaptation | HIGH |
| B-05 | `test_cost_summary_provider.sh` | 4 | `emit_cost_summary()` — provider label logic | HIGH |
| B-06 | `test_orchestrator_cleanup.sh` | ~40 | `orchestrator_cleanup()`, `register_orchestrator_traps()`, `set_normal_exit()`, `monitor-orchestrator.sh --cleanup` | HIGH |

**Key invariants from Zone B:**

- **B-04 Codex prompt:** codex provider strips `"Use the Skill tool"` directive; inlines schema body with `"Required JSON schema (<file>):"` header; claude provider is byte-identical pass-through.
- **B-05 Cost summary:** `provider=codex` → label `"**Codex invocations:** N"` (not `"Claude invocations"`); mixed → `"**Mixed-provider invocations:** N"`.
- **B-06 Cleanup/traps:** Signal-caught → `.state=killed`; normal exit → status unchanged; re-entrant guard (second cleanup is no-op); cleanup NEVER removes log directory or files; TERM/INT/HUP/EXIT all registered; `monitor --cleanup` with `--dry-run` does NOT modify files.

---

### Zone C — `tests/shell/**/*.bats` (8 files, BATS)

| ID | File | Cases | Unit under test | Port value |
|---|---|---|---|---|
| C-01 | `test-orchestrator-provider.bats` | 24 | `get_orchestrator_provider()`, `should_use_codex()`, `run_provider_oneshot()`, `run_codex_stage()` | CRITICAL |
| C-02 | `test-render-stage-prompt.bats` | 14 | `render_stage_prompt()` | CRITICAL |
| C-03 | `test-ralph-resume-decision.bats` | 9 | `compute_resume_decision()` | HIGH |
| C-04 | `test-detect-change-scope.bats` | 2 | `detect_change_scope()` — BATS file routing | MEDIUM |
| C-05 | `test-run-tests-direct.bats` | 2 | `run_tests_direct()` shell-suite integration | MEDIUM |
| C-06 | `test-check-e2e-selectors.bats` | 2 | `check-e2e-selectors.mjs` | LOW (Hey Soo! only) |
| C-07 | `test-check-testid-stability.bats` | 2 | `check-testid-stability.mjs` | LOW (Hey Soo! only) |
| C-08 | `test-deploy-skip-build.bats` | 3 | `.claude/scripts/deploy.sh` build skip logic | LOW (Hey Soo! only) |

**Key invariants from Zone C:**

- **C-01 Provider dispatch (CRITICAL):** Unset/empty provider → `"claude"`; invalid provider → non-zero exit; `TASK_PROVIDER=codex` routes only file-patching stages (implement, fix); global `ORCHESTRATOR_PROVIDER=codex` overrides per-task claude; `run_provider_oneshot` timeout arg kills hung claude with exit 124; missing CLI → rc=127; `run_codex_stage` records `provider=codex` in `stage-costs.jsonl`.
- **C-02 Prompt rendering (CRITICAL):** `codex` strips lines referencing `Skill`, `TodoWrite`, `Agent` (capitalised), `run_in_background`; appends `"Emit exactly one JSON object on stdout..."` postamble; `"skill level"` (lowercase) survives; `"agent configuration"` survives; empty prompt + claude → empty output.
- **C-03 Resume decision:** No status file → `RESUME_ARG=""`; `state=error|killed|running` → `--resume`; `state=completed|already_closed|no_changes` → no resume. Case 9 documents bash `$()` subshell mutation trap — no Python equivalent; do not port the anti-pattern test.

---

### Zone D — `.claude/scripts/test-*.sh` (7 loose scripts, raw bash)

| ID | File | Cases | Unit under test | Port value |
|---|---|---|---|---|
| D-01 | `test-capacity-jitter.sh` | 15 | `calculate_capacity_sleep()`, `capacity_wait_loop()` | HIGH |
| D-02 | `test-ci.sh` | — | CI entrypoint (runs test-unit.sh + test-shell.sh --tap) | LOW (infrastructure) |
| D-03 | `test-counter-final.sh` | 10+ | Stage counter persistence + resume logic | HIGH |
| D-04 | `test-counter-simulation.sh` | similar | Same invariants as D-03, more detailed walkthroughs | MEDIUM |
| D-05 | `test-shell.sh` | — | Test runner (discovers *.bats under tests/shell/) | INFRASTRUCTURE |
| D-06 | `test-stage-counter.sh` | 5 | `next_stage_log` counter mechanics | HIGH |
| D-07 | `test-unit.sh` | — | Run Python unit suites via uv run pytest | INFRASTRUCTURE / Hey Soo! |

**Key invariants from Zone D:**

- **D-01 Capacity jitter:** Pre-stage cap 3600s; post-timeout cap 1800s; floor: minimum 60s + jitter; first chunk capped at poll_interval (900s); early exit on capacity recovery; `CAPACITY_SLEEP_SECS` reflects actual sleep.
- **D-03/D-06 Stage counter:** Fresh run: stages numbered `01-N` sequentially; resume from `status.json` (`stage_counter` field) continues sequence; fallback: count existing `.log` files. `jq '.stage_counter // 0'` default. D-03 and D-04 are near-identical; merge into one parameterised pytest test.

---

### Fixtures (12 files, `implement-issue-test/fixtures/`)

Ground truth for `structured_output` JSON schema field names and values:
`implement-success.json`, `pr-success.json`, `rate-limit.json`, `review-approved.json`, `review-changes-requested.json`, `setup-error.json`, `setup-success.json`, `task-review-improvements.json`, `task-review-no-commit-needed.json`, `task-review-passed.json`, `test-failed.json`, `test-passed.json`.

---

## 6. Port Priority for Phase 3a

### Port immediately (highest generic contract value):
1. **A-19** `test-status-functions.bats` — Status-file API (CRITICAL)
2. **C-01** `test-orchestrator-provider.bats` — Multi-provider dispatch (CRITICAL)
3. **C-02** `test-render-stage-prompt.bats` — Prompt rendering (CRITICAL)
4. **A-18** `test-stage-runner.bats` — `run_stage()` invocation contract
5. **B-06** `test_orchestrator_cleanup.sh` — Cleanup/trap/signal handling
6. **A-14** `test-rate-limit.bats` — Rate-limit detect/handle/retry
7. **A-09** `test-infra-failure.bats` — Infra failure detection + reset
8. **A-13** `test-quality-loop.bats` — Quality loop iteration contract
9. **A-02** `test-constants.bats` — All numeric caps/timeouts
10. **A-01** `test-argument-parsing.bats` — CLI argument contract
11. **A-12** `test-micro-mode.bats` — Execution mode / stage-skip matrix
12. **D-01** `test-capacity-jitter.sh` — Capacity polling loop
13. **A-08** `test-force-push-remediation.bats` — Force-push counter logic
14. **A-20** `test-track-test-commit.bats` — Worktree ownership exclusion
15. **B-04** `test_codex_prompt_schema.sh` — Codex prompt adaptation
16. **B-05** `test_cost_summary_provider.sh` — Cost accounting by provider
17. **A-15** `test-regression-helpers.bats` — Baseline/regression diff

### Port with parameterisation (generic shape, Hey Soo! paths/names):
- A-03, A-04, A-06, A-07, A-10, A-11, A-16, C-03, D-03+D-06 (merge)

### Do NOT port:
- B-02 (live filesystem dependency)
- C-06, C-07, C-08 (Hey Soo!-only frontend tooling)
- D-07 (Python pytest runner, not a contract)

### Parameterise (do NOT hard-code in Python port):
- Agent names: `python-backend-developer`, `bulletproof-frontend-developer`, `phpdoc-writer`, `code-simplifier`, `spec-reviewer`, `code-reviewer`
- Path patterns: `lambda/`, `frontend/`, `tests/e2e/`, `infra/`, `frontend/src/lib/api/`
- Port 5173 (Vite default)
- Comment titles: `"Starting Automated Processing"`, `"Post-Completion Notes"`, etc.
- Test commands: `uv run pytest`, `bash .claude/scripts/e2e-smoke.sh`
- Failure classifier regexes: `^tests/e2e/`, `\.spec\.ts`

---

## 7. Anomalies (all sources)

**A1 — Stash-leak claim RETRACTED:** `capture_baseline_failures` does not leak a stash. Line 146 explicitly pops the stash on the `git checkout -` failure path. The only real flaw is that the error baseline hardcodes `base_commit:"unknown"` even when already resolved.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:141–148`

**A2 — Infinite spin in mkdir lock path:** No timeout cap; a SIGKILL'd process can permanently deadlock callers.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–22`

**A3 — classify-failures not used in regression path:** `compute_regressions` / `get_inherited_failures` operate on flat arrays; regressions are not reported by bucket.

**A4 — Dead variables in `capture_baseline_failures`:** `cap_output` and `cap_exit` declared but never assigned or read (with `shellcheck disable=SC2034`).
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:55–58`

**A5 — `detect_change_scope` success path always emits empty `message`:** Diagnostic text only populated on error path.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:138–142`

**A6 — `base_ref` default HEAD~1 partial first-commit mitigation:** Empty-tree fallback only triggers when `base_ref == HEAD~1` and `rev-list --count == 1`; explicit missing refs → `scope:"error"` with no auto-recovery.

**A7 — FAILURE_CLASSIFIER not implemented:** Design doc §5 names this as a project-config adapter; source has only a hardcoded jq expression.

**A8 — `capture_status:"error"` hardcodes `base_commit:"unknown"`:** Lines 72 and 100 emit `"unknown"` even after successful `rev-parse`.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:100–103`

**A9 — `build-incremental-test-prompt.sh` drops shell tests:** `.bats` files silently excluded from LLM-agent prompts; only `run_tests_direct` (direct path) handles them.
`/build-incremental-test-prompt.sh:28–63`

**A10 — `reset-infra.sh` port kill range narrower than registry range:** 5173–5179 killed; 5180–5272 not reclaimed.
`/reset-infra.sh:64` vs `/port-registry.sh:7–8`

**A11 — `test-json-parsing.bats` mixes documentation-only and enforcing tests (CORRECTED):** Only explicitly `# DOCUMENTATION-ONLY` cases always-pass (`ECHO_BUG` `:94`,`:112`; `CODECHECK` `:781`,`:799`,`:815` ending in bare `true`). Other prefixed tests are **real behavioral assertions** (e.g., `ECHO_BUG` `:55`/`:79`, `CODECHECK` `:925`, `:1116`). Do NOT blanket-skip prefixed tests in the pytest port — port the enforcing ones.

**A12 — `test-e2e-routing.sh` reads live filesystem:** Cannot be ported without fixture isolation.

**A13 — `test-ralph-resume-decision.bats` case 9** documents bash `$()` subshell mutation trap; no Python equivalent. Note, do not port the anti-pattern test itself.
