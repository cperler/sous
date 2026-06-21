# Fragment: run-tests-direct.sh · build-incremental-test-prompt.sh · select-incremental-test-agent.sh · port-registry.sh · reset-infra.sh
Source commit: 560dae9   Mapped lines: run-tests-direct.sh:1–334, build-incremental-test-prompt.sh:1–71, select-incremental-test-agent.sh:1–45, port-registry.sh:1–166, reset-infra.sh:1–110

---

## 1. Role & entry points — who invokes it, with what argv

**run-tests-direct.sh** — sourced library exposing `run_tests_direct()`. Called by orchestrators (e.g. `implement-orchestrator.sh`) as a direct-shell alternative to dispatching an LLM agent for testing. Signature:
```
run_tests_direct <worktree> [--skip-infra] [--e2e] [--shell] [--files '<json-array>']
```
`--files` switches to incremental mode (only specified files); omitting it runs the full suite. Returns 0 always; pass/fail encoded in the JSON stdout.

**build-incremental-test-prompt.sh** — sourced library exposing `build_incremental_test_prompt()`. Called by orchestrators that route to an LLM test agent instead of direct execution. Signature:
```
build_incremental_test_prompt <worktree_path> <changed_tests_json> [e2e_block]
```
Produces a human-readable prompt string instructing the agent which files to run and how.

**select-incremental-test-agent.sh** — sourced library exposing `select_incremental_test_agent()`. Called before dispatching an LLM test agent to pick the correct agent identity. Signature:
```
select_incremental_test_agent <changed_tests_json>
```
Returns JSON: `{"agent":"<name>","family":"<family>"}`.

**port-registry.sh** — sourced library exposing `port_registry_claim_port()` and `port_registry_release_claim()` (plus internal helpers). Consumed by `e2e-smoke.sh` and shell test scripts to allocate non-conflicting Vite dev-server ports across parallel worktrees. No direct CLI entry point. Also read/written as the data file `$REPO_ROOT/.claude/port-registry.json`.

**reset-infra.sh** — sourced library exposing `reset_test_infrastructure()`. Called by orchestrators when `run_tests_direct` returns `infrastructure_failure=true`. Signature:
```
reset_test_infrastructure <worktree_path> <crash_reason>
```

---

## 2. Inputs — every flag, env var, file read

### run-tests-direct.sh
| Input | Type | Default | Effect |
|---|---|---|---|
| `<worktree>` | positional arg | required | path to git worktree under test |
| `--skip-infra` | flag | false | passes `--skip-infra` to `test-unit.sh` |
| `--e2e` | flag | false | enables full E2E suite run |
| `--shell` | flag | false | enables full shell/bats suite run |
| `--files <json>` | option | `""` | JSON array of test file paths; switches to incremental mode |
| `$LOG_BASE` | env (global) | required | base path for stage log files |
| `$STAGE_COUNTER_FILE` | env (global) | required | file path storing current stage counter integer |
| `$STAGE_INDEX` | env (global) | required | file path for stage index table |
| `log()` / `log_error()` | function (global) | required | logging functions from sourcing orchestrator |

Reads: `$STAGE_COUNTER_FILE` (integer, via `cat`); `$STAGE_INDEX` (existence check); `$registry_file` indirectly via test scripts.

### build-incremental-test-prompt.sh
| Input | Type | Default | Effect |
|---|---|---|---|
| `<worktree_path>` | positional | required | embedded verbatim in the prompt `cd` command |
| `<changed_tests_json>` | positional | required | JSON array; split by extension into unit vs E2E sections |
| `[e2e_block]` | positional | `""` | appended verbatim after E2E instructions |

### select-incremental-test-agent.sh
| Input | Type | Default | Effect |
|---|---|---|---|
| `<changed_tests_json>` | positional | required | JSON array used to detect `.py`, `.spec.ts`, `.bats` extensions |

### port-registry.sh
| Input | Type | Default | Effect |
|---|---|---|---|
| `<registry_file>` | arg to each function | required | path to `port-registry.json` (typically `$REPO_ROOT/.claude/port-registry.json`) |
| `<worktree_path>` | arg | required | stored in registry entry |
| `<owner_pid>` | arg | required | stored; used for stale-entry detection via `kill -0` |
| `<preferred_port>` | arg | required | starting point for port-scan loop |
| `<started_at>` | arg | required | ISO timestamp stored in entry |
| `PORT_REGISTRY_MIN_PORT` | constant | 5173 | lower bound of port range |
| `PORT_REGISTRY_MAX_PORT` | constant | 5272 | upper bound; 100-port range |
| `PORT_REGISTRY_LOCK_TIMEOUT_SECONDS` | constant | 30 | max wait for `mkdir`-based lock |

Reads: `<registry_file>` (JSON array); lock directory at `<registry_file>.lock`.

### reset-infra.sh
| Input | Type | Default | Effect |
|---|---|---|---|
| `<worktree_path>` | positional | required | used to scope `node` process kill and locate `tests/e2e/test-results/` |
| `<crash_reason>` | positional | `"unknown"` | selects reason-specific sleep/kill path |
| `log()` / `log_error()` | function (global) | required | from sourcing orchestrator |

---

## 3. Outputs — every file written, exit codes, side effects

### run-tests-direct.sh
- **stdout**: JSON object with schema matching `implement-issue-test.json`:
  ```json
  {
    "result": "passed|failed",
    "summary": "<string>",
    "total_tests": <int>,
    "passed_tests": <int>,
    "failed_tests": <int>,
    "failures": [{"test":"<id>","message":"<msg>"},...],
    "infrastructure_failure": <bool>,
    "e2e_infrastructure_failure": <bool>,
    "unit_infrastructure_failure": <bool>,
    "crash_reason": null | "port_conflict" | "browser_crash" | "server_timeout" | "oom" | "killed" | "unknown"
  }
  ```
- **exit code**: always 0.
- **file written**: `$LOG_BASE/stages/<NN>-<stage_name>.log` — combined stdout/stderr from all test runners, with section headers.
- **file appended**: `$STAGE_INDEX` — one markdown table row per invocation.
- **file updated**: `$STAGE_COUNTER_FILE` — integer incremented by 1.

### build-incremental-test-prompt.sh
- **stdout**: multi-line string prompt (no files written, no side effects).
- **exit code**: 0 always.

### select-incremental-test-agent.sh
- **stdout**: JSON: `{"agent":"<name>","family":"<family>"}`.
- **exit code**: 0 always.

### port-registry.sh
- `port_registry_claim_port()`: writes updated `<registry_file>` (JSON array); stdout: claimed port number integer. Exit 1 if no port available or lock timeout.
- `port_registry_release_claim()`: writes updated `<registry_file>` (JSON array, entry removed). Exit 1 if lock timeout.
- Lock side effect: creates/removes `<registry_file>.lock` directory; creates temp file `<parent>/port-registry.XXXXXX.tmp` (atomic rename).

### reset-infra.sh
- **side effects**: `kill` / `kill -9` on Vite, Playwright, chromium, and node processes; `rm -rf <worktree>/tests/e2e/test-results/`; sleeps (2s, 3s, or 5s depending on reason).
- **exit code**: 0 always (best-effort).
- No files written.

---

## 4. Control flow — state machine with exact caps and exit conditions

### run-tests-direct.sh
1. **Parse args** — positional `<worktree>` captured; flags `--skip-infra`, `--e2e`, `--shell`, `--files` consumed. `/run-tests-direct.sh:29–37`
2. **Guard** — empty worktree → emit error JSON, return 0. `/run-tests-direct.sh:39–43`
3. **Stage naming** — if `$files_json` non-empty: `stage_name="test-direct-incremental"` else `"test-direct-full"`. `/run-tests-direct.sh:47–51`
4. **Stage logging setup** — increment `$STAGE_COUNTER_FILE`; derive `$stage_log` path; append row to `$STAGE_INDEX` if file exists. `/run-tests-direct.sh:53–63`
5. **Branch: incremental vs full**.
   - **Incremental** (`$files_json` non-empty): `/run-tests-direct.sh:72–104`
     - jq-filter `.py` files → `unit_files`; if non-empty: `uv run pytest $pytest_files -v` (in `$worktree` subshell). `/run-tests-direct.sh:75,84`
     - jq-filter `.spec.ts` files → `e2e_files`; if non-empty: `bash .claude/scripts/e2e-smoke.sh $spec_files`. `/run-tests-direct.sh:76,93`
     - jq-filter `.bats` files → `shell_files`; if non-empty: `bash .claude/scripts/test-shell.sh $bats_files`. `/run-tests-direct.sh:77,102`
   - **Full suite** (`$files_json` empty): `/run-tests-direct.sh:106–128`
     - Always: `bash .claude/scripts/test-unit.sh [$unit_cmd_args]`. `/run-tests-direct.sh:114`
     - If `--shell`: `bash .claude/scripts/test-shell.sh`. `/run-tests-direct.sh:119`
     - If `--e2e`: `bash .claude/scripts/e2e-smoke.sh`. `/run-tests-direct.sh:125`
6. **Write stage log** — combined output to `$stage_log`. `/run-tests-direct.sh:131–151`
7. **Parse counts** — grep/awk over output strings for pytest `N passed`/`N failed`; Playwright same patterns; bats `^ok N` / `^not ok N`. `/run-tests-direct.sh:155–169`
8. **Infra failure detection** — if exit ≠ 0 AND parsed failures = 0 → `*_infra_failure=true`; E2E additionally parses crash reason from output strings. `/run-tests-direct.sh:178–213`
9. **Parse failure details** — `FAILED` lines from pytest; `*.spec.ts:N` lines from Playwright failure block; `^not ok` lines from bats. All merged via `jq -n '$u + $s + $e'`. `/run-tests-direct.sh:222–274`
10. **Determine result** — any non-zero exit → `result="failed"`. `/run-tests-direct.sh:277–280`
11. **Build summary string** — join non-empty suite summaries with `"; "`. `/run-tests-direct.sh:282–299`
12. **Emit JSON** via `jq -n`. `/run-tests-direct.sh:311–333`

### build-incremental-test-prompt.sh
1. Split `$changed_tests_json` into `unit_files` (`.py`) and `e2e_files` (`.spec.ts`) via jq. `/build-incremental-test-prompt.sh:28–29`
2. If `unit_files` non-empty: append pytest section with `cd $worktree_path && uv run pytest $pytest_files -v`. `/build-incremental-test-prompt.sh:45–46`
3. If `e2e_files` non-empty: append Playwright section with `bash .claude/scripts/e2e-smoke.sh $spec_files` + `$e2e_block`. `/build-incremental-test-prompt.sh:60–61`
4. Append closing instruction. `/build-incremental-test-prompt.sh:66–69`
5. `printf '%s' "$prompt"` to stdout. `/build-incremental-test-prompt.sh:70`

Note: `.bats` files are handled by `run_tests_direct` directly but **not** included in `build_incremental_test_prompt` — shell tests are absent from the LLM-agent path.

### select-incremental-test-agent.sh
1. jq-test `changed_tests_json` for `.py`, `.spec.ts`, `.bats` extensions → `has_py`, `has_e2e`, `has_shell`. `/select-incremental-test-agent.sh:23–25`
2. Decision tree: `/select-incremental-test-agent.sh:30–39`
   - E2E only → `agent="bulletproof-frontend-developer"`, `family="e2e_only"`
   - Python only (no E2E, no shell) → `agent="python-backend-developer"` (default), `family="python_only"`
   - Mixed py+e2e → `agent="python-backend-developer"`, `family="mixed"`
   - Shell only → `agent="python-backend-developer"`, `family="shell_only"`
   - Nothing matched → `agent="python-backend-developer"`, `family="default"`
3. Emit JSON via `jq -nc`. `/select-incremental-test-agent.sh:41–44`

### port-registry.sh
`port_registry_claim_port()`:
1. Acquire `mkdir`-lock; spin at 100ms intervals; abort after `$PORT_REGISTRY_LOCK_TIMEOUT_SECONDS * 10` iterations (300 iterations = 30s). `/port-registry.sh:41–48`
2. Cleanup stale entries (PID liveness via `kill -0`). `/port-registry.sh:107`
3. Scan from `preferred_port` mod `(MAX-MIN+1)` upward for first unclaimed port. `/port-registry.sh:113–119`
4. If no port found in range: log error, release lock, return 1. `/port-registry.sh:122–125`
5. Write entry `{port, worktree, pid, started}`, release lock, print claimed port. `/port-registry.sh:128–140`

`port_registry_release_claim()`:
1. Acquire lock, cleanup stale, filter out entries matching `(worktree, pid)`, write back, release. `/port-registry.sh:150–165`

### reset-infra.sh
1. `pgrep -f 'vite.*--port'` → kill (SIGTERM then SIGKILL after 1s). `/reset-infra.sh:36–43`
2. `pgrep -f 'playwright.*test'` → kill (SIGTERM). `/reset-infra.sh:46–51`
3. `pgrep -f 'chrome-headless-shell\|chromium.*--headless'` → kill. `/reset-infra.sh:54–59`
4. If `crash_reason == "port_conflict"`: loop ports 5173–5179, `lsof -ti :<port>` → kill. `/reset-infra.sh:62–72`
5. `rm -rf <worktree>/tests/e2e/test-results/`. `/reset-infra.sh:77–81`
6. Reason-specific path: `oom` → sleep 5; `server_timeout` → kill `node.*$worktree`, sleep 2; `killed` → sleep 3; `*` → sleep 2. `/reset-infra.sh:84–106`

---

## 5. External invocations — every command VERBATIM with flags, model, schema

### run-tests-direct.sh — test runner commands

**Incremental unit tests:**
```
cd "$worktree" && uv run pytest $pytest_files -v
```
(`$pytest_files` = space-separated `.py` paths from `--files` JSON) `/run-tests-direct.sh:84`

**Incremental E2E tests:**
```
cd "$worktree" && bash .claude/scripts/e2e-smoke.sh $spec_files
```
(`$spec_files` = space-separated `.spec.ts` paths) `/run-tests-direct.sh:93`

**Incremental shell tests:**
```
cd "$worktree" && bash .claude/scripts/test-shell.sh $bats_files
```
(`$bats_files` = space-separated `.bats` paths) `/run-tests-direct.sh:102`

**Full unit tests:**
```
cd "$worktree" && bash .claude/scripts/test-unit.sh [--skip-infra]
```
`/run-tests-direct.sh:114`

**Full shell tests:**
```
cd "$worktree" && bash .claude/scripts/test-shell.sh
```
`/run-tests-direct.sh:119`

**Full E2E tests:**
```
cd "$worktree" && bash .claude/scripts/e2e-smoke.sh
```
`/run-tests-direct.sh:125`

### build-incremental-test-prompt.sh — commands embedded in prompt text (instructing the LLM agent)

**Unit tests (agent-facing):**
```
cd $worktree_path && uv run pytest $pytest_files -v
```
`/build-incremental-test-prompt.sh:45–46`

**E2E tests (agent-facing):**
```
bash .claude/scripts/e2e-smoke.sh $spec_files
```
`/build-incremental-test-prompt.sh:60–61`

### port-registry.sh — OS calls
- `kill -0 $pid` — PID liveness check `/port-registry.sh:14`
- `mkdir "$lock_dir"` — advisory lock acquire `/port-registry.sh:41`
- `mktemp "${parent_dir}/port-registry.XXXXXX.tmp"` — atomic write staging `/port-registry.sh:29`
- `mv "$tmp_file" "$registry_file"` — atomic publish `/port-registry.sh:31`

### reset-infra.sh — OS calls
- `pgrep -f 'vite.*--port'` `/reset-infra.sh:36`
- `pgrep -f 'playwright.*test'` `/reset-infra.sh:47`
- `pgrep -f 'chrome-headless-shell\|chromium.*--headless'` `/reset-infra.sh:55`
- `lsof -ti ":<port>"` for ports 5173–5179 `/reset-infra.sh:67`
- `kill $pid` / `kill -9 $pid` `/reset-infra.sh:39,42,49,58,69,94`
- `rm -rf "<worktree>/tests/e2e/test-results/"` `/reset-infra.sh:80`

No LLM/claude/gh invocations in any of these five files.

---

## 6. Constants & tunables — numeric caps, timeouts, sleeps, pricing, model pins

| Constant | Value | Location |
|---|---|---|
| `PORT_REGISTRY_MIN_PORT` | 5173 | `/port-registry.sh:7` |
| `PORT_REGISTRY_MAX_PORT` | 5272 | `/port-registry.sh:8` |
| Port range width | 100 ports | derived |
| `PORT_REGISTRY_LOCK_TIMEOUT_SECONDS` | 30 | `/port-registry.sh:9` |
| Lock poll interval | 0.1 s | `/port-registry.sh:42` |
| Port conflict kill range (reset-infra) | 5173–5179 (7 ports) | `/reset-infra.sh:64` |
| Post-kill sleep (Vite) | 1 s | `/reset-infra.sh:40` |
| OOM cooldown | 5 s | `/reset-infra.sh:87` |
| `server_timeout` cooldown | 2 s | `/reset-infra.sh:95` |
| `killed` cooldown | 3 s | `/reset-infra.sh:100` |
| Default/general cooldown | 2 s | `/reset-infra.sh:104` |

---

## 7. Failure handling — retries, fallback chains, circuit breaker, cascade rules

### run-tests-direct.sh
- **No retries** within this function. The orchestrator reads `infrastructure_failure` from the JSON output and decides whether to call `reset_test_infrastructure` and retry. `/run-tests-direct.sh:172–213`
- **Infra failure detection** (circuit-breaker signal): non-zero exit + zero parsed failures → `infrastructure_failure=true` + specific `crash_reason`. `/run-tests-direct.sh:178–213`
- **E2E crash reasons** detected by string matching on output: `EADDRINUSE` → `port_conflict`; `browser.*closed|disconnected|crashed` → `browser_crash`; `timeout.*waiting.*server|Timed out` → `server_timeout`; `out of memory|OOM|ENOMEM` → `oom`; `SIGTERM|SIGKILL|signal` → `killed`; else → `unknown`. `/run-tests-direct.sh:184–194`
- **jq failures** are silenced with `|| echo "[]"` throughout. `/run-tests-direct.sh:231,252,267`
- **Empty worktree** → error JSON emitted, return 0 (never propagates error exit). `/run-tests-direct.sh:39–43`

### port-registry.sh
- **Lock contention**: spin 300 iterations × 0.1s = 30s max; on timeout prints error to stderr and returns 1. `/port-registry.sh:41–48`
- **No free ports**: returns 1 after scanning all 100 slots. `/port-registry.sh:122–125`
- **Stale PID cleanup**: runs automatically before every claim/release to reclaim orphaned entries. `/port-registry.sh:107,152`
- **jq failures** on malformed registry: `|| printf '[]'` fallback. `/port-registry.sh:66,110,155`
- **Lock release**: `rmdir ... || true` — never propagates errors. `/port-registry.sh:56`

### reset-infra.sh
- All kill and rm operations use `|| true` — best-effort only, never aborts. `/reset-infra.sh:39,42,49,58,68,69,80`
- Returns 0 always. `/reset-infra.sh:109`
- No retry logic; caller (orchestrator) decides whether to retry the test run.

### build-incremental-test-prompt.sh / select-incremental-test-agent.sh
- `jq ... 2>/dev/null` silences errors; `|| printf 'false'` provides safe default for boolean detection. `/select-incremental-test-agent.sh:23–25`
- No error paths or retries; both functions return 0 always.

---

## 8. Coupling — per item: generic vs Hey Soo!-specific

| Item | Hey Soo!-specific | Generic shape |
|---|---|---|
| `uv run pytest $files -v` | Python/uv project convention | `UNIT_TEST_CMD` (e.g. `uv run pytest`, `python -m pytest`, `npm test`) |
| `bash .claude/scripts/test-unit.sh` | Hey Soo! wrapper script path | `TEST_UNIT_SCRIPT` (configurable path) |
| `bash .claude/scripts/e2e-smoke.sh` | Hey Soo! wrapper + Playwright assumption | `TEST_E2E_SCRIPT` (configurable path) |
| `bash .claude/scripts/test-shell.sh` | Hey Soo! wrapper + bats assumption | `TEST_SHELL_SCRIPT` (configurable path) |
| `.spec.ts` extension → Playwright/E2E | TypeScript+Playwright project | `E2E_FILE_PATTERN` (configurable regex) |
| `.py` extension → pytest | Python project | `UNIT_FILE_PATTERN` (configurable regex) |
| `.bats` extension → shell tests | bats-core convention | `SHELL_FILE_PATTERN` (configurable regex) |
| `bulletproof-frontend-developer` agent name | Hey Soo!-specific agent identity | `FRONTEND_AGENT_ID` (configurable string) |
| `python-backend-developer` agent name | Hey Soo!-specific agent identity | `BACKEND_AGENT_ID` / `DEFAULT_AGENT_ID` |
| Port range 5173–5272 | Vite default (5173) + 99 worktrees | `PORT_REGISTRY_MIN` / `PORT_REGISTRY_MAX` |
| `tests/e2e/test-results/` path | Hey Soo! directory layout | `E2E_ARTIFACTS_DIR` (configurable) |
| `pgrep -f 'vite.*--port'` | Vite dev server | generic process kill by pattern: `DEV_SERVER_PATTERN` |
| `pgrep -f 'chrome-headless-shell\|chromium.*--headless'` | Chromium/Playwright | `BROWSER_PROCESS_PATTERN` |
| `.claude/scripts/` script prefix in worktree | Hey Soo! layout convention | scripts path should be a runtime config value |
| `port-registry.json` at `$REPO_ROOT/.claude/` | Hey Soo! repo layout | `PORT_REGISTRY_FILE` env var (already args-based; just needs config injection) |
| `$LOG_BASE`, `$STAGE_COUNTER_FILE`, `$STAGE_INDEX` globals | orchestrator-internal coupling | Python engine equivalent: logging context object passed in |

**Key coupling surface (test command verbatim):**
The three test commands — `uv run pytest`, `bash .claude/scripts/e2e-smoke.sh`, `bash .claude/scripts/test-shell.sh` — are the primary coupling surface. In the Python engine these should map to `INSTALL_CMD`, `TEST_UNIT_CMD`, `TEST_E2E_CMD`, `TEST_SHELL_CMD` configuration values. File extension patterns that route files to the right runner are a secondary coupling surface.

---

## 9. Anomalies — bugs, dead code, contradictions

1. **`build-incremental-test-prompt.sh` omits `.bats` shell tests** `/build-incremental-test-prompt.sh:28–63`. When orchestrators route to an LLM agent for incremental runs, shell test files in `changed_tests_json` are silently dropped — the agent prompt contains no instructions to run them. `run_tests_direct` handles bats correctly in the same scenario (`/run-tests-direct.sh:77,97–103`). This asymmetry means shell tests are only run when using the direct (non-LLM) path.

2. **`reset-infra.sh` port kill range (5173–5179) is narrower than port-registry range (5173–5272)** `/reset-infra.sh:64` vs `/port-registry.sh:7–8`. During a `port_conflict` reset, only the first 7 ports are freed; worktrees using ports 5180+ are not reclaimed. Comment at `/reset-infra.sh:61` says "5173-5179 used by Vite" which was true before port-registry was added.

3. **`select-incremental-test-agent.sh` has no path for empty input**. If `changed_tests_json` is `"[]"` or malformed jq returns `false` for all three, `family` remains `"default"` — which is a valid sentinel — but the caller must handle it. Not strictly a bug but undocumented. `/select-incremental-test-agent.sh:27–38`

4. **`port_registry_claim_port` loop uses modular offset arithmetic** `/port-registry.sh:113–115` — `candidate_port = MIN + ((preferred - MIN + offset) % range)`. If `preferred_port < MIN` the result is undefined (negative modulo in bash). The caller (`e2e-smoke.sh`) is responsible for ensuring `preferred_port` is within range, but this is not validated inside the function.

5. **`port-registry.json` is empty array `[]`** at HEAD (commit `560dae9`) — consistent with the registry being runtime-only state, not persisted between runs. This is expected behavior, not a bug.

6. **Stage log write at `/run-tests-direct.sh:151`** uses `2>/dev/null` — if `$LOG_BASE/stages/` does not exist the write silently fails with no error. The orchestrator must ensure the directory exists before calling `run_tests_direct`.

7. **Playwright failure line extraction** (`/run-tests-direct.sh:242–253`) uses `sed -n '/N failed/,/^$/p'` to find the failure block, then `grep -oE '[a-zA-Z0-9_/-]+\.spec\.ts:[0-9]+[^\n]*'`. This regex requires test paths to use only `[a-zA-Z0-9_/-]` characters — paths with spaces, dots in directory names, or unicode will be silently dropped.
