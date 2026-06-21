# Fragment: orchestrator-common.sh (OWNER 2b — state, loops, lifecycle)
Source commit: 0dd5d09d641510ee595e0300f2e9422005194d58
Source file: `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`
Mapped lines (2b-owned defs): 553–614, 620–956, 965–1067, 1071–1316, 1324–1456, 1464–1493, 1673–1787, 3302–3537, 3588–4347

Scope note: this fragment owns the 2b half — status-file lifecycle, logging,
learnings/retrospective, iteration counters, convergence/plateau detection, the
quality loop, the test loop, review-scope evaluators (api-contract + e2e), resume
helpers, worktree claim, and GitHub issue/PR commenting. The 2a half (`run_stage`,
provider routing, capacity, cost, cleanup/traps, `triage_and_dispatch_e2e_fixes`,
setup/dependency stages) is documented in fragment 02a. All cross-references into 2a
are flagged in §8 (SEAM).

---

## 1. Role & entry points — who invokes it, with what argv

`orchestrator-common.sh` is a sourced library, not an executable. None of the 2b
functions has a CLI; they are called by `implement-orchestrator.sh` (the per-task
pipeline) and indirectly by `ralph-loop.sh`. Key 2b entry points and their callers
(caller cites are in `implement-orchestrator.sh`):

| Function | Defined | Invoked by (file:line) |
|---|---|---|
| `init_status` | 620 | implement-orchestrator (setup) |
| `set_stage_started` / `set_stage_completed` / `update_stage` | 709 / 723 / 682 | every stage boundary |
| `set_tasks` / `update_task` | 771 / 737 | implement stage, per-task loop |
| `set_final_state` | 793 | terminal paths (success + every failure) |
| `run_quality_loop` | 3302 | implement-orchestrator.sh:1137, :1200, :1282 |
| `run_test_loop` | 3588 | implement-orchestrator.sh:1472 |
| `evaluate_e2e_policy` | 1201 | implement-orchestrator.sh:1649, :1792 |
| `evaluate_api_contract_review_scope` | 1093 | implement-orchestrator.sh:1663, :1806 |
| `merge_e2e_policy_review_finding` | 1277 | implement-orchestrator.sh:1713, :1868 |
| `detect_convergence` | 985 | quality loop (self, :3451/:3461) + PR review (impl-orch:1946/:1953) |
| `review_has_only_minor_issues` | 965 | quality loop (self) + PR review (impl-orch:1725/:1923/:1926) |
| `detect_force_push_remediation` | 1036 | implement-orchestrator.sh:1309 |
| `validate_resume_status` / `load_resume_state` | 1324 / 1358 | resume path |
| `claim_orchestrator_worktree` | 1442 | setup stage |
| `comment_issue` / `comment_pr` / `upsert_pr_section` | 1673 / 1697 / 1731 | throughout |

`run_quality_loop` argv: `<worktree> <branch> [stage_prefix=main] [agent=python-backend-developer] [quality_tier=full]` (3302–3307).
`run_test_loop` argv: `<worktree> <branch> [agent=python-backend-developer]` (3588–3591).

## 2. Inputs — flags, env vars, files read

These functions take **no CLI flags**; they read globals set by the caller and JSON
state files. Globals consumed (name / default / effect):

- `EXECUTION_MODE` (full|lite|micro; default full): gates skip lists, status init
  shape, and test-loop iteration cap (553-cluster, 620, 3674–3682).
- `STATUS_FILE` (path): the task status JSON, target of all `status_file_update`/`status_file_write` calls.
- `LOG_BASE` (path): log dir; status mirror, convergence files, retrospective, context JSON live here.
- `LOG_FILE` (path): appended by `log`/`log_error` (556, 565).
- `TASK_ID` (issue/roadmap id): written to status `.issue`; used in prompts (620, 3377).
- `BASE_BRANCH` (default for diff base): `get_task_modified_files`, e2e/api evaluators, test loop.
- `REPO` (owner/name): `gh` calls in `comment_pr`/`upsert_pr_section` (1726, 1739).
- `STAGE_COUNTER`, `STAGE_COUNTER_FILE`: stage index, persisted by `set_stage_completed` (726–732) and restored by `load_resume_state` (1398–1400).
- Constants (readonly, lines 31–44): `MAX_QUALITY_ITERATIONS=5`, `MAX_TEST_ITERATIONS=10`,
  `MAX_CONSECUTIVE_INFRA_FAILURES=3`, `MAX_CONSECUTIVE_REGRESSION_PLATEAU=3`,
  `MAX_IDENTICAL_FAILURE_SIGNATURE=2`, `MAX_TASK_REVIEW_ATTEMPTS=3`, `MAX_PR_REVIEW_ITERATIONS=3`,
  `MAX_DISPATCHABLE_E2E_FAILURES=20`.

Files read: `$STATUS_FILE` (jq reads throughout), `$LOG_BASE/status.json` (resume),
`$LOG_BASE/context/extract-output.json` (test loop E2E trigger, 3652/4052),
`$LOG_BASE/context/convergence-<prefix>.txt` (quality loop, 3327), stage logs under
`$LOG_BASE/stages/*.log` (pattern detection, 894–895). Git is read (diff/rev-list/status)
in `get_task_modified_files`, `detect_force_push_remediation`, and the test loop.

## 3. Outputs — files written (every field), exit codes, side effects

### 3a. Status file `status-ralph.json` — full field registry

`init_status` (620–680) creates `$STATUS_FILE` via `status_file_write` with these
top-level fields:

- `state` (init `"initializing"`; later transitions to `running`, then a terminal
  value via `set_final_state`)
- `issue` = `$TASK_ID`
- `base_branch` = `$BASE_BRANCH`
- `branch` (init `""`, set by `set_worktree_info`)
- `worktree` (init `""`, set by `set_worktree_info`)
- `current_stage` (init `"setup"`)
- `substage` (init null; set/cleared by `set_substage`/`clear_substage`)
- `substage_detail` (init null)
- `current_task` (init null; set by `update_task`)
- `execution_mode` = `$exec_mode`
- `stages_skipped` (array; mode-dependent, 622–631)
- `stages_executed` (array; mode-dependent)
- `stages` (object, one key per pipeline stage — see below)
- `tasks` (init `[]`; populated by `set_tasks`, mutated by `update_task`)
- `quality_iterations` (init 0)
- `test_iterations` (init 0)
- `pr_review_iterations` (init 0)
- `last_update` (`now | todate`; rewritten by essentially every writer)
- `log_dir` = `$LOG_BASE`
- `stage_counter` (NOT in init; first written by `set_stage_completed` at 731)

`.stages.*` sub-objects written by `init_status` (658–670):
- `setup`: `{status, started_at, completed_at}`
- `research` / `evaluate` / `plan`: `{status (pending|skipped by mode), started_at, completed_at}`
- `implement`: `{status, task_progress ("0/0")}` — `task_progress` rewritten by `set_tasks` to `"0/<n>"` (778)
- `quality_loop`: `{status, iteration}` — `iteration` driven by `increment_quality_iteration` (915)
- `test_loop`: `{status, iteration, baseline_failures, last_regressions, last_inherited}` —
  `last_regressions`/`last_inherited` written by the full-suite gate (4132–4135)
- `docs`: `{status}`
- `pr`: `{status}` (`.stages.pr.pr_number` read on resume at 1408 — written by 2a/pr stage)
- `pr_review`: `{status, iteration}` — `iteration` from `increment_pr_review_iteration` (953)
- `complete`: `{status}`

Writers and exactly what they touch:
- `update_stage <stage> <status> [field value]` (682): `.stages[stage].status`,
  optional `.stages[stage][field]`, `.current_stage`, `.last_update`.
- `set_stage_started <stage>` (709): `.stages[stage].started_at`, `.stages[stage].status="in_progress"`,
  `.current_stage`, `.substage=null`, `.substage_detail=null`, `.state="running"`, `.last_update`.
- `set_stage_completed <stage>` (723): `.stages[stage].completed_at`, `.stages[stage].status="completed"`,
  `.stage_counter` (from counter file), `.last_update`.
- `update_task <id> <status> [attempts=0] [exit_code] [output_bytes]` (737): per-matched-task
  `.status`, `.review_attempts`, and optionally `.exit_code` / `.output_bytes`; plus top-level
  `.current_task`, `.last_update`.
- `set_tasks <json>` (771): `.tasks` (each task gets `status:"pending"` if missing — fixes
  infinite-retry on resume, comment at 773–774), `.stages.implement.task_progress`, `.last_update`.
- `set_worktree_info <wt> <br>` (783): `.worktree`, `.branch`, `.last_update`.
- `set_final_state <state>` (793): `.state`, `.last_update`; **then** triggers
  `emit_failure_retrospective` for failure states (see 3c, §7).
- `set_substage`/`clear_substage` (928/944): `.substage`, `.substage_detail`, `.last_update`.
- `increment_quality_iteration` (912): `.quality_iterations += 1`, mirrors to `.stages.quality_loop.iteration`.
- `increment_test_iteration` (920): `.test_iterations += 1`, mirrors to `.stages.test_loop.iteration`.
- `increment_pr_review_iteration` (950): `.pr_review_iterations += 1`, mirrors to `.stages.pr_review.iteration`.
- Test-loop inline write (4132–4136): `.stages.test_loop.last_regressions`, `.last_inherited`.

After **every** writer, `sync_status_to_log` (1464) copies `$STATUS_FILE` →
`$LOG_BASE/status.json` (guarded against self-copy, 1468). This mirror is the resume
source of truth.

### 3b. Other files written
- `$LOG_BASE/retrospective.md` — `emit_failure_retrospective` (816, 846).
- `$LOG_BASE/context/convergence-<prefix>.txt` — quality-loop fingerprint accumulator (3327).
- `$LOG_BASE/context/review-comments.json` — raw review results appended (3465).
- `$LOG_BASE/stages/.counter` — stage counter file restored on resume (1399–1400).
- `$LOG_BASE/.child_pids` — PID file reset on resume (1403–1404).

### 3c. Exit codes & side effects
- `run_quality_loop`: returns 0 on approve/skip; calls `exit 2` on cap breach
  (`set_final_state "max_iterations_quality"`, 3349–3350).
- `run_test_loop`: returns 0 on success; `exit 2` on any of:
  `max_iterations_test` (3763), `persistent_infra_failure` (3856/4103),
  `failure_signature_plateau` (3635), `regression_plateau` (4165), `tsc_gate_failure` (3746).
- Network/`gh` side effects: `comment_issue` (`gh issue comment`, 1691),
  `comment_pr` (`gh pr comment`, 1726), `upsert_pr_section` (`gh pr view` + `gh pr edit --body-file`, 1739/1781);
  test/quality loops emit many `comment_issue` calls.
- Git side effects: read-only (diff/rev-list/status/rev-parse); no commits/pushes from 2b.

## 4. Control flow — state machines, loops, exact caps & exit conditions

### 4a. `run_quality_loop` (3302–3537) — cap 5 (`MAX_QUALITY_ITERATIONS`, line 35)
`while [[ "$loop_approved" != "true" ]]` (3343). Each iteration: `increment_quality_iteration` (3345),
then cap guard at 3347–3351: if `loop_iteration > 5` → `set_final_state "max_iterations_quality"; exit 2`.
Per iteration: SIMPLIFY (skippable) → REVIEW → severity/convergence gates → FIX.

Tiers (3307–3324): `none` → return 0 immediately; `light` → review-only (`skip_simplify` stays
true every iteration); `full` → simplify first, skip simplify after a fix iteration (`skip_simplify`
toggled at 3527, reset at 3367).

Early-exit (auto-approve) conditions, in order:
1. Reviewer verdict `approved` (3435).
2. `review_has_only_minor_issues` — only suggestion-severity issues remain (3440).
3. Convergence: from iteration ≥ 3, if `detect_convergence` returns true (all current issues net-new) → auto-approve (3450–3458).
4. Fix produced no commits (HEAD unchanged) → auto-approve (3521–3524).

### 4b. `run_test_loop` (3588–4346) — base cap 10 (`MAX_TEST_ITERATIONS`, line 36)
`effective_max_test_iterations` (3674–3682): full=10; lite=2; **lite+E2E=3**.
Pre-loop TSC gate (3690–3755): if frontend changed, run `npx tsc --noEmit`; up to **2**
auto-fix attempts (3718), else `set_final_state "tsc_gate_failure"; exit 2` (3746).

`while [[ "$loop_complete" != "true" ]]` (3757). Each iteration: `increment_test_iteration` (3759);
cap guard 3761–3765: if `test_iteration > effective_max` → `set_final_state "max_iterations_test"; exit 2`.
Then PHASE 1 (incremental, only if changed test files) and PHASE 2 (full-suite gate, always),
then test validation.

- **Incremental phase** (3819–4009): run changed tests; infra-failure gate (reset + retry,
  cap `MAX_CONSECUTIVE_INFRA_FAILURES=3` → `persistent_infra_failure; exit 2`, 3853–3858);
  regression-aware (0 regressions ⇒ treated as pass); on regressions, signature-plateau check
  then `continue` (skips full suite, 3895/4002).
- **Full-suite gate** (4011–4263): infra-failure gate (same cap, 4100–4105); regression-aware
  (0 regressions ⇒ pass, 4138–4143); **regression plateau** (count unchanged across 3 consecutive
  full-suite runs → `regression_plateau; exit 2`, 4150–4167); signature-plateau check (4170);
  on regressions dispatch classified fixes (unit → python-backend-developer; e2e → 2a
  `triage_and_dispatch_e2e_fixes`) then `continue` (4262).
- **Validation** (4265–4343): if `passed`/`approved` → `loop_complete=true`; else fix and re-loop.

### 4c. `detect_convergence` (985–1022) — the convergence math
Extracts a fingerprint per issue = `(file // "unknown") + ":" + (description lowercased, whitespace-collapsed)`
(994–998). Counts how many current fingerprints already appear in the prior-issues file
(`grep -qF`, 1010). Appends current fingerprints for the next round (1016). Returns 0 (true =
"converging") **only if** `total_current > 0 && repeat_count == 0` — i.e. every current issue is
net-new and no prior issue is being re-flagged (1018). Returns 1 if there are repeats or no
issues. Quality loop only acts on it from iteration ≥ 3 (3450); PR review uses it too (impl-orch:1946).

### 4d. `check_failure_signature_plateau` (3611–3638, nested in `run_test_loop`) — CIRCUIT BREAKER
Computes `signature = jq '.[].test' | sort | shasum -a 256 | cut -d' ' -f1` over the **regressions**
JSON (3616) — a deterministic fingerprint of the *exact set* of failing test names. Compares to
`last_failure_signature`: if equal, `consecutive_identical_signature++`; else reset to 1 (3618–3624).
**Trip condition (3627):** when `consecutive_identical_signature >= MAX_IDENTICAL_FAILURE_SIGNATURE`
(=2, line 39) — i.e. the identical test-name set fails on two consecutive runs — it logs, posts a
`comment_issue` ("Failure Signature Plateau"), calls `set_final_state "failure_signature_plateau"`,
and `exit 2` (3635–3636). Called after both incremental (3895) and full-suite (4170) regressions.
Distinct from the count-based regression plateau (4150) which catches "same *count*, different tests";
the signature check catches "exact same tests" even when the fix agent swaps which tests fail.
Both counters reset on a clean/zero-regression run (4005–4007, 4172–4176).

### 4e. Resume (`validate_resume_status` 1324, `load_resume_state` 1358)
Validate: status file must exist; required fields `issue, branch, worktree, current_stage, log_dir`
present and non-null (1333–1342); `state != "completed"` (1347). Load: restores `TASK_ID, BASE_BRANCH,
BRANCH, WORKTREE, LOG_BASE`, sets `RESUME_STAGE/TASK/TASKS_JSON`, `EXECUTION_MODE`, the three
iteration counters, `STAGE_COUNTER` (+ counter file), resets PID file, and reads `RESUME_PR_NUMBER`
from `.stages.pr.pr_number` (1361–1408).

## 5. External invocations — verbatim commands

2b makes no direct `claude`/`codex` calls — all model work is delegated through the 2a `run_stage`
(see §8 SEAM). Direct external commands:

- `gh issue comment "$num" --body "$comment"` — `comment_issue` (1691).
- `gh pr comment "$pr_num" --repo "$REPO" --body "$comment"` — `comment_pr` (1726).
- `gh pr view "$pr_num" --repo "$REPO" --json body --jq '.body'` and
  `gh pr edit "$pr_num" --repo "$REPO" --body-file "$tmp_file"` — `upsert_pr_section` (1739, 1781).
- `git -C "$worktree" rev-list --count HEAD` / `status --porcelain` — `detect_force_push_remediation` (1052, 1061).
- `git -C "$worktree" diff --name-only "$base"..."$branch"` — `get_task_modified_files` (1076).
- `git -C "$loop_worktree" diff "$BASE_BRANCH...HEAD" --name-only` — test-loop E2E/frontend triggers (3649, 3665, 3693).
- `npx tsc --noEmit` (cd into `$frontend_dir`) — TSC gate (3701, 3731).
- `git -C "$loop_worktree" rev-parse HEAD` — quality loop no-op detection (3507, 3520) and test loop.

Schemas referenced through `run_stage` calls inside the loops:
`implement-issue-simplify.json` (3385), `implement-issue-review.json` (3422, 4300),
`implement-issue-fix.json` (3510, 3728, 3979, 4238, 4336).

## 6. Constants & tunables (lines 31–44, all `readonly`)

| Const | Value | Used at | Meaning |
|---|---|---|---|
| `MAX_QUALITY_ITERATIONS` | 5 | 3347 | quality loop hard cap |
| `MAX_TEST_ITERATIONS` | 10 | 3674/3761 | test loop cap (full); lite=2, lite+E2E=3 (3676–3681) |
| `MAX_CONSECUTIVE_INFRA_FAILURES` | 3 | 3853/4100 | halt on repeated infra crashes |
| `MAX_CONSECUTIVE_REGRESSION_PLATEAU` | 3 | 4160 | halt when regression count unchanged |
| `MAX_IDENTICAL_FAILURE_SIGNATURE` | 2 | 3627 | circuit-breaker: identical failing-test set |
| `MAX_TASK_REVIEW_ATTEMPTS` | 3 | 878 (pattern), impl-orch:1062 | per-task review retries |
| `MAX_PR_REVIEW_ITERATIONS` | 3 | (PR review, impl-orch) | PR review loop cap |
| `MAX_DISPATCHABLE_E2E_FAILURES` | 20 | 2a (`triage_and_dispatch_e2e_fixes`) | over this, suspect env |
| TSC gate auto-fix attempts | 2 | 3718 | inline literal, not a named const |

Convergence-detector minimum iteration before acting: **3** (literal, 3450). E2E-failure
trajectory warning uses last-2 comparison (3992, 4252).

## 7. Failure handling — retries, fallbacks, circuit breakers, cascades

- **Quality cap**: 5 iterations → `max_iterations_quality`, exit 2 (3347–3351).
- **Test cap**: 10/2/3 → `max_iterations_test`, exit 2 (3761–3765).
- **TSC gate**: 2 auto-fix attempts → `tsc_gate_failure`, exit 2 (3718–3748).
- **Infra circuit breaker**: 3 consecutive infra failures (either phase) → `persistent_infra_failure`,
  exit 2 (3851–3858, 4098–4105). Each infra failure triggers `reset_test_infrastructure` then `continue`.
- **Regression plateau**: 3 consecutive full-suite runs with unchanged regression *count* →
  `regression_plateau`, exit 2 (4150–4167).
- **Failure-signature plateau (circuit breaker)**: 2 consecutive runs with the identical failing
  test-name set → `failure_signature_plateau`, exit 2 (3611–3638). See §4d.
- **Regression-aware pass**: 0 regressions (all inherited from base) is treated as a pass in both
  phases (3879–3883, 4138–4143). Inherited failures are explicitly excluded from fix prompts
  (3908–3915, 4192–4200).
- **Force-push remediation** (`detect_force_push_remediation`, 1036): if HEAD commit count dropped
  AND tree is clean, the fix was history cleanup, not a real change — caller decrements the
  review-attempt counter (impl-orch:1309) so it isn't penalized.
- **Retrospective on failure**: `set_final_state` (793) routes the terminal states
  `halted|error|incomplete|blocked|max_iterations_*|failure_signature_plateau|regression_plateau|persistent_infra_failure|tsc_gate_failure`
  (802) into `emit_failure_retrospective` (814), which writes `retrospective.md`, calls
  `detect_failure_patterns` (870), and emits a cost summary (2a `emit_cost_summary`, 861). Re-entrant
  guard at 819. `detect_failure_patterns` scans for 4 signatures: exhausted review attempts,
  zero-output timeouts, fallback-model usage (greps stage logs), and dependency-skipped subtasks (876–908).

## 8. Coupling — generic vs Hey Soo!-specific

### 8a. SEAM — every 2b → 2a call
- `run_quality_loop` and `run_test_loop` call **`run_stage`** (2a, 2848) for every model
  step: quality simplify/review/fix (3385/3422/3510); test tsc-fix/unit-fix/validate/quality-fix
  (3728/3979/4238/4300/4336).
- `run_test_loop` → **`triage_and_dispatch_e2e_fixes`** (2a, 1863) for E2E fix dispatch (3985, 4246).
- `emit_failure_retrospective` → **`emit_cost_summary`** (2a, 2704) at 861.
- `claim_orchestrator_worktree` → `claim_worktree_ownership` (lib `track-test-commit.sh`:36) at 1450.
- Test loop sources lib helpers (3543–3565): `regression-helpers.sh` (`compute_regressions`,
  `get_inherited_failures`), `classify-failures.sh` (`classify_failures`), `detect-change-scope.sh`,
  `run-tests-direct.sh` (`run_tests_direct`), `reset-infra.sh` (`reset_test_infrastructure`),
  `select-incremental-test-agent.sh`, `track-test-commit.sh` (commit tracking). These are
  out-of-fragment but invoked from 2b loops.
- All status writers depend on `status_file_update`/`status_file_write` (lib
  `status-file-helpers.sh`:59/78) — the locked-write primitive.

### 8b. Generic (extract ~as-is, parameterize)
- Status lifecycle (`init_status`/`update_*`/`set_*`), `sync_status_to_log`, iteration counters,
  resume helpers, `detect_convergence`, `check_failure_signature_plateau`, regression/infra plateau
  logic, `comment_issue`/`comment_pr`/`upsert_pr_section`, `detect_force_push_remediation`,
  `get_task_modified_files`, retrospective machinery. The generic shape: loop caps + plateau
  thresholds become config; `gh` commenting becomes an optional "progress reporter" adapter;
  the convergence/plateau detectors are project-agnostic and should be the engine's adaptive-cap
  mechanism (per design doc §5).

### 8c. Hey Soo!-specific (needs adapter)
- **`evaluate_api_contract_review_scope` (1093)** — hardcoded path regexes: `^frontend/src/lib/api/`,
  `^frontend/src/types/(api|chat|recipe|stream).ts$`, `^frontend/src/hooks/use[A-Z]`,
  `^lambda/[^/]+/handler.py$`, `.../*models*.py`, `validation*.py`, `*schema*.py`; plus a hardwired
  service-name remap (chat/suggest/stream→"suggest"; library/admin/share→"library", 1136–1146).
  **Hey Soo!-coupling: api-contract.** Generic shape: a `CONTRACT_REVIEW_CLASSIFIER` hook returning
  `{frontend_files, backend_files, shared}`.
- **`evaluate_e2e_policy` (1201)** — hardcoded `^frontend/src/(pages|components|...)`,
  `^tests/e2e/.*.spec.ts$`, and `^lambda/(suggest|library)/handler.py$`. **Hey Soo!-coupling: E2E-policy.**
- **`merge_e2e_policy_review_finding` (1277)** — injects a `severity:"critical", category:"e2e_policy"`
  issue demanding a `tests/e2e/*.spec.ts` change. **Hey Soo!-coupling: E2E-policy.** Generic shape:
  a pluggable post-review "policy gate" list.
- Test loop hardcodes: `tests/e2e/*.spec.ts`, `frontend/tsconfig.json`/`npx tsc`,
  reproducer commands `bash .claude/scripts/test-shell.sh` / `uv run pytest` (3942–3945),
  `^infra/` skip logic (4023), `.claude/scripts/lib/` shell-suite trigger (3811).
- Default agents: `python-backend-developer`, `code-simplifier`, `code-reviewer`,
  `bulletproof-frontend-developer` (3306, 3385, 3422, 3728) — the agent roster is repo-specific.
- `comment_pr` attribution string `implement-issue-orchestrator` (1713) and the `is_github_issue`
  roadmap-vs-issue branch (1678, 1705) are Hey Soo! conventions.

## 9. Anomalies — suspected bugs, dead code, contradictions vs design doc

1. **Stale model pins (DOC-CONFIRMED).** `MODEL_CHAIN=("claude-opus-4-7" "claude-sonnet-4-6"
   "claude-haiku-4-5-20251001")` (49). Design doc §2.3 flags `opus-4-7` as stale (4.8 current);
   matches the doc's complaint. (Cosmetic to 2b — used by 2a, but lives in the shared header.)
2. **Loop caps match the doc's "fixed worst-case" critique.** quality=5, test=10 (35–36) are
   exactly the `quality 5×3 / test 10×2` framing the design doc §2.2/§5 wants replaced with adaptive
   caps. NOTE the doc says "test 10×2" — the **×2** is the lite-mode cap (3680), not a sub-iteration
   multiplier; full mode is a flat 10. The convergence detector (the doc's proposed adaptive
   mechanism) **already exists** (`detect_convergence`) but is only wired into the quality loop and
   PR review — **not** the test loop, which relies purely on plateau circuit-breakers. Worth flagging
   as the half-built version of the design-doc goal.
3. **`update_stage` writes extra fields as strings only.** The optional `[field value]` path uses
   `--arg` (694), so any numeric extra field is stored as a JSON string. Minor type-fidelity bug.
4. **`baseline_failures` is initialized (665) but never updated by any 2b writer** — only
   `last_regressions`/`last_inherited` are written (4132–4135). Appears to be a vestigial/dead status
   field. DISPUTED — could be written by 2a or a lib helper not in this fragment; flagged below.
5. **`upsert_pr_section` masks errors as success.** On `gh pr view`/`gh pr edit` failure it logs and
   `return 0` (1742, 1784) — callers cannot detect a failed PR-body update. Intentional best-effort,
   but silently lossy.
6. **`comment_pr` attribution hardcodes `implement-issue-orchestrator`** (1713) while `init`/design
   docs elsewhere call the entry point `implement-orchestrator.sh` — naming drift, cosmetic.
7. **TSC-gate auto-fix attempt count (2) is a bare literal** (3718), not a named const like every
   other cap — inconsistency that complicates the design-doc goal of one config table.

DISPUTED (could not fully confirm within 2b's mapped lines):
- Whether `.stages.test_loop.baseline_failures` is ever written (anomaly #4) — no 2b writer touches
  it; needs a cross-check against 2a / `regression-helpers.sh` (which does call `status_file_update`
  at line 36 of that helper).
- `MAX_PR_REVIEW_ITERATIONS` (41) and `MAX_TASK_REVIEW_ATTEMPTS` enforcement live in
  `implement-orchestrator.sh` (impl-orch:1062), outside this file; only `increment_pr_review_iteration`
  (the counter) is 2b. The actual PR-review loop body and its convergence wiring (impl-orch:1946) are
  in the per-task pipeline fragment, not here.
