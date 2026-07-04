# Orchestration Template — Design Notes & Extraction Plan

> **HISTORICAL (June 2026).** The pre-build design doc, kept as the decision record.
> The system has since been built well past it — see `ARCHITECTURE.md` for the as-built
> map and `docs/orchestration-spec/target.md` for the spec.

> Working document. Captures the assessment of Hey Soo!'s existing `.claude/`
> orchestration system, the analysis of the June 15 2026 billing change, the
> strategy for adapting to it, and the plan to extract the system into a
> reusable, project-agnostic template. Written 2026-06-11.

---

## 1. Purpose

Extract the orchestration system built incrementally inside Hey Soo!'s `.claude/`
directory into a standalone, reusable template for other projects — and in doing
so, rebuild it for the **anticipated** post-change world where `claude -p` (headless)
usage would no longer be the cheap default (the June 15 2026 reclassification was
announced then paused — see §3; we design for it as the likely direction, and because
the same engine/adapter split is the architecture win). The original was built one step at a time; we now know
what works and what we want, so the rebuild should be deliberate rather than
accreted.

---

## 2. Assessment of the existing orchestration system

### What it is

A two-layer system:

- **Batch scheduler** (`ralph-loop.sh`, ~2,300 lines): dependency-aware concurrent
  task orchestration. Maintains a dependency DAG, launches up to `MAX_CONCURRENT`
  tasks, retries failed tasks with learnings prepended, ingests new batches from a
  queue file, throttles launches on API capacity, cascade-blocks dependents of
  failed tasks.
- **Per-task orchestrator** (`implement-orchestrator.sh`, ~800 lines, +
  `orchestrator-common.sh`, ~4,347 lines shared engine): an 11-stage pipeline per
  GitHub issue / roadmap task — setup → research → evaluate → plan → implement →
  quality_loop → test_loop → docs → pr → pr_review → complete.

Each stage is an independent `claude -p` invocation:
```
claude -p "$prompt" --model "$model" --dangerously-skip-permissions \
  --verbose --output-format stream-json --json-schema "$schema" [--agent "$name"]
```
A full-mode task makes ~50 model calls (worst case, with retry/review loops);
lite ~20–25; micro ~10.

### What's genuinely strong (best-in-class for a homegrown harness)

- Dependency-DAG scheduler with cascade-blocking and circuit-breaker on identical
  repeated failures.
- Retry-with-learnings: failure summaries from prior attempts are prepended to
  retry prompts.
- Per-stage model tiering (Opus for deep reasoning, Sonnet for review, Haiku/shell
  for setup) with a rate-limit fallback chain (Opus → Sonnet → Haiku).
- Convergence detector that exits quality loops early instead of running to the
  worst-case cap.
- Per-stage cost accounting to `stage-costs.jsonl`, aggregated into a cost summary.
- Clean two-axis provider routing: global `ORCHESTRATOR_PROVIDER` (wholesale swap
  to Codex) plus per-task `:codex` tags (surgical routing of file-patching stages
  only). Analytical stages stay on Claude.
- File-based status tracking (`status-ralph.json`, per-task status files) enabling
  resume-after-failure.
- Real observability: stage index, per-stage logs, stream replay files, learnings
  file, convergence tracking, auto-generated retrospectives.

### Improvement areas identified

1. **No session reuse — every stage is a cold `claude -p`.** ~50 independent
   invocations per task, each re-paying system prompt + context setup. Chaining
   related stages through one session (e.g. research→evaluate→plan, or review→fix
   within a quality iteration) would yield prompt-cache hits (cache reads ~10% of
   input rate) and eliminate redundant context re-reading. The cost tracker already
   *records* cache tokens but nothing deliberately exploits them. **Largest cost
   lever requiring no architectural change.**
2. **Over-prescriptive pipeline for current models.** The 11-stage split and
   fixed worst-case loop caps (quality 5×3, test 10×2) suited older models.
   Opus 4.8-class models do better with goal-plus-constraints than enumerated
   steps and self-verify well; Anthropic's own migration guidance says
   scaffolding tuned for older models often *reduces* quality on newer ones.
   Collapse research/evaluate/plan into one stage; make loop counts adaptive on
   the convergence signal you already detect rather than worst-case-capped. Fewer
   smarter calls beats many cheap ones.
3. **Stale pricing and model pins.** `emit_cost_summary` prices Opus at $15/$75 —
   current Opus is $5/$25 per MTok, so cost reports overstate ~3×. Stages pin
   `claude-opus-4-7` while CLAUDE.md says `4-6` and 4.8 is current. Consolidate
   model/pricing into one config table.
4. **Codex success heuristic is thin.** `run_codex_stage` validates only that
   top-level required keys exist (not full schema conformance); patching-stage
   success is "git HEAD moved." Fine as a fallback path; risky if Codex becomes a
   primary route — tighten validation first.
5. **~10K lines of bash is at the maintainability ceiling.** Well-mitigated (BATS
   tests, shellcheck, shared lib) but every new feature gets harder. Shapes the
   template language decision (§6).

### Coupling: what's generic vs. Hey Soo!-specific

- **Nearly repo-agnostic (extract ~as-is):** `ralph-loop.sh` scheduler, `run_stage`
  / provider routing / capacity throttle / cost tracking in `orchestrator-common.sh`,
  the per-task pipeline skeleton, the Codex adapter, the 17 JSON schemas.
- **Hey Soo!-specific (needs adapter/parameterization):** test commands
  (`test-unit.sh`, `e2e-smoke.sh`), dependency install (`npm install` + `uv sync`),
  failure-classification regexes (Jest/pytest patterns), API-contract and E2E-coverage
  review steps (hardcoded to frontend/lambda dirs), the agent roster
  (`code-reviewer`, `python-backend-developer`, etc.), roadmap task numbering.

**Conclusion:** the differentiated value is the *scheduler* (Ralph: dependency-aware
batch scheduling, retry-with-learnings, capacity throttle, cost discipline). The
per-task plan→implement→test→PR pipeline is being commoditized by the Agent SDK,
cloud agents, and GitHub Actions. Center the template on the scheduler and keep the
stage engine swappable.

---

## 3. The anticipated billing change (announced, then paused)

> **Status (current).** The June 15 2026 reclassification was **announced and then
> paused on the day it was due to take effect.** As of this writing `claude -p`,
> the Agent SDK, and third-party usage **still draw from the subscription pool** —
> **there is no separate credit pool yet.** Anthropic has said it is reworking the
> plan and will give advance notice before anything changes. So the change below is
> **anticipated and likely to return in some form, not current fact.** We treat the
> post-change world as the design center because it is also the architecture win
> (the engine/adapter split) — but the rebuild's justification rests on the §2
> grounds (cache reuse, collapsed stages, adaptive caps, stale-pricing fix, bash
> maintainability, generalization), **not** on billing being a present-day forcing
> function.

### What was announced (the shape to design for)

- `claude -p` (headless), the Agent SDK, Claude Code GitHub Actions, and
  third-party ACP clients would move **out of** the subscription usage pool **into**
  a separate monthly dollar credit — $20 (Pro) / $100 (Max 5x) / $200 (Max 20x) —
  metered at standard API token rates, **no rollover**. When the credit is gone,
  headless calls either fail or spill to usage credits at API rates if explicitly
  enabled.
- **Interactive Claude Code (the TUI) is explicitly unaffected** — it keeps drawing
  from the normal subscription limits.
- Standard Enterprise seats get $0 credit; only usage-based ($20) and Premium ($200)
  Enterprise seats include it.

Sources: pravinkumar.co, digitalapplied.com, techtimes.com, codersera.com,
thenewstack.io (June 2026); pause confirmed on the effective date.

### The correct mental model (this took the conversation a few passes to sharpen)

- `claude -p` was **never unlimited.** Under a subscription it always drew from the
  same rate-limit pool as the TUI — same 5-hour rolling windows, same weekly caps.
  It was never a way to get *more* tokens.
- So June 15 is a **pool reassignment keyed on which binary you invoke**, not a
  change in entitlement. Move the invocation from `claude -p` to interactive
  `claude` and you're back on the identical constraints you already throttle against.
- The "subsidy" Anthropic cited wasn't usage exceeding the caps — it was the gap
  between the flat fee and the API-equivalent value of usage the caps *already
  permitted*. A continuously-running agent pegged near the rate limit extracts
  hundreds of dollars of API-equivalent compute for $20/month. **Headless is the
  behavioral proxy for sustained high utilization** (humans pause and sleep; cron'd
  scripts don't), and the invocation surface is how they detect it cheaply.
- The change accomplishes three things, only one of which touches a solo hobbyist:
  1. **Unattended launch** — cron/CI with no human present (the slice a hobbyist who
     hand-launches batches never used).
  2. **Embedding/resale** — third-party products piggybacking on users' consumer
     subscriptions; remote-runner workloads (GitHub Actions) disconnected from any
     subscriber's machine. Arguably the bigger commercial driver.
  3. **Precise metering** of machine-scale consumption — converting the heaviest,
     most cost-variable traffic from flat-fee-and-hope-the-limits-hold to
     metered-and-margin-positive.

### What would be genuinely lost (under the announced change)

Only **unattended launch** — firing a batch with no human present (cron at 2am).
Everything else (throughput, fan-out, autonomy once running) is unchanged, because
it was always bounded by the same rate limits. For the "kick off a batch before
bed, let it grind within the caps" habit, the cost is **zero** — you were already at
the keyboard to start it. The credit exists to buy back exactly the unattended slice.

---

## 4. The adaptation strategy

Flip orchestration from headless `claude -p` to an **interactive session that fans
out via in-session subagents / the Workflow tool**, which rides the subscription.

### Why this works (and why it's not a loophole)

- Billing attaches to the **entry point that authenticated**, not to the work
  spawned downstream. An interactive `claude` session is not on the reclassified
  list. In-session subagents don't re-authenticate or open a new billable surface —
  they draw on the same session's pool.
- It's sanctioned: Anthropic keeps adding orchestration primitives *to* the session
  (subagents, Workflow, background tasks, loops). The session is "human-scale usage
  at a flat rate," and the in-session fan-out is the metered lane they left open.
- It's self-limiting, which is why they can leave it open: everything in the session
  draws from one human's rolling caps. You cannot buy your way to more throughput in
  this lane — the only lever is "another human with another plan." Anyone whose
  demand exceeds human-scale gets pushed back to metered billing automatically.

### Terminology (clarified, because the words are overloaded)

- **Subagent** — what the Agent tool spawns *inside* your session: a child agent
  loop, fresh context, own tools, optionally own model, runs in the same Claude Code
  process on your machine. Workflow `agent()` calls are the same thing with
  deterministic plumbing. **These ride the subscription.**
- **Agents** (`.claude/agents/*.md`) — just *definitions* (persona + system prompt +
  tool policy). A subagent is an instance of one. No billing significance.
- **Agent SDK** (headless library) and **Managed Agents** (server-hosted,
  `client.beta.agents`) — separate API/SDK surfaces, squarely in the metered lane.
  Not the same as in-session subagents.

### The one concrete watch-item

`claude -p` is **a fully supported execution mode, not a banned one** — it is simply
not the *default*. In-session interactive (subagents / Workflow) is the default and
rides the subscription; headless `claude -p` is the always-available alternative
(under the anticipated change it bills against credit / at API rates — a **cost
property, not a prohibition**).

The watch-item is therefore **accidental, unattributed headless calls**, not
deliberate ones: a subagent that *silently* shells out to `claude -p` puts a call on
the headless lane that the cost ledger never attributed there, surprising the
billing. So the rule is **attribution, not abstinence** — every model call must run
on its *intended* lane and be recorded as such. Deliberately routing a stage to
`claude -p` is fine and tracked; a hidden one is the bug. Codex stays a Bash call
(`codex exec`) and bills OpenAI separately — also fine, also attributed.

### Friction points of the interactive paradigm (design for these)

1. **Resumability.** A dead session is messier to recover than re-running a bash
   script. Mitigation: keep file-based status tracking exactly as-is so any new
   session (or bash ralph on credit) resumes from `status-ralph.json`. This also
   keeps execution modes interchangeable.
2. **No unattended queue mode.** Cron-ability / `ralph-queue.json` ingestion while
   asleep is genuinely gone from the interactive lane — that's what the credit is for.
3. **Shared rate-limit pool, hub-and-spoke parallelism.** Not a *new* constraint
   (bash ralph drew from the same pool — that's why capacity throttling exists). The
   change is mechanical: bash ralph = 3 independent OS processes; interactive = one
   supervisor dispatching subagents (concurrency ~10–16 per workflow). Binding
   constraint is the token caps either way; the real difference is the supervisor as
   a single point of failure → see (1).
4. **Supervisor context growth + token cost.** Bash supervised for free; an
   in-session supervisor's context grows over a multi-hour batch and compaction
   degrades fidelity when you're asleep. Mitigation: keep the supervisor thin —
   subagents hold heavy per-task context and return summaries; the supervisor only
   tracks state and dispatches.

### On workarounds (tmux/sendkeys/IO interception)

These attack the *classification* (entry-point detection) and leave the *wall* (rate
limits) intact. A puppeted-TUI session has the same throughput ceiling as a
hand-launched skill — the only thing it "unlocks" is unattended start, i.e. cron.
That capability comes bundled with: a plain terms violation (misrepresenting
automated traffic as interactive → account termination risk), detectability via usage
telemetry (machine-perfect timing, zero human variance, 24/7 cadence), and an
arms-race whose prize is *capped at plan limits*. Bad expected value; predict such
wrappers exist but never matter. The defensible path gets everything they'd risk
their accounts for, by launching the session yourself and staying inside the limits.

### The determinism catch (the real cost of the move)

**Shell scripts execute; a skill is interpreted.** `ralph-loop.sh` is 2,300 lines of
*deterministic* control flow (circuit breaker, cascade-blocking, dedupe, retry
cooldowns, unlock conditions). Bash runs it perfectly for zero tokens. Translating it
into a `SKILL.md` converts code into prose a model must faithfully follow across a
multi-hour session — models drift, skip steps, lose iteration caps 40 tool-calls
deep, summarize instead of executing. You'd also lose your BATS tests (can't unit-test
prose) and pay tokens for supervision that bash did for free.

**The fix is to not translate the scripts — keep the deterministic machine, change
only who calls it:**

1. **State machine stays in code** (status transitions, dedupe, circuit breaker,
   cascade). The session *calls* it via the Bash tool ("run the unlock check, tell me
   what's ready") rather than re-implementing it in prose.
2. **Fan-out goes through the Workflow tool, not prose loops.** Workflow scripts are
   plain JavaScript — loops, retries, caps, schemas execute deterministically; only
   `agent()` calls cost tokens. Closest in-session analog to bash ralph. The
   retry-with-learnings loop and per-stage schema enforcement (`--json-schema` →
   Workflow's `schema` option) port almost mechanically. Subagents natively support
   worktree isolation and per-agent model selection.
3. **Model judgment stays where it was** (dependency analysis, review verdicts, fix
   decisions were always LLM calls → subagents).
4. **The skill is thin** (~a page): load status via helper, dispatch ready tasks as
   workflows, run unlock helper on completion, repeat. Small enough that drift has
   nowhere to hide.

Operational note: the session must stay alive on the host — run it under tmux *on the
box* (keeping your own session alive, not faking interactivity). VS Code tunnel in to
chime in when a review needs a human.

---

## 5. Architecture principle for the template

Hard line between:

- **Engine** (deterministic): state machine, dependency DAG, retry-with-learnings,
  capacity throttle, cost ledger, status files. Testable, token-cheap, language of
  choice.
- **Adapters:**
  - **Execution adapter — two orthogonal axes, not one list.** The earlier framing
    lumped codex in with interactive/headless; codex is a **provider**, not an
    execution mode. Model the two axes independently:

    ```
    execution_mode ∈ {interactive, headless}  ×  provider ∈ {claude, codex, …}
    billing        = derived property of the (mode, provider) pair
    ```

    A stage picks a cell. Interactive×claude rides the subscription (cheap default,
    attended); headless×claude is the always-works fallback (credit / API rates);
    any×codex bills OpenAI. **Empty cells are honest** — e.g. codex-interactive
    simply isn't offered today, and the table says so rather than hiding it.
    *Keeping the axes orthogonal (rather than a flat mode list) is the key
    generalization* — it makes a future billing line-move **or a new provider** a
    config change, not a rearchitecture.
  - **Project-config adapter:** `INSTALL_CMD`, `TEST_UNIT_CMD`, `TEST_E2E_CMD`,
    `FAILURE_CLASSIFIER`, directory hints, agent roster, optional review plugins
    (API-contract check, E2E coverage) as drop-ins under a `hooks/` dir. The existing
    `adapting-claude-pipeline` skill becomes the bootstrap that fills this in for a
    new repo.

### Fixes to bank in the rewrite (from §2)

- Collapse the over-prescriptive stage split (research/evaluate/plan → one stage).
- Adaptive loop caps via the existing convergence detector instead of fixed
  worst-case caps.
- Single model/pricing config table; fix stale prices ($15/$75 → $5/$25) and model
  pins (`opus-4-7` → current).
- Add session / prompt-cache reuse (chain related stages; exploit the cache tokens
  already being recorded).
- Tighten the Codex success heuristic if Codex becomes a primary route.

---

## 6. The execution plan — five phases

1. **Ground-truth extraction.** Go back to the *actual source files* (not the Explore
   paraphrase) and produce a faithful **as-built spec**: every stage, flag, schema,
   status-file field, circuit-breaker/cascade logic, capacity math, codex success
   heuristic. Goal: avoid reverse-engineering imperfectly and porting bugs forward.
   *Embarrassingly parallel* — fan out one mapping subagent per major script
   (`ralph-loop.sh`, `orchestrator-common.sh`, `implement-orchestrator.sh`, lib
   helpers, schemas), each emitting a spec fragment against a fixed template, then
   synthesize. Building this fan-out is itself a useful test of the in-session
   paradigm the template bets on. First reviewable artifact.
2. **Target spec.** Rewrite implementation-agnostic with the engine/adapter split
   (§5) and the banked fixes. Reviewable before any code.
3. **Build engine + one execution mode (MVP).** Smallest thing that runs one real
   task end-to-end. Not parity.
4. **Add the second execution mode + port adapters.** Interactive/headless duality
   and codex routing land here.
5. **Dogfood.** Run against Hey Soo! as the *reference adapter*, then a second,
   unrelated project to prove generality.

---

## 7. Open decisions (blocking the target spec)

### Decision 1 — Engine language

The retrofit conclusion ("keep bash, change the caller") was right *for a retrofit*.
For a fresh template it reopens, because the primary interactive execution mode is the
Workflow tool, which is **JavaScript**.

- **TypeScript engine** — the deterministic logic runs natively as an in-session
  Workflow *and* as a headless Node CLI calling the SDK. One codebase, two run
  targets, aligned with where Anthropic is putting orchestration primitives. Biggest
  upfront rewrite, best long-term coherence. Eliminates the interactive-mode language
  seam.
- **Keep bash engine** — preserves tested logic + BATS; interactive + headless are
  thin front-ends. Lowest risk. But two languages and a coordination seam in
  interactive mode (bash can't spawn in-session subagents → computes "next step,"
  hands back to the session to dispatch).
- **Python engine** — testable/readable, good for general work, but doesn't align
  with Workflow's JS, so interactive mode keeps the same seam as bash.

### Decision 2 — v1 scope

- **MVP-first** — engine + interactive mode + one project adapter (Hey Soo!), one
  real task end-to-end; headless, codex, and full ralph batch scheduling as
  fast-follows.
- **Full-parity port** — reproduce everything (full ralph scheduling, all three
  execution modes, codex routing, queue mode) before dogfooding. Closer to drop-in,
  much longer to first working run.

*(These two answers determine what phases 2–4 actually look like.)*

---

## 8. Source inventory (Hey Soo! repo, as of writing)

| File | ~Lines | Role |
|---|---|---|
| `.claude/scripts/ralph-loop.sh` | 2,300 | Batch scheduler (DAG, concurrency, retry-with-learnings, queue, capacity throttle, cascade) |
| `.claude/scripts/lib/orchestrator-common.sh` | 4,347 | Shared engine: run_stage, provider routing, model tiering + fallback chain, capacity, status, cost tracking, test/quality loops, learnings |
| `.claude/scripts/implement-orchestrator.sh` | ~800 | Per-task 11-stage pipeline entry point; execution modes (full/lite/micro); per-task provider tags; resume |
| `.claude/scripts/implement-roadmap-task-orchestrator.sh` | 15 | Backward-compat wrapper → implement-orchestrator.sh |
| `.claude/scripts/batch-orchestrator.sh` | 842 | Batch-mode parallel execution with dependency tracking |
| `.claude/scripts/lib/*.sh` (9 helpers) | ~1,540 | Regression detection, test-failure classification, port registry, incremental test selection, worktree management |
| `.claude/scripts/schemas/*.json` | 17 files | Per-stage structured-output schemas (generic; no Hey Soo! coupling) |

**Per-task model selection** (`get_stage_model`): Haiku/shell = setup; Opus =
research/plan/implement/fix-*; Sonnet = evaluate/review/simplify/test/docs/pr/complete.

**Call counts:** full ~50 (worst case w/ retries), lite ~20–25, micro ~10.

**Provider routing (ADR-062):** global `ORCHESTRATOR_PROVIDER=codex` (all stages) +
per-task `:codex` tag (CODEX_ELIGIBLE_STAGES = implement/fix-* only). Codex via
`codex exec --full-auto --json --output-last-message`; success = git-moved (patching)
or required-keys-present (analytical); falls back to Claude on failure.
