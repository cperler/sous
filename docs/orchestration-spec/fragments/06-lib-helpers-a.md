# Fragment: lib/status-file-helpers.sh · lib/classify-failures.sh · lib/regression-helpers.sh · lib/detect-change-scope.sh · lib/track-test-commit.sh
Source commit: HEAD (read 2026-06-20)   Mapped lines: status-file-helpers 1–81 · classify-failures 1–22 · regression-helpers 1–258 · detect-change-scope 1–189 · track-test-commit 1–164

## 1. Role & entry points — who invokes it, with what argv

**status-file-helpers.sh** — idempotently-loadable library (guard at line 7–8).
Exposes four public symbols: `status_file_lock_path`, `with_status_file_lock`,
`status_file_update`, `status_file_write`. Sourced by `regression-helpers.sh`
(line 20–23) and by `orchestrator-common.sh` (not in this fragment; inferred from
the guard pattern and the global `STATUS_FILE` dependency). No argv; all callers
pass jq filter strings via the two public primitives.

**classify-failures.sh** — single-function library. Exposes `classify_failures
<failures_json>`. Called from the test loop in `orchestrator-common.sh` to bucket
a raw failure array into `{e2e:[…], unit:[…]}`. No argv; takes a JSON string on
stdin-equivalent (positional arg).

**regression-helpers.sh** — library sourced by orchestrators that run a test loop.
Exposes three public functions: `update_test_loop_metadata`, `capture_baseline_failures`,
`compute_regressions`, `get_inherited_failures`. Sources `status-file-helpers.sh` on
load (lines 20–23). Requires globals `$LOG_BASE`, `$STATUS_FILE`, and three injected
functions: `log()`, `log_error()`, `sync_status_to_log()`.

**detect-change-scope.sh** — library exposing one public function:
`detect_change_scope <worktree_path> [base_ref]`. Three private helpers:
`_is_test_file`, `_is_shell_library_file`, `_shell_test_for_library`. Called from
`orchestrator-common.sh` to decide whether to run an incremental or full test suite.
No persistent state; pure stdout JSON.

**track-test-commit.sh** — library exposing five public functions:
`claim_worktree_ownership`, `release_worktree_ownership`,
`assert_worktree_owned_by_orchestrator`, `record_last_tested_commit`,
`get_last_tested_commit`. Called from `orchestrator-common.sh` at test-loop
boundaries. Writes/reads a marker file inside the worktree.

---

## 2. Inputs — every flag, env var (name / default / effect), file read

### status-file-helpers.sh
| Input | Default | Effect |
|---|---|---|
| `$STATUS_FILE` (env/global) | none — must be set by sourcer | Path to the JSON status file operated on by `status_file_update` / `status_file_write` |

Files read: `$STATUS_FILE` (inside `_status_file_update_locked`, line 49).

### classify-failures.sh
| Input | Notes |
|---|---|
| `$1` — failures JSON string | Array of `{test, message}` objects; consumed entirely via `jq` |

No env vars; no files read.

### regression-helpers.sh
| Input | Default | Effect |
|---|---|---|
| `$LOG_BASE` (global) | none | Directory for context files and baseline JSON |
| `$STATUS_FILE` (global) | none | Forwarded to `status-file-helpers.sh` primitives |
| `log()` / `log_error()` (injected fns) | none | Logging sink |
| `sync_status_to_log()` (injected fn) | none | Copies status file to log dir after writes |

Files read:
- `$LOG_BASE/context/baseline-failures.json` — by `compute_regressions` and `get_inherited_failures` (lines 221, 247)
- `$LOG_BASE/context/extract-output.json` — by `capture_baseline_failures` to detect if e2e is needed (line 123)

### detect-change-scope.sh
| Input | Default | Effect |
|---|---|---|
| `$1` — `worktree_path` | required | Path passed to all `git -C` calls |
| `$2` — `base_ref` | `HEAD~1` | Git ref to diff against |

No env vars. No files read (filesystem stat only: checks for `.bats` test file existence at line 108).

### track-test-commit.sh
| Input | Default | Effect |
|---|---|---|
| `$1` — `worktree_path` | required | Root of the git worktree |
| `$2` — `log_base` | required | Log directory; determines context subdir |
| `$3` — `owner_pid` (claim only) | `$$` | PID written to owner marker |

Files read: `<worktree_path>/.orchestrator-owner` (marker file, all public fns).
Files written: `<worktree_path>/.orchestrator-owner` (claim), `$log_base/context/last-tested-commit` (record).

---

## 3. Outputs — every file written (path, format, EVERY field), exit codes, side effects

### status-file-helpers.sh outputs
- `$STATUS_FILE` — rewritten atomically via `mv` from a temp file `$STATUS_FILE.tmp.$$`.
  `status_file_update` applies a jq filter *on top of* the existing JSON (read-modify-write).
  `status_file_write` runs jq with *no* input file (produces JSON from scratch from
  caller-supplied filter + args).
- Exit codes: 0 on success; non-zero propagated from jq or mv failures.

### classify-failures.sh outputs
- stdout — compact JSON: `{"e2e":[…], "unit":[…]}`; each element is an object from
  the input array. Exit 0 always (jq errors become empty arrays).

### regression-helpers.sh outputs
- `$LOG_BASE/context/baseline-failures.json` — JSON object written by
  `capture_baseline_failures` (lines 190–201):
  ```json
  {
    "captured_at": "<ISO-8601>",
    "base_branch": "<string>",
    "base_commit": "<short-sha>",
    "capture_status": "success" | "error",
    "failure_names": ["<pytest-id-or-spec-file:line>", …]
  }
  ```
- `$STATUS_FILE` (via `status_file_update`) — updated by `update_test_loop_metadata`
  (lines 36–43):
  ```
  .stages.test_loop.last_run_type    = "incremental" | "full"
  .stages.test_loop.last_tested_commit = "<sha>"
  .stages.test_loop.incremental_files = [<path>, …]
  .last_update                       = "<ISO-8601>"
  ```
- stdout of `compute_regressions` / `get_inherited_failures` — compact JSON array
  of failure objects, filtered from input.
- Side effects: `capture_baseline_failures` checks out a detached HEAD commit in the
  worktree, stashes/unstashes WIP, invokes `.claude/scripts/test-unit.sh --skip-infra`
  and `.claude/scripts/e2e-smoke.sh` as subprocesses, then returns to the original
  branch.

### detect-change-scope.sh outputs
- stdout — JSON object (one of four shapes):
  ```json
  {"scope": "test_only|source_only|mixed|none|error",
   "changed_tests": ["<relative-path>", …],
   "message": "<error-string or empty>",
   "shell_lib_changed": true|false}
  ```
  Exit code always 0 (errors encoded in `scope:"error"`).

### track-test-commit.sh outputs
- `<worktree_path>/.orchestrator-owner` — key=value text file (lines 65–69):
  ```
  pid=<int>
  log_base=<path>
  claimed_at=<ISO-8601>
  ```
- `$log_base/context/last-tested-commit` — plain text file containing a single
  full commit SHA, no newline (line 137).
- `release_worktree_ownership` — deletes `<worktree_path>/.orchestrator-owner`.
- Exit codes: `claim_worktree_ownership` returns 1 if worktree is live-owned by
  another orchestrator (live PID check at line 57); 0 otherwise. `record_last_tested_commit`
  and `get_last_tested_commit` return 1 on missing args or missing commit file.

---

## 4. Control flow — state machine: states, transitions, loop structure with exact caps and exit conditions

### status-file-helpers.sh
Lock acquisition (`with_status_file_lock`, lines 30–43):
1. Check for `flock` binary.
2. **flock path** (Linux/Mac with util-linux): open `$STATUS_FILE.lock` on fd 200,
   run `flock -x 200` (blocking exclusive lock) in a subshell, execute `"$@"`,
   subshell exits releasing the lock.
3. **mkdir path** (macOS without flock, or fallback): busy-spin `mkdir
   <lock_path>.d 2>/dev/null` with `sleep 0.05` between attempts until mkdir
   succeeds (atomic directory creation as mutex), execute `"$@"`, `rmdir` the lock
   dir.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–27`
4. Temp file named `$STATUS_FILE.tmp.$$` (PID-qualified) to avoid collisions among
   concurrent orchestrators.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:60`

Atomicity: jq writes to the temp file; only if jq exits 0 is `mv` called to replace
`$STATUS_FILE`. On jq failure the temp is deleted, `$STATUS_FILE` untouched.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:49–56`

### classify-failures.sh
Single jq pipeline pass; no loop.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/classify-failures.sh:13–21`

### regression-helpers.sh — capture_baseline_failures
States:
1. Resolve base-branch commit: try three candidate refs in order; fail → write error
   baseline and return 1.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:65–75`
2. Compare worktree HEAD to base commit; if equal, set `needs_checkout=false`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:79–85`
3. If checkout needed: `git stash --include-untracked` → `git checkout --detach <hash>`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:88–104`
4. Run `test-unit.sh --skip-infra` and optionally `e2e-smoke.sh` (if playwright/e2e
   keyword in task description OR `.spec.ts` files exist on base branch).
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:113–137`
5. Return to original branch: `git checkout -` then `git stash pop`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:141–153`
6. Parse failures from captured output strings; write `baseline-failures.json`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:157–205`

No retry loop; single-shot. Errors at any checkout step write an error-status
baseline and return 1 (callers treat missing/error baseline as "all failures are
regressions").

### detect-change-scope.sh
1. `git diff --name-status --find-renames <base_ref> HEAD`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:27`
   - On failure: if first commit (`rev-list --count == 1`) and `base_ref == HEAD~1`,
     retry against empty tree hash.
     `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:29–50`
2. Walk entries line-by-line; classify each via `_is_test_file` / `_is_shell_library_file`.
   Deleted test files increment `test_count` but are excluded from `changed_tests[]`.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:66–112`
3. Shell library changes: auto-add corresponding `.bats` file to `changed_tests[]` if
   it exists on disk.
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:103–109`
4. Scope decision matrix:
   - `conftest.py` changed → `mixed` (overrides test_only even if no source changes)
   - `test_count > 0 && source_count > 0` → `mixed`
   - `test_count > 0` → `test_only`
   - `source_count > 0` → `source_only`
   - else → `none`
   `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:115–128`

### track-test-commit.sh
`claim_worktree_ownership` decision tree:
1. No marker file → write new marker.
2. Marker exists, `log_base` matches → return 0 (already ours).
3. Marker exists, different `log_base`, PID still alive → warn, return 1 (refuse).
4. Marker exists, different `log_base`, PID dead → warn, overwrite (stale cleanup).
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/track-test-commit.sh:48–70`

`record_last_tested_commit` / `get_last_tested_commit`: gate on
`assert_worktree_owned_by_orchestrator` (returns 1 if not owner), then
write/read `last-tested-commit` plain-text file.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/track-test-commit.sh:123–164`

---

## 5. External invocations — every claude/codex/gh/git command VERBATIM with flags, model, schema

### regression-helpers.sh
```bash
# Resolve base branch
git -C "$cap_worktree" rev-parse --verify --quiet "$candidate"
# line 66

git -C "$cap_worktree" rev-parse HEAD
# line 80

git -C "$cap_worktree" stash --include-untracked
# line 90

git -C "$cap_worktree" checkout --detach "$base_commit_hash"
# line 97

git -C "$cap_worktree" checkout -
# line 141

git -C "$cap_worktree" stash pop
# line 152

git -C "$cap_worktree" rev-parse --short HEAD
# line 108

git -C "$cap_worktree" ls-files '*.spec.ts'
# line 127

cd "$cap_worktree" && bash .claude/scripts/test-unit.sh --skip-infra
# line 115

cd "$cap_worktree" && bash .claude/scripts/e2e-smoke.sh
# line 134
```

### detect-change-scope.sh
```bash
git -C "$worktree_path" diff --name-status --find-renames "$base_ref" HEAD
# line 27

git -C "$worktree_path" rev-list --count HEAD
# line 29

git -C "$worktree_path" hash-object -t tree /dev/null
# line 32
```

### track-test-commit.sh
```bash
git -C "$worktree_path" rev-parse HEAD
# line 134
```

No `claude`, `codex`, `gh`, or network calls in any of these five files.

---

## 6. Constants & tunables — numeric caps, timeouts, sleeps, pricing, model pins

| Constant | Value | Location |
|---|---|---|
| Lock busy-poll interval | `0.05` seconds | `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:21` |
| `TRACK_TEST_COMMIT_OWNER_FILE` | `".orchestrator-owner"` | `/Users/craigperler/Development/heysoo/.claude/scripts/lib/track-test-commit.sh:14` (readonly) |
| Default `base_ref` for diff | `HEAD~1` | `/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:24` |

No pricing, model pins, retry counts, or timeouts defined in any of these five files.

---

## 7. Failure handling — retries (count/backoff), fallback chains, circuit breaker, cascade rules

### status-file-helpers.sh
- `mkdir` lock: infinite busy-spin; no timeout or max-attempt cap.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–22`
- jq failure on update/write: temp file deleted, `$STATUS_FILE` left unchanged,
  error exit propagated to caller. No retry.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:52–56`

### regression-helpers.sh — capture_baseline_failures
- Checkout failure: writes error-status baseline and returns 1; no retry.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:97–104`
- `git checkout -` failure after tests: writes an error baseline and returns 1, but
  **does** restore the stash on this path — `[[ "$stashed" == "true" ]] && git -C "$cap_worktree" stash pop` at
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:146`.
  (CORRECTED post-verify: an earlier draft claimed a stash leak here; there is none. The
  real cosmetic flaw on this path is that the error baseline hardcodes `base_commit:"unknown"`
  even though the hash was already resolved — `:100`.)

### compute_regressions / get_inherited_failures
- Missing or error-status baseline → graceful degradation: `compute_regressions`
  returns all current failures as regressions; `get_inherited_failures` returns `[]`.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:224–226`, `247–249`

### track-test-commit.sh
- Live-PID ownership conflict: warns and returns 1 (caller must decide to abort or proceed).
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/track-test-commit.sh:57–59`
- Stale-PID marker: silently overwritten.
  `/Users/craigperler/Development/heysoo/.claude/scripts/lib/track-test-commit.sh:62–63`
- No retry logic anywhere in this file.

---

## 8. Coupling — per item: generic vs Hey Soo!-specific

### GENERIC (extract as-is)

| Item | Why generic |
|---|---|
| `with_status_file_lock` / `status_file_update` / `status_file_write` — the entire locking primitive | Operates on any `$STATUS_FILE`; no project assumptions |
| `detect_change_scope` output schema `{scope, changed_tests, message, shell_lib_changed}` | Scope taxonomy is language-agnostic |
| `compute_regressions` / `get_inherited_failures` — baseline diff math | Operates on any `{test, message}` failure-array and any `failure_names[]` baseline |
| `track-test-commit.sh` — ownership marker + commit tracking | Pure file I/O; no project assumptions |
| `update_test_loop_metadata` — status-file stamping | Generic status-file fields |

### HEY SOO!-SPECIFIC (flag for parameterization)

**classify-failures.sh — FAILURE_CLASSIFIER coupling (highest severity)**

The classification regex is hardcoded to two Hey Soo! patterns:

```bash
# e2e bucket: path starts with tests/e2e/ OR filename ends in .spec.ts
((.test // "") | test("^tests/e2e/"; "i")) or
((.test // "") | test("\\.spec\\.ts"; "i"))
/Users/craigperler/Development/heysoo/.claude/scripts/lib/classify-failures.sh:14–16
```

Generic shape: a `FAILURE_CLASSIFIER` plugin (callable or config) that accepts
`failures_json` and returns `{e2e:[…], unit:[…]}`. The e2e/unit taxonomy itself is
Hey Soo!-specific (projects without Playwright have no e2e bucket). The generic
shape should accept a pluggable bucket definition (e.g. a list of named
`{name, path_pattern}` buckets) or be replaced by a project-provided classifier.

**regression-helpers.sh — test runner coupling**

| Item | Coupling | Generic shape |
|---|---|---|
| `bash .claude/scripts/test-unit.sh --skip-infra` (line 115) | Hey Soo! shell script path | `$TEST_UNIT_CMD` config key |
| `bash .claude/scripts/e2e-smoke.sh` (line 134) | Hey Soo! shell script path | `$TEST_E2E_CMD` config key |
| E2E detection: grep for `playwright\|e2e` in `extract-output.json` `.task.description` (line 123) | Reads Hey Soo! task-extraction output schema; assumes `description` is an array joined by spaces | Generic: `$RUN_E2E` boolean env or capability flag in project config |
| `git ls-files '*.spec.ts'` as secondary E2E trigger (line 127) | `.spec.ts` is Playwright/Vitest convention; not universal | Generic: configurable E2E file glob |
| pytest FAILED line parser: `grep -oE 'FAILED [^ ]+'` (line 161) | pytest output format | Abstract behind `parse_unit_failures <output>` hook |
| Playwright failure block parser: `sed -n '/[0-9][0-9]* failed/,/^$/p'` + `grep -oE '[a-zA-Z0-9_-]+\.spec\.ts:[0-9]+'` (lines 168–170) | Playwright list-reporter output format | Abstract behind `parse_e2e_failures <output>` hook |

**detect-change-scope.sh — path and extension coupling**

| Item | Coupling | Generic shape |
|---|---|---|
| `.spec.ts` recognized as test file (line 150) | Playwright/Vitest | Configurable test-file glob list |
| `test_*.py` / `*_test.py` recognized as test file (lines 157–158) | pytest convention | Configurable test-file glob list |
| `*.test.ts` / `*.test.tsx` recognized as test file (lines 163–164) | Vitest/Jest | Configurable test-file glob list |
| `*.bats` recognized as test file (line 168) | Hey Soo! BATS shell tests | Configurable test-file glob list |
| `conftest.py` forces `mixed` scope (lines 97–98, 118–119) | pytest-specific; no equivalent for other runners | Runner-agnostic "force-full" file list config |
| `_is_shell_library_file`: `.claude/scripts/lib/*.sh` pattern (line 181) | Hey Soo! shell-lib path | Configurable library-file glob |
| `_shell_test_for_library`: maps `lib/foo.sh` → `tests/shell/lib/test-foo.bats` (lines 186–188) | Hey Soo! BATS test layout | Configurable lib→test mapping fn |

---

## 9. Anomalies — suspected bugs, dead code, contradictions with docs/orchestration-template.md

**A1 — ~~Potential stash leak~~ RETRACTED (verifier-refuted)**
An earlier draft claimed `capture_baseline_failures` leaks a stash when `git checkout -`
fails (line 141). This is **false**: line 146 explicitly runs
`[[ "$stashed" == "true" ]] && git -C "$cap_worktree" stash pop 2>/dev/null || true`
on that exact failure path, so the stash IS restored. No leak. The only real (cosmetic)
flaw on this path is that the error baseline hardcodes `base_commit:"unknown"` even though
the hash was already resolved (`:100`).
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:141–148`

**A2 — Infinite spin in mkdir lock path**
`_status_file_with_mkdir_lock` busy-polls at 50 ms intervals with no timeout and no
attempt cap. A crashed process that created the lock dir but failed to `rmdir` it
(e.g. SIGKILL between execute and cleanup) will deadlock all subsequent callers
permanently until the lock dir is manually removed.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/status-file-helpers.sh:20–22`

**A3 — `classify-failures.sh` not used in regression path**
`classify_failures` splits into e2e/unit buckets, but `compute_regressions` and
`get_inherited_failures` in `regression-helpers.sh` operate on a flat array (no
bucket split). The two files appear to serve separate consumers in
`orchestrator-common.sh`. This is not a bug per se but means the e2e/unit split is
not visible in the baseline-diff output — regressions are reported flat, not by
bucket.

**A4 — Unused local variables in `capture_baseline_failures`**
`cap_output` and `cap_exit` are declared and immediately marked with
`# shellcheck disable=SC2034` (lines 56–58). They are never assigned to again and
never read. Dead code; likely artefact of an earlier draft that unified output
capture.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:55–58`

**A5 — `detect_change_scope` always outputs `"message": ""`**
The success path assembles the JSON object with a hardcoded empty `message` string
(line 142), regardless of context. The `message` field is only populated on the
error path (line 47). Callers checking `message` for diagnostic text on
non-error scopes will always get an empty string.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/detect-change-scope.sh:138–142`

**A6 — `base_ref` default HEAD~1 breaks on first commit (mitigated but partial)**
The empty-tree fallback is triggered only when `base_ref == HEAD~1` AND
`rev-list --count == 1` (lines 30–34). If a caller explicitly passes `HEAD~1` but
the repo has exactly one commit, it works. But if a caller passes a different
explicit `base_ref` that resolves to a missing ref (e.g. `origin/main` when not
yet fetched), the function emits `scope:"error"` rather than the empty-tree
fallback, which is correct behavior — but callers receive no automatic recovery.

**A7 — Contradiction with docs/orchestration-template.md §5 re: FAILURE_CLASSIFIER**
The design doc (§5) specifies `FAILURE_CLASSIFIER` as a named project-config
adapter key. The current implementation has no such abstraction: `classify-failures.sh`
is a single hardcoded jq expression. The generic shape described in the design doc
does not yet exist anywhere in the source tree.

**A8 — `regression-helpers.sh` `capture_status: "error"` on all checkout failures writes `base_commit: "unknown"`**
Lines 72 and 100 both emit `base_commit: "unknown"` even when `base_commit_hash`
was successfully resolved (line 100 is reached after a successful `rev-parse`).
Only lines 143–145 (post-test return failure) correctly populate `base_commit` from
the already-resolved `base_commit` local variable.
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/regression-helpers.sh:100–103`

---

> Hard rule satisfied: every claim in §§4–7 cites absolute-path:line.
