# As-Built Spec — Hey Soo! `.claude/` Orchestration System

> Phase 1 ground-truth extraction. Faithful as-built of the reference system at
> `/Users/craigperler/Development/heysoo/.claude/` (read in place; nothing copied).
> Produced by a 14-unit read-only fan-out → adversarial verify → hierarchical
> synthesis. Every §§4–7 claim in the underlying fragments cites `absolute-path:line`.
> **This is the reviewable artifact at the Phase 1 gate. No Phase 2 work has started.**

## How to read this
- **Per-subsystem as-built** lives in `sections/` (merged & verified from `fragments/`):
  - [`sections/scheduler.md`](sections/scheduler.md) — ralph-loop + batch-orchestrator (+ D5 verdict)
  - [`sections/stage-engine.md`](sections/stage-engine.md) — run_stage core, provider/cost/capacity, state + quality/test loops, per-task pipeline
  - [`sections/helpers-and-tests.md`](sections/helpers-and-tests.md) — lib helpers + the test corpus (behavioral contract)
  - [`sections/schemas-and-entry.md`](sections/schemas-and-entry.md) — schema↔stage + entry points / task source
  - [`sections/config-agents-skills.md`](sections/config-agents-skills.md) — agent roster, settings/hooks, wired + disposition skills
  - [`sections/runtime-artifacts.md`](sections/runtime-artifacts.md) — empirical on-disk field tables (two log trees)
  - [`sections/monitor.md`](sections/monitor.md) — monitor-orchestrator (+ D5 verdict)
- **Frozen inventory** (the Part-0 source of truth the completeness critic diffs against):
  `inventory/functions.txt` (79 defs, 2a/2b partition), `inventory/files.txt` (full census, every path owned-or-excluded), `inventory/env-vars.txt`, `inventory/skill-wiring.txt`.
- **This file** holds the cross-cutting registries the fragments can't see alone, the
  call-count audit, and the **discrepancy log**.

## Inventory at a glance (authoritative)
- `.claude/scripts/` = **17,075 lines** `.sh` + **448** lines across **17** schema `.json`.
- `orchestrator-common.sh` = **79 definitions** (77 top-level + 2 nested), partitioned 2a=39 / 2b=40.
- Boundary mapped = whole `.claude/` (scripts + agents + 20 skills + hooks + settings) **+** runtime artifacts. No `.github/` Actions; **no `claude -p`/`codex exec` outside `.claude/`** (dispatch fully contained).
- Two-layer system: **scheduler** (`ralph-loop.sh`, 2,299) over a **per-task pipeline** (`implement-orchestrator.sh` 2,217 + `orchestrator-common.sh` 4,347 shared engine).

---

# Cross-cutting registry 1 — Environment & tunables
Full knob list: `inventory/env-vars.txt`. **Env-overridable** knobs (read `${VAR:-…}`, 38)
include `ORCHESTRATOR_PROVIDER` (ADR-062 global codex switch), `TASK_PROVIDER` (per-task
`:codex`), `EXECUTION_MODE`, `STAGE_TIMEOUT*`, `MAX_TASK_REVIEW_ATTEMPTS`, `GITHUB_REPO`,
`LOG_BASE`, `STATUS_FILE`, the resume/retry knobs `RESUME_TASK` / `RETRY_AFTER`
(distinct from the `retry_after` status field) / `RESET_AT` (distinct from internal
`CAPACITY_RESET_AT`), the Hey Soo!-specific `HEYSOO_URL` / `HEYSOO_TEST_EMAIL` /
`HEYSOO_E2E_SMOKE_DRY_RUN`, and the `MOCK_*` test knobs.
**Secrets excluded** from the inventory (`e2e-credentials.env`).

**In-script constants (NOT env-overridable; captured per fragment §6):**
| Constant | Value | Source | Role |
|---|---|---|---|
| `MAX_CONCURRENT` | 3 | ralph-loop.sh:38 | scheduler parallelism |
| `COOLDOWN` | 120s | ralph-loop.sh:39 | retry cooldown |
| `CIRCUIT_BREAKER_IDENTICAL` | 3 | ralph-loop.sh:41 | scheduler breaker (3 identical error sigs) |
| `MAX_RETRIES` | 3 | ralph-loop.sh:37 | per-task attempts |
| `MAX_QUALITY_ITERATIONS` | 5 (flat) | orchestrator-common.sh:35 | quality loop cap |
| `MAX_TEST_ITERATIONS` | 10 full / 3 lite+E2E / 2 lite | orchestrator-common.sh:36 (+3674–3682) | test loop cap |
| `MAX_IDENTICAL_FAILURE_SIGNATURE` | 2 | orchestrator-common.sh:39 | test-loop plateau breaker |
| `MAX_PR_REVIEW_ITERATIONS` | 3 | orchestrator-common.sh:41 | PR review loop |
| `MODEL_CHAIN` | opus-4-7 → sonnet-4-6 → haiku-4-5-20251001 | orchestrator-common.sh:49 | rate-limit fallback |
| `CODEX_ELIGIBLE_STAGES` | 8 globs (implement-task-*, fix-*) | orchestrator-common.sh:145–154 | per-task codex allowlist |
| `STAGE_TIMEOUT_INITIAL` | 1800s | orchestrator-common.sh:25 | stage timeout |
| capacity max sleep cap | 3600s + jitter `RANDOM%301` | orchestrator-common.sh:2061/2077 | throttle clamp |
| `PORT_REGISTRY_{MIN,MAX}` | 5173–5272 | lib/port-registry.sh:7–8 | parallel-worktree ports |
| `ISSUE_TIMEOUT` (batch) | 10800s | batch-orchestrator.sh | legacy batch |
| `STALL_THRESHOLD` (monitor) | 1800s (banner says 900, header 600 — bug) | monitor-orchestrator.sh:42 | stall alert |

# Cross-cutting registry 2 — Status-file field registry
Empirical key lists verified against real files (`inventory` + fragment 13). Writers are 2b
functions; reader is the monitor.

**`status-ralph.json` (scheduler)** — 7 top-level: `state, tasks, dependency_graph, config, progress, log_dir, last_update`.
- `progress{}`: `total, completed, running, blocked, ready, retrying, permanently_failed, cascade_blocked`.
- `tasks["#NNN"]{}` (14): `state, attempt, max_retries, depends_on, reason, mode, mode_reason, unmet_deps, pid, status_file, last_error, retry_after, completed_at, error_signatures`.
- Writer: `ralph-loop.sh`. Reader: `monitor-orchestrator.sh` (detects via `.dependency_graph`, :1246).

**Per-task status (`status-ralph-issue-NNN.json`)** — 24 keys: `state, issue, base_branch, branch, worktree, current_stage, substage, substage_detail, current_task, execution_mode, stages_skipped, stages_executed, stages, tasks, quality_iterations, test_iterations, pr_review_iterations, last_update, log_dir, stage_counter, current_model, current_provider, pr_number, pr_url`.
- Writers (orchestrator-common.sh): `init_status`:620, `update_stage`:682, `set_stage_started`:709, `set_stage_completed`:723, `update_task`:737, `set_tasks`:771, `set_final_state`:793; every writer mirrors to `$LOG_BASE/status.json` via `sync_status_to_log`:1464 (the resume source).
- `.stages.<name>{status, started_at, completed_at}` (+ `task_progress`/`iteration`/`baseline_failures` on some). **Writer-omission anomalies:** `stages.verify` and `stages.quality_loop` lack `started_at`.
- **`pr_number` + `pr_url` writer** (completeness-critic closure): both set together by the **pr stage**, `implement-orchestrator.sh:1628` (`.stages.pr.pr_number = $pr | .pr_number = $pr | .pr_url = $url`) — not orphans (`pr_url` was simply unattributed in the fragments). `current_model`/`current_provider` are written inline by `run_stage` (orchestrator-common.sh:74).
- Atomicity: all writes go through the locked primitive `status_file_update`/`status_file_write` (flock fd200 + mkdir fallback + `tmp.$$` atomic mv), `lib/status-file-helpers.sh`.

# Cross-cutting registry 3 — Model + pricing table
| Stage class | Model (pinned) | Source |
|---|---|---|
| setup | `claude-haiku-4-5-20251001` | get_stage_model, orchestrator-common.sh:55–71 |
| research / plan / implement-task-* / fix-* | `claude-opus-4-7` | " |
| evaluate / *review* / simplify / test / docs / pr / complete | `claude-sonnet-4-6` | " |
| ralph dependency-analysis + learnings (one-shot) | `claude-sonnet-4-6` (hardcoded) | run_provider_oneshot, orchestrator-common.sh:275 |
| rate-limit fallback | opus-4-7 → sonnet-4-6 → haiku-4-5 | MODEL_CHAIN :49 |

**Pricing (in `emit_cost_summary`):** Opus priced **$15/$75 per MTok @ :2726** — **STALE**, real Opus is ~$5/$25 → cost reports **overstate ~3×**. Model pins are stale (`opus-4-7` while 4.8 is current; CLAUDE.md says 4-6). Feeds the design-doc §5 "single model/pricing config table" fix.

# Cross-cutting registry 4 — Schema ↔ stage matrix (17 schemas)
13 live, **3 DEAD**, **1 ORPHAN** (full table in `sections/schemas-and-entry.md`).
- Live (consumed via `--json-schema`): research, evaluate, plan, implement (+docs reuse), simplify, review (multi), fix (multi), task-review, pr, complete, process-pr (batch), ralph-dependency-analysis, ralph-learnings-summary.
- **DEAD** (never passed to `--json-schema`): `implement-issue-test.json`, `implement-roadmap-task-extract.json`, `implement-roadmap-task-update.json` (the last two only *document* the bash scripts' output — parsed by `jq`, not validated).
- **ORPHAN:** `implement-issue.json` (batch design intent, never wired).
- Coupling: only `ralph-dependency-analysis.json:50–55` (`needs_deploy`) is weakly Hey Soo!-coupled; otherwise schemas are generic (design-doc claim holds).

# Cross-cutting registry 5 — Skill ↔ script ↔ agent map
Evidence-driven (`inventory/skill-wiring.txt`). 20 skills → **11 wired, 9 disposition**.
| Skill | Wiring type | Evidence | Dispatches agent |
|---|---|---|---|
| using-skills | hook-injected | session-start.sh:12 | — |
| writing-plans | script-referenced | implement-orchestrator.sh:793 (plan stage) | — |
| ralph-loop | script (drives) | ralph-loop.sh | → implement-roadmap-task-orchestrator.sh |
| implement-roadmap-task | script (drives) | SKILL drives implement-orchestrator.sh directly | the pipeline agents |
| improvement-loop | script-emitted | orchestrator-common.sh:812/845/851 | — |
| test-driven-development | agent-referenced | python-backend-developer.md:219 | — |
| ui-design-fundamentals | agent-referenced (PRODUCT-SPECIFIC) | bulletproof-frontend-developer.md:23 | — |
| bulletproof-frontend | agent-referenced (2 skill / 28 agent refs; PRODUCT-SPECIFIC) | refactor-blade-thorough.md:57 etc. | via bulletproof-frontend-developer |
| adapting-claude-pipeline | entry-point (the §5 BOOTSTRAP) | human-invoked | → cc-orchestration-writer |
| dispatching-parallel-agents / subagent-driven-development | entry-point methodology | — | python-backend / bulletproof-frontend |

**Agent roster (5):** `code-reviewer` (code-review loops + batch PR), `spec-reviewer` (per-task goal review), `python-backend-developer` (lambda/backend + unit-fix), `bulletproof-frontend-developer` (frontend tasks + E2E/tsc fix; PRODUCT-SPECIFIC), `cc-orchestration-writer` (**ORPHAN** — author agent, 0 pipeline dispatch). None declare a `tools:` allowlist → inherit all.

# Cross-cutting registry 6 — Two-axis routing-decision table
`execution_mode × provider`; the as-built provider routing (ADR-062-conformant):
| Axis | Mechanism | Source |
|---|---|---|
| Provider — global | `ORCHESTRATOR_PROVIDER=codex` flips **every** stage to codex | orchestrator-common.sh:191–200 |
| Provider — per-task | `:codex` tag → `TASK_PROVIDER` (exported :183) routes **only** `CODEX_ELIGIBLE_STAGES` (8 file-patching globs) | :145–154 |
| Model | `get_stage_model` tiering + `MODEL_CHAIN` fallback | :55–71, :49 |
| Execution surface | `run_stage`→`run_claude_streaming` (stream-json) \| `run_codex_stage` (`codex exec --json --output-last-message`) | :2399 / :2469 |
| **One-shot side path** | `run_provider_oneshot` (:242, hardcodes sonnet-4-6, **bypasses run_stage + record_stage_invocation**) — used by ralph dep-analysis (:1172) + learnings (:1515) | **cost-attribution gap** |

**Codex success heuristic:** `exit0 ∧ no is_error ∧ (HEAD-moved ∨ tree-dirty ∨ required-top-level-keys-present)` (:2576–2624) — thin (top-level `has()` only); design-doc §2 fix #5 (tighten before codex becomes primary).

# Cross-cutting registry 7 — Call-count audit
Design doc claims **full ~50 / lite ~20–25 / micro ~10** model calls per task. Derived from
the verified stage map + worst-case loop caps:
- **Full (worst case):** extract(0 model) + setup(1) + research(1) + evaluate(1) + plan(1) + per-task implement(1)+task-review(1)+quality_loop(≤5×~2)+ fixes + test_loop(≤10 fix calls; test *runs* are direct/no-model)+ tsc-gate(≤2) + docs(1) + pr(1) + pr_review(spec+code ≤3 ×~2) + complete(1) ⇒ **easily ~50** at worst case. **Consistent.**
- **Lite** skips research/evaluate/plan/quality_loop/docs ⇒ **~20–25. Consistent.**
- **Micro** also skips test_loop ⇒ **~10. Consistent.**
- **Caveats:** (a) these are *worst-case* caps — `detect_convergence` exits quality/PR loops early, so typical ≪ cap; (b) the **ledger under-counts** because `run_provider_oneshot` (dependency-analysis + per-failure learnings) is never recorded (cost-attribution gap) — observed `stage-costs.jsonl` totals are below true spend.

## Engine-helper coverage closure (completeness critic)
The critic flagged 8 owned functions thinly covered by the engine fragments. Source-grounded one-liners (so `functions.txt` is fully accounted for):
| Function | Owner | Behavior | Source |
|---|---|---|---|
| `dependency_hash_matches` | 2a | compares a lock-file's hash against the stored hash file → the dep-cache skip decision (skip `INSTALL` if unchanged) | orchestrator-common.sh:414 |
| `write_dependency_hash` | 2a | writes the lock-file's current hash to the hash file (caches dep state for the above) | orchestrator-common.sh:427 |
| `sync_python_project_dependencies` | 2a | `uv sync` a given python `project_dir` (labelled); distinct from `install_python_dependencies` | orchestrator-common.sh:457 |
| `normalize_task_id` | 2a | trims/normalizes a task id (`xargs` whitespace strip + form-normalize) | orchestrator-common.sh:373 |
| `extract_markdown_from_log` | 2a | derives `<log>.md` from a stage `.log` and extracts the markdown body (feeds cost/learnings/markdown capture) | orchestrator-common.sh:2240 |
| `get_completed_task_count` | 2b | `jq` count of `.tasks[]` with `status=="completed"` | orchestrator-common.sh:1421 |
| `validate_worktree` | 2b | asserts the worktree path exists / is a dir before use | orchestrator-common.sh:1426 |
| `next_stage_log` | 2b | backward-compat helper; formats the current `STAGE_COUNTER` value (caller increments in parent shell) | orchestrator-common.sh:571 |

---

# Discrepancy log (vs `docs/orchestration-template.md`)
| # | Discrepancy | As-built truth | Severity |
|---|---|---|---|
| D1 | §8:397 calls `batch-orchestrator.sh` "parallel execution with dependency tracking" | **Serial** `for` loop, **no** concurrency, **no** dependency tracking (those are ralph's). Correct the line. | material |
| D2 | §2/§8 "11-stage pipeline" | Really **~12–13**: `extract` and `verify` are real stages the list omits; `quality_loop` is **nested inside implement**, not a peer stage. | material |
| D3 | §2 #3 stale pricing | Confirmed: Opus **$15/$75** literal @ orchestrator-common.sh:2726 (real ~$5/$25, ~3× overstatement). | confirmed |
| D4 | §2 #3 stale model pins | Confirmed: `claude-opus-4-7` pinned (4.8 current; CLAUDE.md says 4-6); 2nd hardcoded sonnet pin @ :275. | confirmed |
| D5 | §5 names a `FAILURE_CLASSIFIER` adapter | **Does not exist** in source — `classify-failures.sh` hardcodes `^tests/e2e/`/`.spec.ts` (Hey Soo!). It's a *target* shape, not as-built. | material |
| D6 | (new) Cost-attribution gap | `run_provider_oneshot` (dep-analysis :1172, learnings :1515) hardcodes sonnet + **bypasses `record_stage_invocation`** → ledger systematically under-counts. | material (new) |
| D7 | Design implies all 17 schemas used | **3 DEAD + 1 ORPHAN** (see registry 4). | material |
| D8 | Roadmap-markdown task source | **DEAD** at this commit — GitHub Issues is the live source; `update-roadmap-status.sh` self-**DEPRECATED**; extract output is `jq`-parsed, **not** schema-validated. | material |
| D9 | ADR-062 provider routing | As-built **matches** ADR-062 (`ORCHESTRATOR_PROVIDER` global + `:codex` tag + `CODEX_ELIGIBLE_STAGES`). | consistent |
| D10 | Cost ledger naming | Design's `stage-costs.jsonl` **exists** (nested `context/stage-costs.jsonl`, 11 KB) **and** coexists with `cost-summary.md` rollup (writer `emit_cost_summary`). Not missing — nested + paired. | clarified |
| D11 | `monitor-orchestrator.sh` (2,254 lines) | **Absent from §8 inventory** — an undocumented component. | material |
| D12 | Function count | **77 top-level** (79 with nested) — earlier counts undercounted via a digit-excluding grep. | clarified |
| D13 | (new) `phpdoc-writer` agent | Assigned to write **Python** docstrings (implement-orchestrator.sh:1517) — PHP-carryover bug. | minor (bug) |
| D14 | (new) cascade "transitively" | Cascade blocks only **direct** dependents despite the :1596 "directly or transitively" comment (:1609 direct-membership only). Grandchildren slip to the terminal sweep. | minor (bug) |
| D15 | §2 #2 adaptive caps | `detect_convergence` is wired into quality + PR-review loops but **NOT** the test loop — the adaptive-cap goal is half-built as-built. | confirmed |
| D16 | (new) in-flight fields | `needs_brainstorm`/`brainstorm_*` are in the dep-analysis schema + prompt but **absent from all real output** (issue #505 in-flight). | minor (new) |
| D17 | Capacity threshold | **Two distinct gates** (not a conflict): ralph **launch throttle ≥80%** (:79) vs per-task **at-capacity ≥90%** (:2042). | clarified |
| D18 | CLAUDE.md MODEL_CHAIN | Matches as-built `MODEL_CHAIN` Opus→Sonnet→Haiku (:49) + follow-up-issue tracking convention. | consistent |

---

# D5 decision inputs (for the gate — port vs. drop)
- **`batch-orchestrator.sh` → DROP (subsumed).** Ralph is a strict superset (parallelism, DAG, retry-with-learnings, throttle, codex routing). The only non-ralph behaviors (per-stage agent role split; flat PR ledger shape) are config/layout, not capability. Spec-only-to-confirm, then drop. (`sections/scheduler.md`.)
- **`monitor-orchestrator.sh` → PORT ~30–40%.** The poll/diff/render/liveness engine (~60–70%) is made free by in-session `/workflows` for the interactive×claude default lane. **Must port:** (1) cross-session/headless observability, (2) away-from-keyboard **push notifications** (the unattended slice the credit buys), (3) `--cleanup` state hygiene, (4) capacity-aware alert downgrade. **Note:** force-push detection does **not** exist (earlier hypothesis refuted); only rate-limit + timeout log scanning. (`sections/monitor.md`.)
