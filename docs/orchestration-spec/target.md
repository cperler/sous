# Target Spec — Orchestration Template (Python rebuild)

> Implementation-agnostic design for the rebuilt orchestration system, derived from
> the verified as-built (`as-built.md` + `sections/`). The last reviewable artifact
> before code. Banks the design-doc §5 fixes and the engine/adapter split.
> **Phase 2 — stop at the review gate. No Phase 3 code.**
>
> Reading order: §1 principles → §2 traceability (the parity ledger) → §3 engine API →
> §4 execution adapter → §5 project-config adapter → §6 banked fixes → §7 status schema →
> §8 package layout. Defer rows live in `../../DEFERRED.md`.

---

## 1. Principles & scope

- **Engine / adapter split (the load-bearing line).** A deterministic, token-cheap,
  testable **engine** (state machine, DAG, retry-with-learnings, cost ledger, status
  store, model/pricing table) — invoked by a thin supervisor via a CLI. Two adapter
  families: the **execution adapter** (how/where a model call runs) and the
  **project-config adapter** (what a given repo plugs in). The engine never imports an
  adapter and never makes a model call itself.
- **Orthogonal axes.** `execution_mode ∈ {interactive, headless} × provider ∈ {claude, codex, …}`.
  **Billing is a derived property of the (mode, provider) pair**, never a hardcoded
  branch. `claude -p` is a **supported, non-default** lane (a cost property, not a
  prohibition). Empty cells (e.g. codex-interactive) are explicit, not hidden.
- **Lane attribution, not abstinence.** Every model call is attributed to its
  (mode, provider) lane and recorded in the cost ledger. The failure mode to prevent is
  a *hidden/unattributed* `claude -p`, not a deliberate one (closes the as-built D6 gap
  by construction, §4/§7).
- **Resumability is a schema contract.** A new session resumes from the status files
  alone; this is what keeps execution modes interchangeable (§7).
- **MVP-first with a parity ledger.** Everything cut is tracked in §2 / `DEFERRED.md` —
  nothing is dropped silently.

---

## 2. Traceability table (parity ledger)

Every as-built behavior → exactly one disposition. **Zero TBD rows.** Dispositions:
`port` · `collapse` (into a §6 stage) · `drop` · `defer` (→ DEFERRED.md) · `fix-forward`
(carry intent, fix the bug) · `build-fresh` (target shape the as-built lacks) ·
`complete` (finish a half-built behavior).

### Scheduler — `ralph-loop.sh`
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Dependency DAG + LLM dependency-analysis | scheduler §1.3 | **port** | engine DAG module (§3) |
| `<=2`-task dep-analysis skip (drops real 2-task deps) | RL:1079 | **fix-forward** | always analyze, or honor explicit `depends_on` |
| `MAX_CONCURRENT=3` dispatch loop | RL:1869 | **port** | **engine** owns the capacity-derived dispatch limit (the policy); the Workflow concurrency cap is only a ceiling — interactive mode must not exceed the capacity-safe concurrency |
| Retry-with-learnings (APPEND `## Attempt N`) | RL:1461 | **port** | engine retry module |
| Circuit breaker (3 identical error sigs) | RL:1561 | **port + fix-forward** | port mechanism; replace brittle `<stage>:100-char-line` signature with a structured signature |
| Cascade-blocking **direct dependents only** (D14 bug) | RL:1596/1609 | **fix-forward** | true **transitive** cascade in the DAG module |
| Queue-file ingestion / unattended idle-wait | RL:1768 | **defer** | unattended mode = the credit lane; DEFERRED |
| Launch throttle ≥80% util (+ ≥90% per-task) | RL:79 / OC:2042 | **port** | unify into one capacity policy with two thresholds (admission vs per-call) |
| Crash-resume resets `permanently_failed`→ready (foot-gun) | RL:1711 | **fix-forward** | resume must not silently re-burn budget; require explicit opt-in |
| Cost-attribution gap (one-shot bypasses ledger) (D6) | OC:242 | **fix-forward** | every call → StageResult → ledger row (§4/§7) |

### Legacy batch — `batch-orchestrator.sh`
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| All behavior (serial loop, consecutive-failure breaker, single rate-limit retry) | scheduler §2 | **drop** | D5: fully subsumed by ralph |

### Execution core — `orchestrator-common.sh` (2a)
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| `run_stage` central dispatch | stage-engine §2 | **port → engine emits WorkItem** | §4 |
| Two-axis provider routing (`should_use_codex`, `CODEX_ELIGIBLE_STAGES`) | OC:191/145 | **port** | execution adapter selection (§4) |
| Model tiering (`get_stage_model`) + `MODEL_CHAIN` fallback | OC:55/49 | **port** | single model/pricing table (§6) |
| Execution surfaces (`run_claude_streaming`, `run_codex_stage`) | OC:2399/2469 | **port** | runner implementations (§4) |
| One-shot path (`run_provider_oneshot`, hardcodes sonnet, no ledger) | OC:242 | **fix-forward** | a normal WorkItem on its lane; recorded |
| Capacity throttle math (sleep clamp+jitter, wait-loop) | OC:2060/2095 | **port** | engine capacity policy |
| Rate-limit detect + fallback chain | OC:1977/2223 | **port** | engine; structured detection over regex where possible |
| Cost ledger (`record_stage_invocation`, `emit_cost_summary`) | OC:2653/2704 | **port + fix-forward** | records every call; single pricing table |
| Stale pricing $15/$75 (D3) + stale pins opus-4-7 (D4) | OC:2726/49 | **fix-forward** | single config table, current values |
| Codex success heuristic (top-level keys only) | OC:2595 | **fix-forward** | **full schema validation** (design §2 #5) |
| Traps/cleanup/stale-process reaping | OC:1512 | **port** | engine lifecycle; worktree cleanup |
| Setup/dependency stages (pure shell) | OC:497 | **collapse → intake** | §6 |

### State + loops — `orchestrator-common.sh` (2b)
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Status lifecycle (init/update/final writers) | stage-engine §3 | **port → versioned schema** | §7 |
| Locked status write (flock + mkdir + atomic mv) | helpers §status | **port** | status store contract (§3/§7) |
| Convergence detector | OC:985 | **port + complete** | wire into the test loop too (D15) |
| Quality loop (flat cap 5) | OC:3302 | **collapse → implement (adaptive)** | §6 cap = safety ceiling |
| Test loop (caps 10/3/2) | OC:3588 | **collapse → test (adaptive)** | §6 |
| TSC pre-gate (2 fixes) | OC:3690 | **port** | project-config check inside `test` |
| Circuit breaker plateau (identical test-set, cap 2) | OC:3611 | **port** | engine |
| Infra-failure breaker (3 consecutive) | OC:3853 | **port** | engine |
| Force-push remediation (don't penalize history cleanup) | OC:1036 | **port** | engine |
| Resume (validate/load state) | OC:1324 | **port** | §7 resume contract |
| Issue/PR commenting | OC:1673 | **port** | project-config output plugin |

### Per-task pipeline — `implement-orchestrator.sh`
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| The real **~12–13-stage** sequence | stage-engine §1 | **collapse → 6 stages** | §6 map |
| `LITE_SKIP_STAGES`/`MICRO_SKIP_STAGES` mode machinery | impl-orch:582 | **port** | mode = which collapsed stages run |
| Implement per-task loop (dep-gate, review, fix) | impl-orch:911 | **port** | `implement` stage |
| `phpdoc-writer` writing Python docstrings (D13) | impl-orch:1517 | **fix-forward** | generic docstring agent = project-config value; drop phantom agent |
| Exit-code taxonomy | impl-orch | **port** | engine result codes |
| Completion gate | impl-orch:2009 | **port → review** | §6 |

### Schemas
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| 13 live per-stage schemas | schemas §matrix | **port** | re-mapped onto the 6 collapsed stages |
| 3 dead + 1 orphan schema (D7) | schemas | **drop** | omit |
| `needs_deploy` weak coupling | ralph-dep-analysis | **port** | dependency-analysis output field (adapter-aware) |

### Entry points / task source
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Two wrapper aliases (`implement-roadmap-task-orchestrator`, `implement-issue-orchestrator`) | entry §1 | **drop** | replaced by the engine CLI |
| `extract-roadmap-task` parser | entry | **build-fresh** | pluggable **task-source provider** (§5) |
| Roadmap-markdown source (D8 — dead) | entry | **drop** | dead at HEAD |
| `update-roadmap-status` (deprecated) | entry | **drop** | task-source `mark_complete()` |
| GitHub-Issues as the live task source | entry/D8 | **build-fresh** | the reference task-source impl (§5) |

### Config / agents / skills
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Agent roster (code-reviewer, spec-reviewer, python-backend, bulletproof-frontend) | config §1 | **port → project-config** | roster is adapter config |
| `cc-orchestration-writer` (orphan author agent) | config | **drop** | not a runtime worker |
| `bulletproof-frontend` / `ui-design-fundamentals` (product-specific) | config §skills | **port → adapter drop-in** | project-config plugins |
| Wired skills (ralph-loop, implement-roadmap-task, improvement-loop, writing-plans, using-skills, tdd) | config §skills | **port** | methodology skills / supervisor skill (§8) |
| Non-wired skills (9, disposition catalogue) | fragment 12 | **defer** | DEFERRED disposition rows |
| `adapting-claude-pipeline` bootstrap | config §skills | **port** | the §5 adapter bootstrap |
| settings hooks (fetch-usage, .env/credential block, deploy guard, lint) | config §2 | **port (selective)** | engine/project-config; secrets never inlined |
| `session-start.sh` injects using-skills | config §3 | **port** | supervisor session bootstrap |

### Monitor — `monitor-orchestrator.sh`
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Poll/diff dashboard, render, process-liveness (~60–70%) | monitor D5 | **drop** | free from in-session `/workflows` (default lane) |
| Cross-session observability + push notifications + `--cleanup` + capacity-aware downgrade (~30–40%) | monitor D5 | **port** | the unattended/observability slice |
| Force-push detection (doesn't exist) | monitor | **drop** | refuted hypothesis |
| `STALL_THRESHOLD` 1800/900/600 inconsistency | monitor | **fix-forward** | single source of truth |

### Runtime artifacts
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| `status-ralph.json` (batch) + per-task status shapes | runtime | **port → versioned (§7)** | normalized schema |
| `stage-costs.jsonl` ledger + `cost-summary.md` | runtime | **port** | cost on the stage record + audit sidecar |
| `started_at` writer omission (verify/quality_loop) | runtime | **fix-forward** | always present (§7) |
| In-flight `brainstorm_*` fields (#505) | runtime | **defer** | not landed at HEAD |
| Log trees (batch + per-task) | runtime | **port (simplified)** | one run dir per task; engine-owned |

### Lib helpers / tests
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| Status-file locking primitive | helpers | **port** | status store (§3) |
| `classify-failures` regexes (Jest/pytest, e2e path) | helpers | **build-fresh** | `FAILURE_CLASSIFIER` interface (D5, §5) |
| regression-helpers / detect-change-scope / track-test-commit | helpers | **port (mechanism) + project-config (taxonomy)** | engine owns the change→test-set mechanism (diff → impacted tests, regression diff, last-tested-commit); the **test-taxonomy conventions** (`.spec.ts`, `conftest.py`, `test_*.py`, dir layout) move to the project-config `TEST_TAXONOMY` (§5), not the engine |
| `run-tests-direct` (verbatim test commands) | helpers | **port → project-config** | `TEST_*_CMD` (§5) |
| `port-registry` (parallel-worktree ports) | helpers | **port** | needed when headless worktrees run in parallel; interactive uses workflow concurrency |
| `reset-infra` | helpers | **port → project-config** | infra-reset plugin |
| BATS test corpus (~550+ cases) | fragment 14 | **port (cases) + defer (bulk)** | port engine-logic cases to pytest in 3a; DEFERRED tracks the rest |

### Dev/ops periphery (Phase-1 appendix items)
| Behavior | Source | Disp. | Rationale → target |
|---|---|---|---|
| `statusline-command.sh` (status-line display) | files.txt appendix | **drop** | IDE/display tooling, not orchestration; not template scope |
| `silent-failure-lint.py` (lints Python query helpers that swallow exceptions) | files.txt appendix | **drop** | repo-specific CI lint; if wanted, it becomes a project-config review-plugin (§5), not engine code |
| Other appendix dev/ops (`deploy.sh`, seeders, `e2e-smoke.sh`, `shellcheck-lint.sh`, `sync-shared-modules.sh`, `fetch-usage.sh`, Blade prompts) | files.txt appendix | **drop** | product/CI tooling; `fetch-usage.sh`'s util-cache role is absorbed into the engine `capacity` module |

> **Coverage assertion (verification §):** every as-built subsystem above is dispositioned;
> every `defer` row appears in `DEFERRED.md`; every collapsed-stage target has ≥1 incoming
> as-built stage (§6 map).

---

## 3. Engine API surface (deterministic core)

A Python package `orchestrator/` (importable) + a CLI (`orchestrator …`) the supervisor's
Bash calls. **No model calls here.** Modules and their contracts:

- **`status_store`** — versioned read/write of run/task status (§7) with the as-built
  locking/atomic-write contract (flock + mkdir fallback + temp-then-rename). API:
  `load_run(run_id)`, `load_task(run_id, task_id)`, `update_task(...)`, `append_event(...)`.
- **`state_machine`** — per-task stage transitions over the 6-stage graph (§6); computes
  the next runnable stage; `resume(task)` re-enters at the first non-`completed` stage.
- **`dag`** — `depends_on` graph; `ready()`, `blocked()`, **transitive `cascade(failed_id)`**
  (fixes D14), `unlock_on_complete(task_id)`.
- **`retry`** — attempt tracking, learnings accumulation (append), structured error-signature
  circuit breaker, retry-with-cooldown.
- **`cost_ledger`** — one row per `StageResult` (so the one-shot path is recorded; closes
  D6); reads prices from the single `model_table`; emits the per-run cost summary.
- **`model_table`** — single source for model ids, tier mapping, and prices (fixes D3/D4);
  the only place a model/price is named.
- **`capacity`** — unified policy: admission threshold (was 80%) + per-call threshold
  (was 90%), the clamp+jitter sleep math, the wait-loop with early-exit, and the
  model-fallback-before-sleep option. **The engine computes the capacity-safe dispatch
  limit and that is the binding concurrency policy** — the execution adapter's own
  concurrency cap (e.g. Workflow's `agent()` cap) is merely a *ceiling*; interactive mode
  must never dispatch beyond the engine's capacity-derived limit even if the shim could.
- **`failure_classifier`** (interface) — `classify(test_output) → {unit|e2e|infra|…}` +
  regression diff; the concrete classifier is a project-config plugin (build-fresh, D5).
- **CLI surface** (supervisor entry points): `orchestrator ready` (what's runnable),
  `orchestrator next <task>` (emit the next WorkItem), `orchestrator record <result.json>`
  (ingest a StageResult → ledger + status), `orchestrator resume <run>`,
  `orchestrator status <run>`. The supervisor loop is: *ask engine what's ready →
  dispatch via the execution adapter → record the result → repeat.* No `claude -p` on
  this path; every dispatch is lane-attributed.

---

## 4. Execution adapter — the contract-first seam

**Synthesized from judge-panel (a): artifact contract (A3) + typed schemas (A1) + registry
for in-process cell resolution (A2).** The engine and any execution mode communicate *only*
through two persisted, versioned artifacts — so execution modes are interchangeable by
construction and a run resumes across a session death.

**`WorkItem`** (engine → store; immutable; the engine never imports an adapter):
```
schema_version, id, content_hash (sha256 of stage+prompt+schema_ref+model+lane+attempt → idempotency),
run_id, task_id, stage, attempt, prompt (fully rendered), schema_ref, model,
lane_policy { execution_mode∈{interactive,headless}, provider∈{claude,codex}, allow_fallback },
timeout_s, created_at
```
**`StageResult`** (runner → store; the engine consumes it):
```
schema_version, work_item_id, content_hash, run_id, task_id, stage, attempt,
status ∈ {success, schema_violation, failure, timeout},
structured_output | null, raw_output | null,
lane_used { execution_mode, provider, invocation },   # ground truth for attribution
token_usage { input, output, cache_read, cache_write }, cost_usd, pricing_ref,
error | null, completed_at
```

**Runners (one per served cell; each consumes WorkItems, writes StageResults):**
| Cell | Runner | Notes |
|---|---|---|
| interactive × claude (**default**) | **Workflow shim** (in-session) | the shim runs *inside* the Claude Code session and calls the subagent/`agent()` in-process. **It does NOT write to disk** (the Workflow sandbox has no filesystem): it **returns** StageResults to the supervisor on Workflow completion, and the **supervisor persists them via `orchestrator record` (Bash)**. The engine (a Python CLI) never calls `agent()` itself — this is the supervisor→engine→shim architecture. |
| headless × claude | **Agent-SDK / `claude -p` CLI runner** | subprocess; always-works fallback; bills credit/API (derived) |
| any × codex | **`codex exec` runner** | bills OpenAI (derived) |
| codex × interactive | **EXPLICIT_EMPTY** | registry declares it unsupported; `resolve()` fails loudly |

An in-process **registry** maps a `lane_policy` → the runner serving that cell (with a
`CapabilityDescriptor`: streaming?, schema_enforced?, cost_metered?), and lets one runner
serve multiple cells. A pre-run `assert_cells_covered()` check fails fast if the active
mode's required cells have no runner (mitigates A2's runtime-registration weakness).

**Cost by construction (closes D6):** every dispatch — interactive subagent, headless
`claude -p`, codex, *or the former one-shot path* — produces exactly one StageResult, and
`orchestrator record` writes exactly one ledger row keyed by `lane_used` + `token_usage`.
There is no code path that runs a model without a StageResult, so an unattributed call is
structurally impossible.

**Codex heuristic tightened (fix-forward):** the codex runner sets `status=success` only on
**full schema validation** of the structured output (not the as-built top-level-keys-present
heuristic), plus a non-empty result; `git HEAD moved` is evidence but not sufficient alone.

**`content_hash` / `attempt` invariant:** retry mutates the prompt (learnings are appended),
so a retry's WorkItem already hashes differently; including `attempt` in the hash makes this
guaranteed even if the rendered prompt were byte-identical — so a re-dispatch never collides
with the prior attempt's idempotency key.

**Resumability:** the engine advances a task only after `orchestrator record` ingests a
`success` StageResult, so a session death leaves a WorkItem un-recorded (re-dispatched) or a
StageResult already recorded (advanced) — never a half-counted stage. A lease/heartbeat on
in-flight WorkItems distinguishes a slow call from a dead one (fixes A3's stale-claim race).
**Interactive corollary:** because the in-session shim only persists results *on Workflow
return* (it can't write mid-run), **resume granularity in interactive mode is the dispatch
batch** — a session death mid-batch re-runs the whole batch. Size dispatch batches accordingly
(small enough that re-running one is cheap), and have the lease/heartbeat cover the in-flight
batch, not just a single WorkItem. Headless runners persist per-WorkItem, so their resume
granularity is finer.

> **Billing matrix** (derived, never branched in code): interactive×claude → subscription
> (default); headless×claude → credit/API; any×codex → OpenAI; codex×interactive → n/a.

---

## 5. Project-config adapter

What a repo plugs in (the Hey Soo! values become the reference adapter). A single
`project.toml`/plugin module providing:
- **Commands:** `INSTALL_CMD`, `TEST_UNIT_CMD`, `TEST_E2E_CMD`, `TEST_SHELL_CMD`, `TYPECHECK_CMD` (the TSC gate generalized), `INFRA_RESET` hook.
- **`TEST_TAXONOMY`** — the test-file conventions the engine's change→test-set mechanism consumes: how to recognize unit vs e2e vs shell tests and map a changed source file to its impacted tests (Hey Soo! impl: `.spec.ts`→e2e, `test_*.py`/`conftest.py`→unit, `*.bats`→shell, plus dir layout). Engine owns the mechanism; this owns the conventions.
- **`FAILURE_CLASSIFIER`** (build-fresh, D5) — implements the `failure_classifier` interface;
  the Hey Soo! impl ports the Jest/pytest/`^tests/e2e/`/`.spec.ts` regexes.
- **Task-source provider** (build-fresh, D8) — `resolve(id) → TaskSpec` and `mark_complete(id)`.
  **GitHub-Issues is the reference impl** (the live source); roadmap-markdown is dropped.
- **Agent roster** — the per-stage agent map (review/implement/fix), incl. a **generic
  docstring agent** for the `deliver` stage (fixes the `phpdoc-writer` bug, D13) and the
  PR-finalize agent; `bulletproof-frontend`/`ui-design-fundamentals` are product-specific
  drop-ins.
- **Directory hints** + optional **review plugins** (API-contract / E2E-coverage) as
  drop-ins under a `hooks/` dir.
- **Bootstrap:** the `adapting-claude-pipeline` skill's Keep/Modify/Replace/Delete audit
  generates this adapter for a new repo.

---

## 6. Banked fixes

### 6.1 Collapsed stage map (real ~12–13 → 6, each dispositioned)
Every as-built stage maps into exactly one target stage:
| Target stage | Collapses (as-built) | Notes |
|---|---|---|
| **intake** | extract + setup (+ baseline capture) | task-source resolve → worktree/branch → baseline; pure-shell where possible |
| **scope** | research + evaluate + plan | the design-doc headline collapse: one reasoning stage that understands, decides feasibility (can still exit `blocked`), and emits the task plan |
| **implement** | implement + quality_loop | per-task implement with an **adaptive** review/quality loop. **Must tolerate a missing `scope` output** — in `lite`/`micro` mode `scope` is skipped (as-built synthesizes a 1-task plan), so `implement` falls back to a synthetic single-task plan from the task spec rather than assuming `scope` ran |
| **test** | test_loop + verify | adaptive test/fix loop |
| **deliver** | docs + pr | generic docstring agent (fix D13); open PR |
| **review** | pr_review + complete | PR-review loop + completion gate |

Mode machinery (port): `lite` runs intake/implement/test/deliver/review (skips scope);
`micro` runs intake/implement/deliver/review (skips scope + test) — the skip-lists become
"which collapsed stages run."

### 6.2 Adaptive loop caps (complete D15)
The convergence detector drives the loop, with the as-built numeric caps demoted to **safety
ceilings**, not the operating point. Convergence is wired into **implement, review, AND the
test loop** (the as-built only wired quality + PR review — completing the half-built D15).
The structured circuit breaker (signature plateau) and infra breaker remain as hard stops.

### 6.3 Single model/pricing config table (fix D3/D4)
One `model_table` with current model ids + **current prices** (replacing the stale $15/$75
Opus and the `opus-4-7`/`sonnet-4-6` pins). Tier mapping (deep-reason / review / cheap-shell)
references this table by role, not by literal id, so a model bump is a one-line change.

### 6.4 Cost ledger records every call (close D6)
Per §4/§7: every model call → a StageResult → one ledger row. The one-shot path is a normal
WorkItem. The per-run cost summary is computed from the ledger using the single price table.

### 6.5 Session / prompt-cache reuse
The collapsed stages that chain (scope's three former stages; implement's review→fix) are
candidates to run in one session to exploit prompt-cache reads (~10% input rate) — a noted
optimization point for Phase 3, not a correctness requirement.

---

## 7. Versioned status-file schema (resumability contract)

**Synthesized from judge-panel (b): normalized run/task/stage (B2) + reader-tolerant
versioning (B1) + per-call cost as an append-only audit sidecar (B3).** Task status is the
single source of truth; run-level aggregates are a derived cache.

**Run doc** `status-<run>.json`:
```
schema_version, document_type:"run", run_id, created_at, updated_at,
status, mode, task_refs[ {task_id, status_file, status(cache)} ],
dependency_graph { task_id: [task_id…] }, metadata
```
(`progress` counters are **derived** from task_refs, not stored — fixes redundancy.)

**Task doc** `status-<run>-<task>.json`:
```
schema_version, document_type:"task", task_id, run_id, created_at, updated_at,
status, attempt, max_attempts, title, issue_number, pr_number, pr_url,
current_stage, stages{ intake, scope, implement, test, deliver, review },
resume_cursor { stage, hint }
```

**Stage record (uniform — no special-case omissions; fixes the `started_at` defect):**
```
status ∈ {pending,running,completed,skipped,failed},
started_at (null until running — ALWAYS PRESENT), completed_at,
attempt, model|null, provider|null, lane|null,
cost_usd|null, input_tokens|null, output_tokens|null,
error|null, output (stage-specific payload), iteration|null (adaptive loops)
```
Putting model/provider/cost/tokens on the **stage record** makes cost traceable to the exact
stage+model and closes D6 at the schema level (each `StageResult` → one stage record update).

**Resumability:** read run doc → for each non-terminal task read `resume_cursor` → re-enter
at the first stage whose `status ∉ {completed, skipped}`; a `running` stage with `started_at`
set and `completed_at` null is a crash marker → bump `attempt`, re-run (stages idempotent).
PIDs nulled on resume.

**Versioning (reader-tolerant):** top-level `schema_version`; a v1 reader backfills a v0 file
(add `started_at:null`, drop `brainstorm_*`, re-key old stage names → the 6) and treats it as
v1. **Open issue flagged for review:** the stage re-key is the one genuine breaking change
(B1-W1) — any external v0 reader loses history; we accept it because there are no external v0
readers of *this* template's files (the as-built files stay in Hey Soo!).

**Audit sidecar (optional, append-only):** `events.jsonl` of `cost_recorded` / `stage_*` /
`lane_assigned` events (B3) gives an append-only audit trail + cheap concurrent writes for
the cost ledger, **without** making the status files a projection — the status docs remain
the directly-readable truth. Compaction/query tooling is explicitly deferred.

---

## 8. Python package layout (uv / pytest / ruff)

```
orchestrator/                 # engine core — importable, no model calls
  status_store.py  state_machine.py  dag.py  retry.py
  cost_ledger.py   model_table.py    capacity.py  failure_classifier.py  (interface)
  schemas/         # WorkItem, StageResult, run/task/stage JSON-Schemas (versioned)
  cli.py           # `orchestrator ready|next|record|resume|status`
adapters/
  execution/       # workflow_shim (interactive×claude), claude_cli (headless), codex_exec
  project/         # base interface + heysoo/ reference (commands, classifier, task source, roster)
run_targets/
  workflow_shim.*  # the in-session JS/skill fan-out that calls agent() and writes StageResults
  supervisor_skill # the thin (~1pg) interactive skill: ready → dispatch → record → repeat
  headless_cli     # Agent-SDK driver for the headless lane
tests/             # pytest; ports the engine-logic BATS *cases* (fragment 14) as fixtures
pyproject.toml     # uv-managed; ruff + pytest
```
Engine core is built with TDD; independent leaf modules (cost_ledger, status_store,
classifier, schema) fan out to subagents in worktree isolation once §3/§7 interfaces are
fixed. Run targets stay thin (the deterministic logic lives in the engine; the shim only
calls `agent()` and records results).

---

## 9. Deferred scope

Seeded in `../../DEFERRED.md` from every `defer` row above. See that file for the full ledger.
