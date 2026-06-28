# orchestration-template

A reusable, project-agnostic orchestration harness for driving multi-stage,
dependency-aware coding tasks through Claude (and Codex) — runnable as an in-session
Workflow on the subscription, and headless via the Agent SDK / `claude -p` / `codex exec`.

It is a deliberate **Python rebuild** of a bash orchestration system (Hey Soo!'s
`.claude/`), extracted to a spec and rebuilt around a clean engine/adapter split.

**Status: built and live-proven.** Phases 1–5 complete plus two engine-hardening passes
and a workflow code-review pass. 171 pytest cases green, ruff clean. Driven real GitHub
issues to merged/draft PRs on the reference project (heysoo PRs #556–#560) with clean
lane-attribution audits. Remaining/known-thinned scope is tracked in `DEFERRED.md`.

## What it does

Takes a task (e.g. a GitHub issue), runs it through a collapsed **6-stage pipeline** in
an isolated git worktree, and opens a PR — with retry-with-learnings, a circuit breaker,
dependency-aware batching, capacity throttling, full cost attribution, and durable
observability. A batch of tasks runs over a DAG with transitive cascade-blocking and
clean resume-after-kill.

The **6 stages** (`STAGE_ORDER`): `intake` → `scope` → `implement` → `test` → `deliver`
→ `review`. (Collapsed from the reference system's ~12–15 stages; the mapping lives in
`docs/orchestration-spec/target.md` §6.)

## The load-bearing idea: engine / adapter split

- **The engine never calls a model.** It emits a `WorkItem` and ingests a `StageResult`
  (the contract seam in `orchestrator/schemas/work.py`). That makes execution modes
  interchangeable, runs resumable, and every model call structurally attributable.
- **Two orthogonal axes:** `execution_mode ∈ {interactive, headless} × provider ∈
  {claude, codex}`. Billing is a derived property of the (mode, provider) pair, not a
  hardcoded branch. `codex` is always headless; `codex×interactive` is an explicit empty
  cell.
- **Two adapter families:** the **execution adapter** (`adapters/execution/` — how/where a
  call runs: the interactive Workflow shim, headless `claude -p`, `codex exec`) and the
  **project-config adapter** (`adapters/project/` — what a repo plugs in: commands, test
  taxonomy, agent roster, task source). The engine imports neither.

## Layout

```
orchestrator/            the deterministic engine (never calls a model) + CLI
  engine.py              ties the modules together; next_work / record
  scheduler.py           batch DAG scheduler (dispatch / tick / run)
  state_machine.py       per-task stage transitions; resume
  dag.py                 dependency graph: ready / blocked / transitive cascade
  retry.py               retry-with-learnings + structured circuit breaker
  cost_ledger.py         every-call ledger + per-stage/-task + session-reuse analysis
  model_table.py         single model/pricing table + fallback chain
  capacity.py            capacity-derived dispatch limit + backpressure
  routing.py             lane selection (execution_mode × provider)
  failure_classifier.py  pluggable failure taxonomy (regressions vs baseline)
  retrospective.py       failure retrospective (patterns + what retries learned)
  render.py              cost-summary / cost-report / retrospective / per-stage markdown
  scaffold.py            `orchestrator-scaffold` — generate a new project adapter
  stages.py / cli.py     stage prompts + the supervisor's CLI surface
  schemas/               versioned status + work-item/result schemas
adapters/
  execution/             interactive shim, headless_claude, codex, registry, transport
  project/{base,heysoo,selfhost}/   the reference adapter + a self-host adapter
run_targets/             thin run targets: the Workflow shim (JS) + supervisor skills
tests/                   pytest suite (171)
docs/                    design doc, plan, and the as-built/target spec (see below)
DEFERRED.md              the deferred-scope ledger (reviewed at every gate)
```

## Running it

The engine is a CLI the supervisor drives. Common commands (see `orchestrator/cli.py`):

```bash
# interactive×claude lane (supervisor loop): init → add task → next/record per stage
uv run orchestrator --root runs/issue-42 --run issue-42 --project adapters.project.heysoo init-run --lane full
uv run orchestrator --root runs/issue-42 --run issue-42 --project adapters.project.heysoo add-task --task "#42"
uv run orchestrator --root runs/issue-42 --run issue-42 --project adapters.project.heysoo next --task "#42" --util 0
uv run orchestrator --root runs/issue-42 --run issue-42 --project adapters.project.heysoo record --result result.json

# headless lane (engine drives the runners in-process)
uv run orchestrator --root runs/r --run r --project <pkg> run-headless --mode headless

# observability
uv run orchestrator … status          # progress + cost summary + lane audit
uv run orchestrator … cost-report      # per-stage/-task breakdown + session-reuse win
uv run orchestrator … retrospective    # failure patterns + retry learnings (on a failed run)
```

In practice the **interactive** lane is driven by a supervisor following
`run_targets/supervisor_skill.md` (single task) or `scheduler_skill.md` (a batch), which
dispatch the actual work via the Workflow shim.

Standing up a **new project** is an interview, not boilerplate: `orchestrator-scaffold
--detect <repo>` reads the repo's stack and prints a draft `profile.toml`;
`run_targets/adapter_bootstrap_skill.md` walks the detect → confirm → generate → verify
flow (and is re-callable to tune from run artifacts). The scaffold turns the profile into a
project-config adapter **and** seeds a stack-appropriate starter kit (agents, skills, hooks,
schemas from `templates/project-default/`) into the project's `.claude/`. The engine is
never touched.

## Developing

```bash
uv run pytest        # 171 cases
uv run ruff check .
```

The engine stays project-agnostic: new projects plug in via `adapters/project/`, never by
editing the engine. Anything cut from MVP scope is logged in `DEFERRED.md` (and
re-dispositioned at each gate) — nothing is silently dropped.

## Docs

- `docs/orchestration-template.md` — original design notes + the billing-change analysis (2026-06, historical).
- `docs/orchestration-template-plan.md` — the phased implementation plan (historical record).
- `docs/orchestration-spec/as-built.md` (+ `sections/`, `fragments/`) — the verified extraction of the **reference** bash system (read-only ground truth; describes heysoo's `.claude/`, not this code).
- `docs/orchestration-spec/target.md` — the implementation-agnostic target design the rebuild was built from. Deviations are tracked in `DEFERRED.md` + git history.
- `docs/orchestration-spec/retrospective.md` — the Phase 5 dogfood retrospective.
- `DEFERRED.md` — the live deferred-scope ledger (the source of truth for what's intentionally not built yet).
