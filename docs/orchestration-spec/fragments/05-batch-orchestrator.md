# Fragment: batch-orchestrator.sh
Source commit: working tree (uncommitted reference repo) — Mapped lines: 1–842
Reference path: `/Users/craigperler/Development/heysoo/.claude/scripts/batch-orchestrator.sh`

> **D5 verdict up front:** batch-orchestrator.sh is **SUBSUMED by ralph-loop.sh** on
> every orchestration mechanic, with **two minor non-unique deltas** to confirm before
> dropping (per-stage agent selection; flat per-issue PR ledger shape). Recommendation:
> **spec-only-to-confirm-then-drop.** See the D5 section at the end.

---

## 1. Role & entry points — who invokes it, with what argv

Legacy serial batch driver for GitHub issues. Each issue runs two stages: an
implement stage (delegated to `implement-issue-orchestrator.sh`, itself a wrapper →
`implement-orchestrator.sh`, `:546-606`) and a `process-pr` stage (a direct
`claude -p /process-pr` call, `:670-674`).

Entry points / argv (`:56-106`):
- `./batch-orchestrator.sh --manifest <path>` (`:78-82`)
- `./batch-orchestrator.sh --issues "123,124,125" --branch "test"` (`:83-92`)
- `./batch-orchestrator.sh --manifest <path> --agent <name>` (`:93-97`)
- `--help|-h` → `usage()` → exit 3 (`:98-100`, `:56-74`)

Invoked by the `handle-issues` skill (status.json consumer noted at `:16`). Referenced
in `adapting-claude-pipeline/SKILL.md:156` as "Modify (agent references)". No script in
the repo shells out to it (grep found no callers other than the skill doc); it is a
human/skill-launched top-level driver, same launch posture as ralph.

## 2. Inputs — every flag, env var, file read

Flags (`:51-106`):
- `--manifest <path>` → `MANIFEST`. Required value or exit 3 (`:79`).
- `--issues <csv>` → `ISSUES`. Required value or exit 3 (`:84`).
- `--branch <name>` → `BRANCH`. Required value or exit 3 (`:89`).
- `--agent <name>` → `AGENT`. Required value or exit 3 (`:94`). Applies to the
  implement stage ONLY; process-pr is hardcoded to `code-reviewer` (`:72`, `:673`).

Manifest file (read via `jq`, `:108-120`): `.issues` (array → CSV, `:114`),
`.base_branch` (`:115`), `.agent` (optional, only if `--agent` not given on CLI,
`:117-119`). Missing manifest file → exit 3 (`:110-113`).

Validation: `--issues` AND `--branch` both required if no manifest (`:122-125`).

Env vars: NONE read by this script. (No `ORCHESTRATOR_PROVIDER`, no capacity envs.)

Other files read:
- `$SCHEMA_DIR/process-pr.json` — required, exit 3 if absent (`:132-135`); loaded
  compacted into `PROCESS_SCHEMA` (`:137`). Schema enum: `merged | changes_requested |
  error | rate_limit`; fields `status`(req), `follow_up_issues[]`, `innovation_idea`,
  `process_learnings`, `pipeline_notes[]`, `error`
  (`/Users/craigperler/Development/heysoo/.claude/scripts/schemas/process-pr.json`).
- `lib/status-file-helpers.sh` sourced (`:39`) — provides `status_file_write` /
  `status_file_update` with a lock wrapper (shared with ralph).
- Per-issue status file `$LOG_BASE/issue-$N-status.json` written by the delegated
  implement orchestrator, read back for `.state` and `.stages.pr.pr_number` (`:613-639`).

## 3. Outputs — every file written, exit codes, side effects

Files written:
- `status.json` (`STATUS_FILE`, `:35`) — live progress. Top-level: `state`,
  `base_branch`, `current_issue`, `progress{total,completed,failed,pending,in_progress}`,
  `issues[]`, `rate_limit{waiting,resume_at,session_id}`, `last_update`, `log_dir`
  (`:281-306`). Per-issue record fields: `number`, `status`, `stage`, `pr`,
  `session_id`, `error`, `follow_ups[]`, `started_at`, `completed_at` (`:268-278`).
  `state` values: `running`(`:282`) → `killed`(`:209`) / `circuit_breaker`(`:806`) /
  `completed_with_errors`(`:819`) / `completed`(`:821`). Per-issue `status`: `pending`,
  `in_progress`, `failed`, `skipped`, `completed` (`:555,645,750,791,337`).
- `logs/batch-<ts>/orchestrator.log` (`:247`) — main log via `log()` (`:253-255`).
- `logs/batch-<ts>/issue-<N>.log` + `.impl` + `.stream.jsonl` per issue (`:548,574,471`).
- `logs/batch-<ts>/.child_pids` (`_ORCHESTRATOR_PID_FILE`, `:250`) — PID tracking.
- `logs/batch-<ts>/status.json` — copy on signal-kill only (`:215-217`).
- `logs/batch-<ts>/summary.json` — final summary (`:831-838`): `{state, base_branch,
  progress, issues[{number,status,pr,follow_ups,error}], log_dir, completed_at}`.
- `logs/.batch-orchestrator.lock` (`LOCK_FILE`, `:36`) — PID lock file.

Exit codes (`:19-24`): `0` all OK; `1` some failed; `2` circuit breaker; `3` config/arg error.

Side effects:
- git: `git worktree remove --force` + `git worktree prune` on merge/changes_requested
  (`:532,539`), worktree path read from the per-issue status file (`:519`).
- gh / PR work happens INSIDE the delegated orchestrator and `/process-pr` skill, not
  directly here. This script never calls `gh`.
- network: only via `claude` subprocess and the delegated orchestrator.
- process control: kills tracked child PGIDs/PIDs on signal (`:191-199`).

## 4. Control flow — state machine, loop structure, caps, exit conditions

Top-level: parse args (`:76-125`) → load schema (`:132-137`) → `acquire_lock` (`:240`) →
set traps (`:236-239`) → mkdir logs (`:246`) → `init_status` (`:782`) → **serial main
loop** (`:787-811`) → final state (`:814-822`) → summary (`:831-838`) → `exit $exit_code`.

Main loop (`:787-811`) — one iteration **per issue, strictly serial** (no concurrency):
1. Idempotency check: read `.status` for this issue from `STATUS_FILE`; if `completed`,
   `continue` (skip) (`:789-794`).
2. `process_issue "$issue"` (`:796`). On success: `consecutive_failures=0` (`:797`).
3. On failure: `consecutive_failures++`, `exit_code=1` (`:800-801`).
4. **Circuit breaker:** if `consecutive_failures >= MAX_CONSECUTIVE_FAILURES (3)` →
   `set_state circuit_breaker`, `exit_code=2`, `break` (`:804-808`).

`process_issue` per-issue state machine (`:546-765`):
- Set `in_progress`, `started_at`, `stage=implement-issue` (`:554-558`).
- **Implement stage** (`:579-606`): run delegated orchestrator in background, track PID,
  `wait` (`:585-599`). Parse per-issue status file `.state` (`:615`):
  - `completed` → `impl_status=success`, extract `pr_number` from `.stages.pr.pr_number`
    or fallback regex `PR: #N` on log (`:619-627`).
  - `error|max_iterations_quality|max_iterations_pr_review` → error (`:628-631`).
  - else / missing file → error (`:632-639`).
  - On non-success OR empty `pr_number` → mark `failed`, `return 1` (`:643-657`).
- **process-pr stage** (`:662-761`): set `stage=process-pr` (`:660`); run
  `run_claude_streaming` (`:670`). Capture `session_id` (`:679-682`).
  - **Rate-limit branch** (`:685-716`): if `detect_rate_limit` true → compute
    `wait_time`, `sleep`, then RESUME via `--resume $session_id` (or re-issue
    `/process-pr` if no session) — a SINGLE retry, no loop (`:700-713`).
  - **Timeout branch** (`:719-725`): if `proc_exit == 124` → mark `failed`, `return 1`.
  - Parse `.structured_output.status` (`:732`); dispatch (`:738-761`):
    - `merged` → `completed`, set `completed_at`, `follow_ups`, cleanup worktree (`:739-745`).
    - `changes_requested` → `completed` + cleanup (re-impl handled inside `/process-pr`) (`:746-753`).
    - `error|rate_limit|*` → `failed`, `return 1` (`:754-760`).

There is exactly ONE loop (the per-issue `for`, `:787`) and ONE inline single-shot
rate-limit retry. No quality/test/review iteration loops live here — those are inside
the delegated `implement-orchestrator.sh`.

## 5. External invocations — verbatim

Delegated implement orchestrator (`:579-584`):
```
"$SCRIPT_DIR/implement-issue-orchestrator.sh" \
    --issue "$issue_num" \
    --branch "$BRANCH" \
    "${agent_args[@]}" \                # (--agent "$AGENT") if AGENT set, :565-568
    --status-file "$issue_status_file" \
    > "$impl_output_file" 2>&1 &
```
(`implement-issue-orchestrator.sh` maps `--issue N` → `--task '#N'` and `exec`s
`implement-orchestrator.sh` — `/Users/craigperler/Development/heysoo/.claude/scripts/implement-issue-orchestrator.sh:20,30`.)

process-pr stage (via `run_claude_streaming`, `:670-674` + `:464-503`):
```
timeout "$ISSUE_TIMEOUT" claude \
    -p "/process-pr $pr_number $issue_num $BRANCH" \
    --agent code-reviewer \
    --dangerously-skip-permissions \
    --json-schema "$PROCESS_SCHEMA" \
    --output-format stream-json
```
(`--output-format stream-json` appended at `:477`; result = `tail -1` of stream, `:497`.)

process-pr RESUME (`:701-706`): same as above but `-p "please continue" --resume "$session_id"`
(or re-issues `/process-pr ...` with no `--resume` if session id missing, `:708-712`).

git (`:532,539`): `git worktree remove "$worktree_path" --force`; `git worktree prune`.

jq: used throughout for status-file mutation and manifest/schema parsing (`:114-118,137,
268,336,392,...`). No `gh`, no `codex`, no model pins, no `--model` flag anywhere — model
selection is entirely delegated downstream.

## 6. Constants & tunables

- `ISSUE_TIMEOUT=10800` (180 min/issue) — `readonly`, `:42`. Applied via `timeout` to
  both implement (indirectly, downstream) and process-pr (`:476`).
- `MAX_CONSECUTIVE_FAILURES=3` — circuit-breaker threshold, `readonly`, `:43`.
- `RATE_LIMIT_BUFFER=60` — extra seconds added after computed wait, `:44,688`.
- `RATE_LIMIT_DEFAULT_WAIT=3600` (1 h) — fallback when wait time can't be parsed, `:45,455`.
- `LOG_BASE="logs/batch-$(date +%Y%m%d-%H%M%S)"` (`:34`); `STATUS_FILE="status.json"`
  (`:35`); `LOCK_FILE="logs/.batch-orchestrator.lock"` (`:36`).
- No pricing, no model pins, no concurrency constant (it is serial → effective
  concurrency = 1).

## 7. Failure handling — retries, fallback, circuit breaker, cascade

- **Retry:** essentially none at the batch layer. Implement stage: zero retries — one
  failure marks the issue `failed` (`:643-649`). process-pr: exactly ONE rate-limit
  retry after a sleep (`:685-716`); no exponential backoff.
- **Rate-limit detection** (`detect_rate_limit`, `:386-421`): trusts
  `.structured_output.status` first (`success|merged|changes_requested` → NOT limited;
  `rate_limit` → limited), else greps `.result` / raw output for
  `rate.limit|429|too many requests|quota.exceeded|secondary rate` (`:395-418`).
- **Wait-time extraction** (`extract_wait_time`, `:423-456`): parses `retry.after N`,
  `wait N min`, `wait N hour`; defaults to `RATE_LIMIT_DEFAULT_WAIT` (`:432-455`).
- **Timeout:** `proc_exit == 124` → `failed` for that issue (`:719-725`). Implement
  timeout is enforced downstream, not surfaced specially here.
- **Circuit breaker** (`:804-808`): GLOBAL consecutive-failure counter; 3 in a row →
  abort whole batch, `state=circuit_breaker`, exit 2. Reset to 0 on any success
  (`:797`). NOTE: this is consecutive-failure, NOT identical-error — different mechanism
  from ralph's `CIRCUIT_BREAKER_IDENTICAL`.
- **Cascade:** NONE. A failed issue does not block any other issue. No dependency edges
  exist, so there is nothing to cascade.
- **Locking** (`:143-167`): PID lock with live-PID check (`kill -0`) and stale-lock
  removal; second instance → exit 3 (`:150-154`).
- **Signal handling** (`:169-239`): TERM/INT/HUP → `_batch_signal_handler` →
  `batch_cleanup` (re-entrant guarded): kills tracked child PGIDs (`kill -- -PID`) then
  SIGKILL stragglers (`:182-201`), marks in_progress issues `killed` + `state=killed`
  (`:204-217`), releases lock (`:221`), re-raises signal (`:232-233`). EXIT trap also
  runs `batch_cleanup` (`:239`).

## 8. Coupling — generic vs Hey Soo!-specific

| Element | Class | Generic shape |
|---|---|---|
| Serial per-issue loop, circuit breaker, lock, signal cleanup, status-file schema, summary writer | **Generic** | Reusable engine skeleton; matches the ralph status/lock/cleanup patterns (shares `status-file-helpers.sh`). |
| Two-stage `implement → process-pr` shape | **Generic** | "execute task, then review/merge its PR" — but ralph collapses both into one delegated per-task orchestrator. |
| `--agent` roster names (`bulletproof-frontend-developer`, `python-backend-developer`, `code-reviewer`) in usage text (`:67-72`) | **Hey Soo!-specific** | Agent roster must come from the project-config adapter, not hardcoded in `usage()`. |
| Hardcoded `--agent code-reviewer` for process-pr (`:673`) | **Hey Soo!-specific (role pin)** | Per-stage agent role should be an adapter/config value, not literal. |
| `/process-pr <pr> <issue> <branch>` skill contract (`:671`) | **Hey Soo!-specific** | The review/merge skill name + arg order is a project skill; generic engine should treat "PR-finalize stage" as a pluggable command. |
| `implement-issue-orchestrator.sh` delegation (`:579`) | **Generic w/ HS naming** | "delegate the per-task pipeline to a sub-orchestrator" is the generic move; the script name is incidental. |
| GitHub-issue-number identity model (`issue_num`, `--issues` CSV) | **Mostly generic** | Generic "work item ID"; ralph already generalized this to roadmap-IDs + bare-issue-numbers. |
| `process-pr.json` schema | **Generic** (schema file itself is project-agnostic) | Keep as the PR-finalize stage output contract. |
| Worktree cleanup via git | **Generic** | Reusable; reads worktree path from per-task status file. |

## 9. Anomalies — suspected bugs, dead code, contradictions

1. **`process_issue` declares `local proc_output proc_exit` but `session_id` is assigned
   unqualified** (`:679`) — `session_id` leaks to global scope. Harmless (single-threaded
   serial loop) but inconsistent with the rest of the function's `local` discipline.
2. **`changes_requested` is treated as terminal `completed`** (`:746-753`) with a comment
   asserting `/process-pr` already handled re-implementation internally. If that skill
   ever returns `changes_requested` WITHOUT having merged, the batch silently records a
   success for an unmerged PR. Trust boundary is implicit and undocumented.
3. **Rate-limit retry is single-shot and unconditional-once** (`:685-716`): if the
   resume ALSO hits a rate limit, the subsequent `case` falls into `error|rate_limit|*`
   → `failed` (`:754`). No second wait. Acceptable, but means a long outage burns an
   issue rather than parking it (ralph parks via `paused`/`throttled` and keeps it
   non-failed).
4. **No env-var capacity throttle** — unlike ralph, batch never consults usage cache;
   it relies purely on reactive rate-limit detection mid-call. Under the §3 billing
   model this is strictly worse than ralph's proactive throttle.
5. **Contradiction with `docs/orchestration-template.md`:** the §8 inventory table
   (template.md:397) labels batch-orchestrator "Batch-mode parallel execution **with
   dependency tracking**." The source has **NEITHER** parallelism (the loop is strictly
   serial, `:787`, no `&`-backgrounded fan-out, no `MAX_CONCURRENT`) **NOR** dependency
   tracking (no `depends_on`, no DAG, no blocked/unlock states anywhere in 842 lines).
   The doc's one-line description of THIS file is **inaccurate**; those properties belong
   to `ralph-loop.sh`, which the same doc correctly credits at template.md:29-33,393.
   **DISPUTED — see below.**
6. **Prompt premise mismatch (mapping-task brief):** the assignment described an
   "`--agent unique-features` mandate." No such flag/concept exists. `--agent` is a
   single optional implement-stage agent selector (`:93-97,565-568`); the only "mandate"
   is the hardcoded `code-reviewer` for process-pr (`:673`). Flagged so it is not
   carried into the target spec as a real feature.

---

## D5 DECISION — Is batch-orchestrator.sh subsumed by ralph-loop.sh?

**VERDICT: YES — subsumed. Recommend spec-only-to-confirm-then-drop.**

Ralph is a strict superset on every orchestration axis. Evidence (batch cite ←→ ralph
cite, ralph cites from `/Users/craigperler/Development/heysoo/.claude/scripts/ralph-loop.sh`):

| Capability | batch-orchestrator.sh | ralph-loop.sh | Subsumed? |
|---|---|---|---|
| Fixed set of GitHub issues from a list | `--issues` CSV / `--manifest` (`:114,128`) | `--tasks` CSV (issue #s + roadmap IDs) (`:8,171-175,341-345`) | YES (ralph also reads a runtime queue file `:33,488-507`) |
| Parallel execution | **NO — strictly serial** (`:787`, no fan-out) | YES, `MAX_CONCURRENT=3`, backgrounded launches (`:38,1413,1878-1882`) | YES (ralph adds the parallelism the doc wrongly attributed to batch) |
| Dependency tracking | **NONE** | analyzed `depends_on` DAG, blocked→unlock, cascade-block (`:1070-1214,985-1011,1593-1620`) | YES (ralph adds it) |
| Implement → PR stages | two explicit stages here (`:546-761`) | delegates full lifecycle to per-task orchestrator (`:1408`) | YES (same work, ralph delegates both into one child) |
| Circuit breaker | global consecutive-3 (`:804-808`) | per-task identical-error-3 (`:41,1561-1581`) | FUNCTIONALLY YES (different, arguably better, mechanism) |
| Rate-limit handling | reactive detect + single sleep/resume (`:685-716`) | proactive capacity throttle + `throttled`/`paused` non-failing states (`:58-84,1838-1867`) | YES (ralph strictly better) |
| Lock / signal cleanup | PID lock + traps (`:143-239`) | PID+statusfile lock + traps (`:800-871,2154-2171`) | YES |
| Idempotency / resume | skip `completed` on re-run (`:789-794`) | full `--resume` of `status-ralph.json` (`:1662-1740`) | YES (ralph richer) |
| Retry-with-learnings | **NONE** | yes (`:1481-1487`, attempt/error-signatures) | ralph adds it |
| Provider routing (codex) | **NONE** | per-task `:codex`/`:provider` (`:348-376,1384-1389`) | ralph adds it |

**The only two batch behaviors NOT literally present in ralph** (and neither is unique
value worth preserving as-is):

1. **Per-stage agent role split** — batch lets `--agent` pick the implement agent while
   pinning `code-reviewer` for process-pr (`:565-568,673`). Ralph passes ONE global
   `--agent` straight through (`:247-251,1373-1375`) and has no separate PR-review agent
   role at the scheduler level (the review agent lives inside the per-task pipeline).
   → **Not unique value; it's a coupling artifact.** In the target spec the PR-finalize
   agent is a project-config adapter value (see §8), so this collapses into config.

2. **Flat per-issue PR ledger in `status.json`** — batch records `number/status/stage/
   pr/follow_ups` per issue (`:268-278`). Ralph's `status-ralph.json` is dependency/
   retry-graph shaped; `pr`/`stage`/`follow_ups` live in each task's own per-task status
   file, not the scheduler record. → **Shape difference, not a capability.** `follow_ups`
   (from `process-pr.json`) is data the per-task layer already produces; the spec just
   needs to decide where it surfaces. No batch-only behavior is lost.

**Conclusion:** batch-orchestrator.sh is the older, serial, dependency-free ancestor of
ralph. Everything it does, ralph does or supersedes. Keep it in the as-built spec ONLY to
record the two deltas above (PR-finalize agent role; where `follow_up_issues` surface),
then drop it from the rebuild. Do **not** port it.

---

## DISPUTED (carry to synthesis)

- **D-1 (doc inaccuracy, high confidence):** `docs/orchestration-template.md:397`
  describes batch-orchestrator.sh as "parallel execution with dependency tracking." The
  source has neither (serial `for` loop `:787`; zero dependency machinery). Recommend
  correcting the inventory line; these properties are ralph's, not batch's.
- **D-2 (brief premise, high confidence):** there is no "`--agent unique-features`
  mandate" in the file. `--agent` = single implement-stage agent; process-pr is pinned
  to `code-reviewer`. Do not encode a phantom "unique-features" mandate in the target spec.
- **D-3 (behavior trust boundary, medium confidence):** `changes_requested` →
  `completed` (`:746-753`) assumes `/process-pr` always merged before returning that
  status. Unverified against the skill; flag for the per-task-pipeline fragment owner to
  confirm the contract.
