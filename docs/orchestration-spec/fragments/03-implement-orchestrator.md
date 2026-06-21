# Fragment: .claude/scripts/implement-orchestrator.sh
Source commit: 0dd5d09 (file last touched 987c02f)   Mapped lines: 1–2218 (full file)

Reference path is read-only: `/Users/craigperler/Development/heysoo/.claude/scripts/implement-orchestrator.sh`.
Shared engine functions cited as `lib/orchestrator-common.sh:<line>` =
`/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`.

This script is the **per-task pipeline entry point**. It owns argv parsing,
resume bootstrap, the main() stage sequencer, and (critically) the **stage prompt
text** for every LLM stage. It does NOT own `run_stage`/model-tiering/provider
routing — those live in the shared engine and are documented here only where they
determine routing.

---

## 1. Role & entry points — who invokes it, with what argv

`main "$@"` at `:2217` is the sole entry; `main()` defined `:358`. Invoked directly
by an operator or by the batch scheduler (`ralph-loop.sh`, per docs).

Usage forms (`usage()` `:60`–`:94`):
- `--task <id> --branch <name> [opts]` — roadmap task id (e.g. `1.5.2`) or `#N` (`:103`).
- `--issue <N> --branch <name>` — back-compat; sets `TASK_ID="#N"` (`:98`–`:100`).
- `--task <id> --branch <name> --lite` (`:148`) / `--micro` (`:152`).
- `--resume [--status-file <path>]` (`:127`) / `--resume-from <log-dir>` (`:131`).
- Per-task provider tag: `--task "82:codex"` parsed by `parse_task_tags` (`:105`);
  mode can also be tagged via `PARSED_MODE` (`:108`).

Validation (`:174`–`:181`): non-resume runs require both `TASK_ID` and `BASE_BRANCH`.
Exit 3 on any argv error (`:93`, `:99`, etc.).

---

## 2. Inputs — every flag, env var, file read

### Flags (argv loop `:96`–`:172`)
| Flag | Var set | Default | Effect |
|------|---------|---------|--------|
| `--issue N` | `TASK_ID="#N"` | — | GitHub-issue mode (`:100`) |
| `--task <id>` | `TASK_ID` (normalized), `TASK_PROVIDER`, `EXECUTION_MODE` | — | `parse_task_tags` strips `:codex`/mode tags (`:105`–`:108`) |
| `--branch <name>` | `BASE_BRANCH` | — | base for PR (`:113`) |
| `--agent <name>` | `AGENT` | `""` | default agent for setup/research/evaluate/plan/docs/test/fix stages (`:118`) |
| `--status-file <path>` | `STATUS_FILE`, `STATUS_FILE_USER_SET=true` | `status.json` then auto-scoped | (`:123`–`:124`) |
| `--learnings-file <path>` | `LEARNINGS_FILE` | `""` | prepend learnings to every stage prompt (`:140`; consumed at engine `:2920`) |
| `--baseline-file <path>` | `BASELINE_FILE` | `""` | use pre-computed baseline failures, skip per-task capture (`:145`, `:616`) |
| `--lite` | `EXECUTION_MODE=lite` | `full` | (`:149`) |
| `--micro` | `EXECUTION_MODE=micro` | `full` | (`:153`) |
| `--provider claude\|codex` | `TASK_PROVIDER` | `claude` | rejects other values, exit 3 (`:158`–`:161`) |
| `--resume` | `RESUME_MODE=status` | — | (`:128`) |
| `--resume-from <dir>` | `RESUME_MODE=logdir`, `RESUME_LOG_DIR` | — | (`:133`–`:134`) |
| `--help`/`-h` | — | — | `usage`, exit 3 (`:165`) |

### Env vars
- `TASK_PROVIDER` — **exported** `:183` so the engine's `should_use_codex` sees it.
- `ORCHESTRATOR_PROVIDER` — NOT read here; read by engine (`lib/...:174`). Global
  wholesale Codex flip. (Two-axis routing — see §Routing Table.)
- Engine-side caps (`MAX_*`, `STAGE_TIMEOUT*`) are `readonly` in the engine, not set here.

### Files read
- `extract-roadmap-task.sh` output JSON (`:492`) → `$LOG_BASE/context/extract-output.json`.
- `$BASELINE_FILE` copied to `$LOG_BASE/context/baseline-failures.json` (`:619`).
- `$STATUS_FILE` (resume: `load_resume_state`, plus task-status jq reads throughout).
- `$LOG_BASE/context/extract-output.json` re-read for closed-issue gate (`:522`).
- Schema files live in `$SCRIPT_DIR/schemas/*.json` (validated by engine `run_stage`).

---

## 3. Outputs — files written, exit codes, side effects

### Files written
- `status-task-<id>.json` / `status-issue-<N>.json` (auto-scoped `:293`–`:299`),
  or user `--status-file`. Copied to `$LOG_BASE/status.json` at end (`:2195`).
- `$LOG_BASE = logs/implement-roadmap-task/{task-<id>|issue-<N>}-<ts>/` (`:287`–`:290`).
  - `stages/NN-<stage>.log` + `.stderr` + `.stream.jsonl` (engine-written).
  - **`stages/index.md`** (`STAGE_INDEX` `:347`) — markdown table header written here
    `:349`–`:351`; one row appended per stage by engine `:2952`–`:2956` with columns
    `# | Stage | Agent | Provider | Model | Timestamp | Log file`.
  - `stages/.counter` (file-based stage counter, survives subshells `:331`).
  - `.child_pids` (PID tracking for force-kill cleanup `:335`).
  - `context/{extract,research,evaluate,plan,setup}-output.json`, `context/tasks.json`,
    `context/baseline-failures.json`, `context/convergence-pr-review.txt` (`:1762`).
- `docs/plans/*.md` committed + pushed at completion (`:2165`–`:2188`).

### Exit codes
| Code | Meaning | Lines |
|------|---------|-------|
| 0 | success / already-closed / no-changes / PR-skipped | `:528`,`:539`(typo: see §9),`:1612`,`:2213` |
| 1 | generic error (setup, plan, blocked eval, PR, incomplete completion gate) | `:558`,`:747`,`:842`,`:1366`,`:1619`,`:2035` |
| 2 | fatal halt: impl timeout/empty-output (no committed work), build fail after 3 fixes, docs fail, max PR-review iters | `:1165`,`:1444`,`:1523`,`:1787` |
| 3 | argv/usage error | `usage`/argv branches |
| 4 | extract stage failed | `:502` |

### Side effects
- **git**: worktree create (engine `run_setup_stage`), `rev-parse`/`rev-list`/`log`
  reads, `push origin <branch>` (`:1749`,`:2001`), plan-file commit+`push origin main`
  (`:2183`).
- **gh**: `gh pr create`/comment via PR-stage agent prompt (`:1594`); `gh issue close`
  (`:2157`); issue/PR comments via `comment_issue`/`comment_pr`/`upsert_pr_section`.
- **network**: every `claude -p` / `codex exec` invocation (engine), npm/uv builds.
- **frontend build**: `npm run build` in worktree (`:601`) + frontend validation (`:1402`).

---

## 4. Control flow — the stage sequence (state machine)

`main()` runs a **fixed linear sequence of guarded blocks** (not a dispatch table).
Each block: `skip_stage`/resume-guard → `set_stage_started` → work → `set_stage_completed`.
Order as executed in `main()`:

| # | Stage | Lines | Guard / skip | full | lite | micro |
|---|-------|-------|-------------|------|------|-------|
| 0 | **extract** | `:472`–`:514` | resume `is_stage_completed` | ✅ | ✅ | ✅ |
| — | closed-issue early-exit | `:516`–`:530` | github-issue only; exit 0 `already_closed` | ✅ | ✅ | ✅ |
| 1 | **setup** | `:532`–`:641` | resume-guard | worktree | worktree | **branch-only** (`run_setup_stage_micro` `:548`) |
| — | frontend build | `:589`–`:608` | inside setup | ✅ | ✅ | ⊘ micro (`:593`) |
| — | baseline capture | `:610`–`:640` | inside setup | ✅ | ✅ | ⊘ micro (`:614`) |
| 2 | **research** | `:643`–`:674` | `skip_stage "research"` | ✅ | ⊘ | ⊘ |
| 3 | **evaluate** | `:676`–`:754` | `skip_stage "evaluate"` | ✅ (can exit 1 `blocked` `:747`) | ⊘ | ⊘ |
| 4 | **plan** | `:756`–`:878` | `skip_stage "plan"` → synthetic 1-task list (`:778`) | ✅ | ⊘ (synthetic) | ⊘ (synthetic) |
| 5 | **implement** (per-task loop) | `:880`–`:1377` | resume `is_stage_completed` | review+quality loop | single-pass | single-pass |
| — | post-impl frontend build validation | `:1379`–`:1459` | resume-guard | ✅ | ✅ | ⊘ micro (`:1383`) |
| 6 | **test_loop** | `:1461`–`:1477` | `skip_stage "test_loop"` | ✅ (cap 10) | ✅ (cap 2, or 3 w/E2E) | ⊘ |
| — | **verify** (tests/verify/*.sh) | `:1479`–`:1497` | resume-guard; non-fatal | ✅ | ✅ | ✅ |
| 7 | **docs** | `:1499`–`:1527` | `skip_stage "docs"` | ✅ | ⊘ | ⊘ |
| — | no-commits early-exit | `:1529`–`:1540` | exit 0 `no_changes` | ✅ | ✅ | ✅ |
| 8 | **pr** | `:1542`–`:1631` | resume-guard | ✅ | ✅ | ✅ |
| 9 | **pr_review** | `:1633`–`:2007` | resume-guard | spec+code loop (cap 3) | code-only single-pass | code-only single-pass |
| 10 | **complete** | `:2009`–`:2192` | completion gate + resume-guard | ✅ | ✅ | ✅ |

**quality_loop** is NOT a top-level block — it runs *inside* implement per-task via
`run_quality_loop` (`:1137`,`:1200`,`:1282`) and is marked completed at `:1371` only in
full mode. `skip_stage "quality_loop"` is in both LITE/MICRO skip lists
(`lib/...:582`,`:585`) so quality never runs in lite/micro.

### Skip mechanism
`skip_stage <name>` (`lib/...:590`) returns true if `<name>` ∈ the active mode's list:
- `LITE_SKIP_STAGES=(research evaluate plan quality_loop docs)` (`lib/...:582`)
- `MICRO_SKIP_STAGES=(research evaluate plan quality_loop test_loop docs)` (`lib/...:585`)

### Implement per-task loop (`:911`–`:1345`)
For each task in `tasks_json`:
1. **Dependency gate** (`:922`–`:946`): if any `depends_on` id ∈ `failed_task_ids`,
   mark `skipped_due_to_dependency`, add self to `failed_task_ids`, `continue`.
2. **Resume recovery** (`:948`–`991`): completed→skip; `timed_out`→git-grep for
   committed work, promote to completed if found.
3. **lite/micro branch** (`:997`–`1047`): single `run_stage` implement, no review, no quality.
4. **full branch** (`:1048`–`1338`): review-while-loop, cap `MAX_TASK_REVIEW_ATTEMPTS=3`
   (`:1062`). Inner flow: implement→(git-aware timeout recovery `:1106`)→
   no-op auto-approve (`:1180`)→review (`spec-reviewer`)→ if `passed` run quality loop;
   else fix (`:1303`) + force-push-remediation detection (`:1309`, decrements attempt).
5. Exhausted attempts → mark `failed`, record in `failed_task_ids` (`:1317`–`:1336`).
6. **Completion gate** (`:1347`–`:1367`): any task not `completed` → exit 1 `incomplete`.

### PR review loop (full, `:1778`–`:2003`)
`while pr_approved != true`: cap `MAX_PR_REVIEW_ITERATIONS=3` (`:1783`, exit 2 `max_iterations_pr_review`).
Per iter: spec-review (`spec-reviewer`) → code-review (`code-reviewer`) → both approved? done;
else severity-gate auto-approve (only-suggestion `:1930`), convergence auto-approve
(`detect_convergence`, iter≥2 `:1945`), else fix (`$AGENT`) + push.

### Completion gate (`:2009`–`2036`)
Auto-promote null/pending tasks if implement/pr/pr_review passed (`:2019`); then any
non-completed task → exit 1 `incomplete` (`:2035`).

---

## 5. External invocations — verbatim

This script never calls `claude`/`codex` directly; it calls `run_stage`
(`lib/...:2848`) / `run_stage_with_timeout` (`lib/...:89`). The actual command
(engine `:2421`–`:2428`):
```
env -u CLAUDECODE "$TIMEOUT_CMD" --kill-after=10 "$timeout_val" claude -p "$prompt" \
    ${agent_args[@]+"${agent_args[@]}"} \
    --model "$current_model" --dangerously-skip-permissions --verbose \
    --output-format stream-json --json-schema "$schema"
```
where `agent_args=(--agent "$agent")` iff agent non-empty (`lib/...:2932`), model from
`get_stage_model` (`lib/...:55`), and the codex path is taken when `should_use_codex`
(`lib/...:191`) is true (`run_codex_stage`, not in this file).

### run_stage call-site inventory (stage → schema → agent → prompt source)
| Stage name (literal/pattern) | Schema | Agent (`--agent`) | Prompt var | Prompt line |
|------|--------|-------|------------|-------------|
| `research` | `implement-issue-research.json` | `$AGENT` | `research_prompt` | `:653`–`:665`, call `:668` |
| `evaluate` | `implement-issue-evaluate.json` | `$AGENT` | `evaluate_prompt` | `:686`–`:699`, call `:702` |
| `plan` | `implement-issue-plan.json` | `$AGENT` | `plan_prompt` | `:788`–`:829`, call `:832` |
| `implement-task-<id>` (lite/micro) | `implement-issue-implement.json` | `$task_agent` | `impl_prompt` | `:1001`–`:1007`, call `:1010` |
| `implement-task-<id>` (full) | `implement-issue-implement.json` | `$task_agent` | `impl_prompt` | `:1080`–`:1085`, call `:1091` (`run_stage_with_timeout`) |
| `task-review-<id>-attempt-N` | `implement-issue-task-review.json` | `spec-reviewer` | `review_prompt` | `:1212`–`:1223`, call `:1226` |
| `tsc-fix-task-<id>` | `implement-issue-fix.json` | `bulletproof-frontend-developer` | `tsc_fix_prompt` | `:1259`–`:1264`, call `:1265` |
| `fix-task-<id>-attempt-N` | `implement-issue-fix.json` | `$task_agent` | `fix_prompt` | `:1296`–`:1301`, call `:1303` |
| `build-fix-attempt-N` | `implement-issue-fix.json` | `bulletproof-frontend-developer` | `build_fix_prompt` | `:1417`–`:1422`, call `:1424` |
| `docs` | `implement-issue-implement.json` | `phpdoc-writer` | `docs_prompt` | `:1509`–`:1513`, call `:1517` |
| `pr` | `implement-issue-pr.json` | *(none → default)* | `pr_prompt` | `:1586`–`:1598`, call `:1601` |
| `code-review-lite` | `implement-issue-review.json` | `code-reviewer` | `code_prompt` | `:1691`–`:1709`, call `:1712` |
| `fix-pr-review-lite` | `implement-issue-fix.json` | `python-backend-developer` | `fix_prompt` | `:1734`–`:1739`, call `:1742` |
| `spec-review-iter-N` | `implement-issue-review.json` | `spec-reviewer` | `spec_prompt` | `:1838`–`:1864`, call `:1867` |
| `code-review-iter-N` | `implement-issue-review.json` | `code-reviewer` | `code_prompt` | `:1885`–`:1901`, call `:1904` |
| `fix-pr-review-iter-N` | `implement-issue-fix.json` | `$AGENT` | `fix_prompt` | `:1974`–`:1984`, call `:1987` |
| `complete` | `implement-issue-complete.json` | *(none → default)* | `complete_prompt` | `:2050`–`:2083`, call `:2086` |

Note `setup` does NOT call `run_stage` — it is pure shell (`run_setup_stage` /
`run_setup_stage_micro`, comment `:533` "no LLM call, see issue #227"). Likewise the
test_loop body (`run_test_loop` `:1472`) and quality loop (`run_quality_loop`) issue
their own `run_stage` calls inside the engine, not here.

### Verbatim load-bearing prompt excerpts
- **plan** references a skill (`:794`): `"1. Write a detailed implementation plan using writing-plans skill"` — and defines the `quality_tier` (full/light/none `:799`–`:802`), `implementation_budget` (standard/short `:805`–`:816`), and `depends_on` (`:818`–`:821`) task-tagging contract the rest of `main()` reads back.
- **plan** E2E policy (`:823`–`:826`): "Do NOT create a separate task for E2E/Playwright tests… each implementation task that adds or changes user-facing flows must include its own E2E specs."
- **implement (full)** injects `policy_reminders` (`:1066`–`:1077`): frontend agents get an E2E/Playwright reminder + `bash .claude/scripts/e2e-smoke.sh`; all agents get a docs/ADR + "Do NOT modify docs/roadmap.md — it is deprecated; tasks are tracked via GitHub Issues" reminder.
- **complete** (`:2050`–`:2083`): 3 mandated steps — file follow-up issues (`follow_up_issues`), answer Innovation + Process reflection questions (`innovation_brainstorm`/`orchestration_retrospective`), produce `pipeline_notes` for the PR.
- **pr** (`:1586`): instructs `gh pr create --base $BASE_BRANCH --title 'feat(task-...)'`; returns `{"status":"skipped"}` if 0 commits ahead.

### gh / git invoked directly in this file
- `gh issue close "$(issue_number)" --comment "Implemented in PR #$pr_number"` (`:2157`).
- `git -C "$worktree" push origin "$branch"` (`:1749`,`:2001`); `git add/commit/push origin main` for plan files (`:2167`–`:2183`).

---

## 6. Constants & tunables

Numeric caps are mostly `readonly` in the engine, consumed here:
| Name | Value | Where used here |
|------|-------|------------------|
| `MAX_TASK_REVIEW_ATTEMPTS` | 3 (`lib/...:34`) | implement review loop `:1062`,`:1293` |
| `MAX_PR_REVIEW_ITERATIONS` | 3 (`lib/...:41`) | PR review loop `:1783` |
| `MAX_TEST_ITERATIONS` | 10 (`lib/...:36`); lite cap 2, or 3 w/E2E (`lib/...:3674`–`:3681`) | engine test loop |
| `MAX_QUALITY_ITERATIONS` | 5 (`lib/...:35`) | engine quality loop |
| `STAGE_TIMEOUT_INITIAL` | 1800s (`lib/...:25`) | standard impl budget `:1087`→`get_subtask_implementation_timeout` |
| `SUBTASK_STAGE_TIMEOUT_SHORT` | 900s (`lib/...:29`) | `short` budget |
| `MAX_STAGE_RETRIES` / `RETRY_COOLDOWN` | 2 / 120s (`lib/...:31`,`:32`) | engine retry |
| frontend build-fix attempts | **3** (literal, `:1413`) | post-impl build loop |
| tsc-fix | 1 attempt + 1 verify (`:1256`–`:1268`) | per-frontend-task |
| `MODEL_CHAIN` | `claude-opus-4-7` → `claude-sonnet-4-6` → `claude-haiku-4-5-20251001` (`lib/...:49`) | fallback |

`get_stage_model` tiers (`lib/...:55`–`:71`): setup→haiku;
research/plan/`implement-task-*`/`fix-*`→opus-4-7; evaluate/`*-review-*`/`simplify-*`/
`test-*`/docs/pr/complete→sonnet-4-6; default→opus.

---

## 7. Failure handling

- **extract fail** → exit 4 (`:502`).
- **setup fail** → exit 1 (`:558`).
- **evaluate blocked** (status≠success) → comment + exit 1 `blocked` (`:746`–`:747`).
- **implement timeout/empty_output**: git-aware recovery — if `check_git_for_committed_work`
  finds commits, treat success + run quality loop (`:1113`–`:1139`); else fatal exit 2
  with boxed diagnostics (`:1142`–`:1165`).
- **implement plain failure** → mark `failed`, break review loop (`:1168`–`:1170`).
- **review-attempt exhaustion** → mark `failed`, record for dependency skip; **does NOT
  halt** — independent subtasks still attempted (`:1316`–`:1336`).
- **dependency cascade**: failed/skipped subtask ids in `failed_task_ids`; downstream
  subtasks whose `depends_on` intersects are skipped (`:925`–`:945`), not run on broken base.
- **frontend build fail** → up to 3 auto-fix attempts (`:1413`), then fatal exit 2 (`:1444`).
- **docs fail** (exit≠0) → fatal exit 2 (`:1523`).
- **PR fail** → exit 1; **PR skipped** (0 commits) → exit 0 `no_changes` (`:1607`–`:1619`).
- **PR review max iters** → exit 2 `max_iterations_pr_review` (`:1787`).
- **PR review auto-approve escape hatches**: only-suggestion severity gate (`:1930`),
  convergence detection on iter≥2 (`:1945`), force-push remediation un-counts a retry (`:1309`).
- **completion gate** (defense-in-depth): incomplete tasks → exit 1 `incomplete`
  (`:1366`,`:2035`).
- Retry/backoff/rate-limit fallback chain lives in the engine `run_stage`, not here.

---

## 8. Coupling — generic vs Hey Soo!-specific

**Generic pipeline skeleton (extract ~as-is):**
- The 11-block linear sequencer, resume guards, `skip_stage`/lite/micro mode machinery,
  stage-index/log layout, status-file state model, dependency-gated per-task loop,
  completion gate, two-axis provider routing wiring (`export TASK_PROVIDER`).
- Stage prompts that are project-agnostic: research, evaluate, plan (minus skill name),
  implement, generic review/fix, complete.

**Hey Soo!-specific (needs adapter/parameterization):**
- **Agent names hardcoded**: `python-backend-developer`, `bulletproof-frontend-developer`
  (`:772`,`:795`,`:1066`), `spec-reviewer`, `code-reviewer`, `phpdoc-writer` (`:1517` — note:
  PHP-named agent writing **Python** docstrings, see §9). Generic shape: an agent-role
  registry mapped per project.
- **Frontend stack assumptions**: `npm run build` (`:601`,`:1402`), `frontend/package.json`
  (`:1253`,`:1400`), `npx tsc --noEmit` TS check (`:1256`), `dist/config.json` CDK guard
  (`:1451`). Generic shape: a pluggable build/typecheck hook.
- **E2E policy**: `e2e-smoke.sh` (`:1070`), `tests/e2e` Playwright coverage clauses
  (`:1696`,`:1850`), `evaluate_e2e_policy`/`merge_e2e_policy_review_finding`. Hey Soo! CLAUDE.md policy.
- **API-contract review**: `evaluate_api_contract_review_scope` + the frontend/backend
  `extra="forbid"` Pydantic-shaped contract clause (`:1678`,`:1821`). Specific to a
  React-frontend + Python-Lambda contract.
- **Docs stage**: Python-only docstrings, scoped to `^(lambda|infra)/` (`:1511`). Lambda/CDK layout.
- **Baseline capture / test classification**: ADR-035 references; `lambda`/`infra` path filters.
- **Roadmap/issue duality**: `extract-roadmap-task.sh`, `docs/roadmap.md` deprecation
  (`:1077`), `docs/decisions/ADR-NNN.md` convention (`:1076`).

---

## 9. Anomalies / disputes vs docs/orchestration-template.md

- **"11 stages" claim** (template `:35`–`:36`: setup→research→evaluate→plan→implement→
  quality_loop→test_loop→docs→pr→pr_review→complete). In the actual `main()`:
  - `extract` is a real first stage (`:472`) the template list omits.
  - `quality_loop` is **not** a standalone stage — it runs *inside* implement per-task
    and is merely flag-marked completed (`:1371`). `verify` is a real (unlisted) stage (`:1479`).
  So the executed sequence is closer to **extract → setup → research → evaluate → plan →
  implement(+quality) → test_loop → verify → docs → pr → pr_review → complete** (~12–13
  guarded blocks), not a clean 11. DISPUTED with the template's tidy count.
- **Stale model pins**: stages pin `claude-opus-4-7` / `claude-sonnet-4-6` /
  `claude-haiku-4-5-20251001` (`lib/...:49`,`:60`–`:66`). Template `:84`–`:86` flags
  stale pins / ~3× overstated Opus pricing in `emit_cost_summary` (`:2211`). Consistent
  with the template's "stale pricing and model pins" improvement area.
- **`phpdoc-writer` agent writes Python docstrings** (`:1517`) for a prompt that says
  "Write docstrings for all modified Python files" (`:1509`). Almost certainly a copy/paste
  carryover from a PHP project; mismatched agent name. SUSPECTED BUG.
- **micro early-exit uses exit 0 path but the success-exit guard** `set_normal_exit; exit 0`
  appears at `:528`/`:1539`/`:1612`/`:2213`. The closed-issue branch (`:528`) and no-changes
  branch (`:1539`) both early-return success — fine, noting they bypass PR/complete entirely.
- **Lite-mode `quality_loop` completion flag**: `set_stage_completed "quality_loop"` only
  fires in full mode (`:1370`–`:1372`); in lite/micro the stage is never marked, relying on
  `skip_stage` keeping it out of the resume path. Consistent but fragile coupling between
  the skip list and the completion-flag guard.
- **`--issue`/`--task` mode interaction with `--lite`/`--micro`**: mode can be set three
  ways (`--lite`/`--micro` flags, or `PARSED_MODE` from the task tag `:108`). No conflict
  resolution if both a tag mode and a flag disagree — last writer wins (flags parsed after
  in argv order is not guaranteed). POTENTIAL AMBIGUITY.
- Template `:75`–`:79` calls the 11-stage split + fixed caps "over-prescriptive for current
  models" — this fragment confirms the literal caps (quality 5, test 10/2, PR-review 3,
  task-review 3) and the heavily-scaffolded prompts that motivate that critique.
