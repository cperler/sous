# Orchestration Template — Implementation Plan

> Operationalizes `docs/orchestration-template.md` §6 into sequenced, executable
> phases. Resolves the §7 open decisions. Written 2026-06-12. Status: **awaiting
> review — no build work started.**

---

## 0. Decisions

### Decision 1 — Engine language: **Python** (revised from initial TS lean)

The load-bearing fact: Workflow scripts run in a sandbox with **no filesystem
access, no Node APIs, no clock** — so the engine's core job (status files,
cost ledger, resumable state) cannot live inside a Workflow script in *any*
language. The forced interactive-mode architecture is supervisor → engine CLI
via Bash ("what's ready?") → Workflow fan-out → engine CLI records results.
In that architecture the engine is a Bash-invoked CLI either way, which
neutralizes TypeScript's headline "no seam" advantage. What remains is:

- The Claude **Agent SDK ships a first-class Python package**, so the headless
  run target is equally native in Python.
- Maintainer fluency: Craig reads/writes Python, not TS — decisive for a
  personal template he'll own and debug for years. (The bash option was
  rejected partly on maintainability; an unfamiliar language re-creates that
  problem in a different costume.)
- One toolchain with the existing stack (Lambda, CDK, uv).

Residual cost: the per-batch Workflow fan-out scripts remain short JS shims.
That seam exists under TS too in all but name; making it a *named, tested
artifact* (the "Workflow shim", Phase 3a deliverable) keeps it from being an
afterthought.

### Decision 2 — v1 scope: **MVP-first, with a deferred-scope ledger**

The rebuild intentionally *changes* the design (collapsed stages, adaptive
loop caps, session reuse), so "full parity" is parity with a shape we've
already decided to abandon — it would port forward exactly the
over-prescription §2 flags. MVP-first gets a real task through the new
paradigm fastest, which is also the earliest test of the riskiest assumption
(that in-session Workflow execution actually sustains a multi-task batch).

**Condition (per review):** everything cut from the MVP must be visibly
tracked, not silently dropped. Mechanism — `DEFERRED.md`, see §6a. The Phase 2
traceability table is its seed: every `defer` row carries forward into the
ledger automatically.

### Surfaced sub-decisions (recommendations stated; flag at review if you disagree)

| # | Decision | Recommendation |
|---|---|---|
| D3 | Is the ralph scheduler in v1 or a fast-follow? §2 calls the scheduler "the differentiated value," but the MVP-first option deferred it. | **In v1, as Phase 3b.** MVP lands the per-task pipeline first (3a), then the scheduler core (DAG, concurrency, retry-with-learnings, cascade) on top — both before the second execution mode. Deferring the differentiator past v1 would make v1 a clone of what the Agent SDK already commoditizes. |
| D4 | Where does the template live? | **New standalone repo** `../orchestration-template`, created at the start of this work and seeded with the two design docs; Phases 1–2 artifacts are authored there from the start under `docs/orchestration-spec/`. The reference `.claude/` system is read in place via absolute paths, never copied in (deliberate Python rebuild, not a bash fork). Dogfooding Hey Soo! as an *external* adapter is itself the generality test. |
| D5 | `monitor-orchestrator.sh` (2,254 lines, absent from §8) and `batch-orchestrator.sh` (842 lines, likely superseded by ralph) — port or drop? | **Map both in Phase 1, decide in Phase 2.** Default lean: batch-orchestrator is legacy → spec it only to confirm ralph subsumes it; monitor-orchestrator's observability may partially be replaced by `/workflows` live progress → port only what the in-session paradigm doesn't give for free. |
| D6 | §8 inventory is stale (`implement-orchestrator.sh` is 2,217 lines, not ~800; monitor-orchestrator missing; 10 lib helpers, not 9). | Treat the §8 table as a hint, not ground truth. Phase 1 produces the authoritative inventory. |

---

## 1. Phase 1 — Ground-truth extraction (as-built spec)

**Deliverable:** `docs/orchestration-spec/fragments/*.md` (one verified spec
fragment per source unit) synthesized into `docs/orchestration-spec/as-built.md`.
First reviewable artifact.

**Execution model:** in-session Workflow — a find → verify → synthesize
pipeline. This is deliberately the first dogfood of the paradigm the template
bets on.

### 1.1 Inline scout (main session, before fan-out)

- `grep -nE '^[a-zA-Z_]+\(\)' .claude/scripts/lib/orchestrator-common.sh` to
  get the full function inventory; partition it into two balanced, themed
  halves for agents 2a/2b below.
- Confirm final file list + line counts (already done once: 15,570 total lines
  across `.claude/scripts/`).

### 1.2 The fan-out — 9 mapping agents (parallel, read-only)

| # | Agent scope | Source | ~Lines |
|---|---|---|---|
| 1 | Batch scheduler | `ralph-loop.sh` | 2,299 |
| 2a | Shared engine, half A: `run_stage`, provider routing, model tiering + fallback chain, codex adapter, cost tracking | `lib/orchestrator-common.sh` (function list from scout) | ~2,200 |
| 2b | Shared engine, half B: quality/test loops, convergence detector, learnings, capacity throttle, status helpers | `lib/orchestrator-common.sh` (remainder) | ~2,150 |
| 3 | Per-task pipeline | `implement-orchestrator.sh` | 2,217 |
| 4 | Monitor | `monitor-orchestrator.sh` | 2,254 |
| 5 | Legacy batch mode (unique-features-only mandate) | `batch-orchestrator.sh` | 842 |
| 6 | Lib helpers A | `status-file-helpers`, `classify-failures`, `regression-helpers`, `detect-change-scope`, `track-test-commit` | ~715 |
| 7 | Lib helpers B | `run-tests-direct`, `build-incremental-test-prompt`, `select-incremental-test-agent`, `port-registry`, `reset-infra` | ~725 |
| 8 | Schemas: field tables + which stage consumes each | all 17 `schemas/*.json` | — |
| 9 | Entry points & periphery | `implement-roadmap-task-orchestrator.sh`, `implement-issue-orchestrator.sh`, `batch-runner.sh`, `extract-roadmap-task.sh`, `update-roadmap-status.sh` | ~722 |

Agents 2a/2b each also list every call they make into the other half, so the
synthesizer can stitch the seam.

### 1.3 Fixed spec-fragment template (every agent fills exactly this)

```markdown
# Fragment: <file(s)>
Source commit: <sha>   Mapped lines: <range(s)>

## 1. Role & entry points — who invokes it, with what argv
## 2. Inputs — every flag, env var (name / default / effect), file read
## 3. Outputs — every file written (path, format, EVERY field), exit codes, side effects (git, gh, network)
## 4. Control flow — state machine: states, transitions, loop structure with exact caps and exit conditions
## 5. External invocations — every claude/codex/gh/git command VERBATIM with flags, model, schema
## 6. Constants & tunables — numeric caps, timeouts, sleeps, pricing, model pins
## 7. Failure handling — retries (count/backoff), fallback chains, circuit breaker, cascade rules
## 8. Coupling — per item: generic vs Hey Soo!-specific (with the generic shape it should take)
## 9. Anomalies — suspected bugs, dead code, contradictions with docs/orchestration-template.md
```

Hard rule: **every claim in §§4–7 cites `file:line`.** This is what makes the
verify stage cheap and mechanical.

### 1.4 Verify stage (pipelined — each fragment verified as it completes)

One adversarial verifier per fragment: re-opens the source, spot-checks every
line citation, and is prompted to *refute* — find caps, env vars, transitions,
or external calls the fragment missed or misstated. Material misses go back to
a fix pass (same workflow, one repair agent per failed fragment).

### 1.5 Synthesis (single agent, all fragments + design doc as input)

Produces `as-built.md`, organized by subsystem (scheduler / stage engine /
per-task pipeline / monitor / helpers / schemas), plus cross-cutting
registries the fragments can't see alone:

- env-var registry (all knobs, defaults, which scripts read them)
- status-file field registry (`status-ralph.json` + per-task files, every field, who writes/reads)
- model + pricing table (current pins vs. stale ones — feeds the §5 fix)
- schema ↔ stage matrix
- call-count audit (verify the "~50 full / ~20–25 lite / ~10 micro" claims)
- **discrepancy log** vs. `orchestration-template.md` (D6 items + whatever else surfaces)

Then a completeness critic: diff the scout's function inventory and an
env-var grep against the spec — anything unaccounted for becomes a repair
round.

### Done-criteria

- Every file in the authoritative inventory has a verified fragment; zero
  unrefuted material errors outstanding.
- Function-inventory and env-var coverage checks pass.
- `as-built.md` answers, from the spec alone: how does the circuit breaker
  trip? what's the capacity-throttle math? what's the codex success heuristic?
  what unlocks a cascade-blocked task?
- You review and sign off (especially the discrepancy log and the D5 verdict inputs).

---

## 2. Phase 2 — Target spec

**Deliverable:** `docs/orchestration-spec/target.md` — implementation-agnostic
spec of the new system, reviewable before any code.

**Work items:**
- Engine/adapter split per §5: engine API surface (state machine, DAG,
  retry-with-learnings, cost ledger), execution adapter as **two orthogonal
  axes** — `execution_mode ∈ {interactive, headless} × provider ∈ {claude,
  codex, …}`, billing a derived property of the (mode, provider) pair, empty
  cells (e.g. codex-interactive) left explicit — project-config adapter
  interface (`INSTALL_CMD`, `TEST_UNIT_CMD`, failure classifiers, review
  plugins as drop-ins).
- Bank the §5 fixes explicitly: collapsed stage map (research/evaluate/plan →
  one stage; final stage list ~5–6, each dispositioned), adaptive loop caps
  driven by the convergence detector, single model/pricing config table,
  deliberate session/prompt-cache reuse points.
- **Traceability table:** every as-built behavior → port / collapse / drop /
  defer, with one-line rationale. This is the parity ledger that makes
  MVP-first safe — nothing gets lost silently. (D5 verdicts land here.)
- Versioned status-file schema (the resumability contract that keeps execution
  modes interchangeable — §4 friction point 1).
- Python package layout: engine core as an importable package with a CLI
  surface (the supervisor's Bash entry point), run targets as thin shells
  (Workflow shim + supervisor skill for interactive; Agent-SDK CLI for
  headless), adapters as plugins. uv-managed, pytest.
- Seed `DEFERRED.md` from the traceability table's `defer` rows (§6a).

**Execution:** authored inline in the main session (judgment-heavy; needs one
coherent voice). Two narrow judge-panel workflows where the design space is
genuinely wide: (a) engine ↔ execution-mode adapter boundary, (b) status-file
schema — 3 independent design attempts each, scored, best synthesized.

**Done-criteria:** traceability table has no "TBD" rows; the spec contains
enough detail that Phase 3 modules can be built by subagents from spec
sections alone; you sign off.

---

## 3. Phase 3 — Build: engine + interactive mode (MVP)

**Deliverable:** new template repo; one real Hey Soo! task lands a PR driven
end-to-end by the in-session engine; then a 3-task batch with the scheduler core.

### 3a — Engine core + per-task pipeline

- Scaffold repo: Python, uv, pytest, ruff. Engine core modules: status store,
  state machine, cost ledger, config/model table, failure classifier
  interface — importable package + CLI surface (`engine ready`, `engine
  record`, etc.) for the supervisor's Bash calls.
- **Workflow shim** (named deliverable): the short JS fan-out script the
  supervisor dispatches — `agent()` per stage with the ported JSON schemas
  (Workflow's `schema` option replaces `--json-schema`), adaptive verify loop
  replacing fixed quality 5×3 / test 10×2 caps. Kept thin and templated; the
  engine CLI computes everything the shim needs ahead of dispatch.
- Project-config adapter interface + the Hey Soo! reference adapter.
- Port BATS test *cases* (not the bash) as pytest fixtures for engine logic —
  the regression suite carries over even though the implementation doesn't.

**Execution:** engine core built inline with TDD (deterministic, coherence
matters). Independent leaf modules (cost ledger, status store, classifier,
schema port) fan out to parallel subagents in worktree isolation once
interfaces are fixed in the target spec. Integration + the live end-to-end run
happen inline.

**Done 3a:** pytest green; one real Hey Soo! roadmap task goes
issue → implement → verify → PR entirely via in-session execution; **audit
confirms every model call runs on its intended lane and is attributed to it** —
no hidden/unattributed `claude -p` surprising the cost ledger; a deliberately
selected `claude -p` stage is fine and tracked (§4 watch-item).

**Feedback edge (3a → Phase 2):** if the collapsed-stage bet (research/evaluate/
plan → one stage; adaptive caps) **regresses quality vs. the historical 11-stage
runs**, loop back to revise the Phase 2 stage map before proceeding — the collapse
is a hypothesis, not a commitment.

### 3b — Scheduler core (the differentiator, per D3)

- DAG + dependency analysis, `MAX_CONCURRENT` dispatch, retry-with-learnings,
  cascade-blocking, circuit breaker — as engine code called from a Workflow
  script, not prose.
- The thin skill (~one page, per §4): load status via engine helper → dispatch
  ready tasks as workflows → run unlock check on completion → repeat.
- Resumability: kill the session mid-batch, start a new one, resume from
  status files.

**Done 3b:** a 3-task batch with one induced failure demonstrates
retry-with-learnings firing, cascade-blocking the dependent, and clean resume
after a deliberate session kill.

---

## 4. Phase 4 — Second execution mode + codex

**Deliverable:** the execution-mode adapter proven by ≥2 real implementations;
a billing line-move is now a config change.

**Work items:**
- Headless run target: Python CLI driving the same engine via the **Python
  Agent SDK** (explicitly the metered lane — the always-works fallback).
- Codex adapter: port `codex exec` routing (global + per-task tags,
  `CODEX_ELIGIBLE_STAGES`), **with the tightened success heuristic** — full
  schema validation, not required-keys-present (§2 fix 5, now load-bearing).
- Conformance suite: the same task spec, run through both modes with only a
  config flip, must produce an **identical stage-DAG traversal and identical
  terminal disposition per task**. The diff **excludes non-deterministic fields**
  (token counts, iteration counts, wall-clock durations) — equivalence is about
  control-flow and outcome, not byte-identical telemetry.

**Execution:** headless target inline (touches engine seams); codex adapter
fans out (well-specified, isolated). Conformance runs inline.

**Done-criteria:** same task passes through interactive and headless modes
unchanged; codex-routed implement stage passes full schema validation;
mode selection is config-only.

---

## 5. Phase 5 — Dogfood + generalize

**Deliverable:** template proven on two projects.

**Work items:**
- Run a real multi-task Hey Soo! batch as the ralph replacement (tmux-on-host
  per the §4 operational note); compare cost ledger vs. historical
  `stage-costs.jsonl` to quantify the session-reuse/cache win.
- Second, unrelated project: write its adapter; port the
  `adapting-claude-pipeline` skill as the bootstrap that generates adapters
  for new repos.
- Retrospective → fold learnings back into the template docs.

**Done-criteria:** second project completes a task with changes confined to
its adapter config; bootstrap skill produces a working adapter skeleton;
retrospective written.

---

## 6. Cross-cutting rules

- **Review gate at every phase boundary** — Phases 1 and 2 end in documents,
  3–5 in demonstrated runs; nothing proceeds past a gate without sign-off.
- **Every model call runs on its intended, attributed lane** — re-audited at
  each phase gate, not just 3a. `claude -p` is a supported (non-default) mode;
  the failure is a *hidden/unattributed* headless call, not a deliberate one.
- Phases 1–2 artifacts live in this repo under `docs/orchestration-spec/`;
  Phase 3 creates the template repo and the spec (and `DEFERRED.md`) move
  with it.

### 6a. Deferred-scope ledger (`DEFERRED.md`)

The MVP-first condition: nothing cut is silently dropped. One file, one row
per deferred item:

| Item | Source (as-built §) | Why deferred | Earliest phase it could land | Trigger to revisit |

- **Seeded in Phase 2** from every `defer` row in the traceability table —
  the table is the single funnel, so nothing reaches "deferred" without a
  ledger entry.
- **Reviewed at every phase gate** (standing agenda item alongside the
  `claude -p` audit): each row is re-dispositioned — promote into the next
  phase, keep deferred, or retire with a written reason. Retired rows move to
  a "retired" section rather than being deleted, so the decision history
  survives.
- Known candidates going in (from the design doc, so they're captured even
  before Phase 2 runs): queue-file ingestion / unattended mode (credit lane),
  capacity-throttle jitter tuning, monitor-orchestrator observability
  (pending D5), codex routing (Phase 4), retrospective auto-generation,
  per-stage cost summary reports.
