# orchestration-template

A reusable, project-agnostic orchestration harness for driving multi-stage,
dependency-aware coding tasks through Claude (and Codex) — runnable as an in-session
Workflow on the subscription, and headless via the Agent SDK / `claude -p` / `codex exec`.

It is a deliberate **Python rebuild** of a bash orchestration system (Hey Soo!'s
`.claude/`), extracted to a spec and rebuilt around a clean engine/adapter split.

New here? **`ARCHITECTURE.md`** is a one-page map of the whole system — the engine/adapter
split, the six-stage pipeline, the front doors, the control loops, and where to start reading.

**Status: built and live-proven.** Phases 1–5 complete plus two engine-hardening passes
and a workflow code-review pass. 294 pytest cases green, ruff clean. Driven real GitHub
issues to merged/draft PRs on the reference project (heysoo PRs #556–#560) with clean
lane-attribution audits. Remaining/known-thinned scope is tracked as GitHub issues
(label `deferred-scope`); see `DEFERRED.md` for the discipline.

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
  project/{base,heysoo,selfhost}/   the contract + reference adapters (a NEW project's
                         adapter lives in the project's OWN repo — see below)
run_targets/             thin run targets: the Workflow shim (JS) + supervisor skills
tests/                   pytest suite (294)
docs/                    design doc, plan, and the as-built/target spec (see below)
DEFERRED.md              scope-ledger discipline (the ledger itself = GitHub issues)
```

## The front door: idea → issues

A run starts from an already-written issue. The **spec front door** is the missing
upstream: it turns *an idea* into small, dependency-ordered, independently-shippable
issues that feed the batch lane. The model authors a spec file during a conversation
(the `spec-intake` skill guides it); deterministic code validates and files it. The
spec shape is a JSON doc validated by `orchestrator/schemas/spec.json` — a `title`,
`summary`, and `tasks[]` where each task has a local `id`, `title`, full-issue `body`,
`depends_on` (local ids), and optional `labels`/`provider_tag`/`pipeline`/`estimate`.

```bash
uv run orchestrator spec validate spec.json     # schema + DAG (cycles, unknown refs, dup ids)
uv run orchestrator spec plan spec.json          # print the topological filing plan — no writes
uv run orchestrator --project $PROJECT spec file spec.json --dry-run   # preview exactly what would be created
uv run orchestrator --project $PROJECT spec file spec.json            # file each task in dependency order
```

`spec file` files each task via the project's task source in topological order,
translating local `depends_on` ids into the real issue refs of already-filed tasks
(a `Depends-on: #N` line in each body), applying the task's labels plus a `spec:<slug>`
batch label so the whole set is queryable (`gh issue list --label spec:<slug>`). The
filed issues then feed the batch lane below (`add-task --task "#N"` for each).

## Install

The engine runs straight from a checkout (`uv run orchestrator …`, the zero-packaging
path), or install it as a library so any repo can drive its own project:

```bash
# console scripts: `orchestrator` + `orchestrator-scaffold`
uv tool install "git+https://github.com/cperler/orchestration-template"
# or into a project's own venv
uv pip install "git+https://github.com/cperler/orchestration-template"
```

(PyPI publication isn't in scope — install from git.) The starter kit, stage schemas,
and reference adapters all ride inside the wheel, so `orchestrator-scaffold` and codex
full-validation work from an installed copy exactly as from a checkout.

A project plugs its adapter in one of three ways (in resolution order for `--project`):
a **directory path** (`../my-project/.orchestration` — the zero-packaging option, the
adapter lives in the project repo), a **dotted module** (`adapters.project.heysoo`), or
an **entry-point name** once a package registers one. To ship an adapter as an installable
package, register it under the `orchestrator.project_adapters` group — then
`--project <name>` resolves it:

```toml
# in the adapter package's pyproject.toml
[project.entry-points."orchestrator.project_adapters"]
myproj = "myproj_orchestration"                    # module: CONTRACT_VERSION + get_config
# or: myproj = "myproj_orchestration:MyProjConfig" # a ProjectConfig class (class-level CONTRACT_VERSION)
```

The engine's own `heysoo` / `selfhost` reference adapters are registered exactly this way
(`--project heysoo`), which self-tests the mechanism.

## Running it

Two things vary independently: **how you launch** (from a plain terminal, or from inside
a Claude session) and **which lane the work runs on** (`execution_mode × provider`). The
engine is the same CLI in every case (`orchestrator/cli.py`); what differs is who dispatches
the model calls. Set these once so the examples stay short:

```bash
ROOT=runs/issue-42; RUN=issue-42; PROJECT=adapters.project.heysoo
ORCH="uv run orchestrator --root $ROOT --run $RUN --project $PROJECT"
```

`--lane full|lite|micro` picks the pipeline depth (how many stages); `--util N` is the
current 5h utilization %, which the engine turns into the capacity-bounded dispatch limit.

### 1. From a terminal — headless, in-process (self-contained)

`run-headless` drives the **whole** run in one process, shelling out to `claude -p` (and
`codex exec`) per stage. No Claude session required — just the `claude` CLI on your PATH.
This is the mode for cron / CI / a background terminal.

```bash
$ORCH init-run --lane full
$ORCH add-task --task "#42"
$ORCH run-headless --mode headless --util 0
$ORCH status
```

### 2. From within a Claude session — interactive (subscription)

Invoke a supervisor **slash command** and Claude drives the run in-session. This runs the
same engine CLI but dispatches each stage via the in-session Workflow shim (`agent()`
calls), so the work bills to your Claude subscription instead of the API. This lane is
*only* launchable from inside a session — the interactive dispatch needs the in-session
shim, which a plain terminal doesn't have.

- **single task** → `/orchestrate-task-interactive`
- **batch** → `/orchestrate-batch-interactive`

These are real, registered skills — they live at `.claude/skills/<name>/SKILL.md` (the
layout Claude Code discovers), sourced from `run_targets/supervisor_skill.md` /
`scheduler_skill.md`. The skill runs the loop: `init-run` → `add-task` → `next` → dispatch
via `run_targets/workflow_shim.js` → `record`, repeating until the task is terminal. It
never calls a model directly or drives `next`/`record` by hand out of order — the engine
sequences the state; the supervisor just follows it. (The same engine commands exist
standalone — `$ORCH next --task "#42" --util 0`, then `$ORCH record --result result.json`
— if you want to step a run manually from a terminal.)

> **Bootstrapped projects get these slash commands too.** `orchestrator-scaffold` seeds the
> supervisor skills into the new project's `.claude/skills/<name>/SKILL.md` (keyed by each
> skill's frontmatter `name:`), so `/orchestrate-task-interactive` and
> `/orchestrate-batch-interactive` are invocable in that repo out of the box.

### 3. Headless via `claude -p` / `codex exec` (what mode 1 uses underneath)

Mode 1's transport is literally `claude -p <prompt> --model <model> --output-format json`
(see `adapters/execution/transport.py`), with a `codex exec` sibling for the codex
provider. You don't invoke these by hand — `run-headless` builds and runs one per stage —
but that's the actual model call, and it's why mode 1 needs no interactive session. Pick
the provider on the same command:

```bash
$ORCH run-headless --mode headless --provider claude   # every stage via claude -p
$ORCH run-headless --mode headless --provider codex     # file-patching stages via codex exec
```

### 4. Pointing at different models

The engine assigns a model **per stage by role** — it's never a CLI flag. Two knobs:

- **Provider axis** — `--provider claude|codex` (whole run) or a per-task `:codex` tag
  (routes only the file-patching stages, `IMPLEMENT`/`TEST`, to codex). Codex is always
  headless; `codex×interactive` is an intentional empty cell.
- **Which Claude model per role** — `orchestrator/model_table.py` is the single source of
  truth: roles resolve to ids (`DEEP_REASON → claude-opus-4-8`, `REVIEW →
  claude-sonnet-4-6`, `CHEAP_SHELL → claude-haiku-4-5`). A model bump is a one-line edit
  there and every lane (the Workflow shim *and* `claude -p`) picks it up automatically. The
  rate-limit fallback chain (opus → sonnet → haiku) lives in the same table.

### Observability (any mode)

```bash
$ORCH status          # progress + cost summary + lane-attribution audit
$ORCH cost-report     # per-stage / per-task breakdown + the session-reuse win
$ORCH retrospective   # failure patterns + what the retries learned (on a failed run)
```

Standing up a **new project** is an interview, not boilerplate: `orchestrator-scaffold
--detect <repo>` reads the repo's stack and prints a draft `profile.toml`;
`run_targets/adapter_bootstrap_skill.md` walks the detect → confirm → generate → verify
flow (and is re-callable to tune from run artifacts). The scaffold turns the profile into a
project-config adapter **and** seeds a stack-appropriate starter kit (agents, skills, hooks,
schemas from `templates/project-default/`) into the project's `.claude/`. The engine is
never touched.

The adapter is **owned by the project's repo**, not this one (the two-folder layout):
`orchestrator-scaffold --into <repo>` writes it to `<repo>/.orchestration/` (profile.toml
+ generated config.py + hand-tunable classifier.py / task_source.py), and the engine loads
it by path — `--project <repo>/.orchestration`. The adapter runs inside the engine's
process, so the project repo needs no Python packaging. Because an external adapter isn't
updated in lockstep with the engine, its generated `__init__.py` declares the
`CONTRACT_VERSION` it targets (`adapters/project/base.py`); the loader refuses a mismatch
loudly, and `orchestrator --project <dir> validate` duck-checks the full ProjectConfig
surface without needing a run. The in-repo `adapters/project/{heysoo,selfhost}` remain the
reference implementations, kept in lockstep by this repo's test suite.

## Developing

```bash
uv run pytest        # 294 cases
uv run ruff check .
```

The engine stays project-agnostic: new projects plug in via a project-owned
`.orchestration/` adapter (or `adapters/project/` for in-repo reference adapters), never
by editing the engine. Anything cut from scope is filed as a `deferred-scope` GitHub
issue (and re-dispositioned at each gate) — nothing is silently dropped.

## Docs

- `docs/orchestration-template.md` — original design notes + the billing-change analysis (2026-06, historical).
- `docs/orchestration-template-plan.md` — the phased implementation plan (historical record).
- `docs/orchestration-spec/as-built.md` (+ `sections/`, `fragments/`) — the verified extraction of the **reference** bash system (read-only ground truth; describes heysoo's `.claude/`, not this code).
- `docs/orchestration-spec/target.md` — the implementation-agnostic target design the rebuild was built from. Deviations are tracked in the issue tracker + git history.
- `docs/orchestration-spec/retrospective.md` — the Phase 5 dogfood retrospective.
- `DEFERRED.md` — the scope-ledger discipline; the live ledger is [GitHub issues](https://github.com/cperler/orchestration-template/issues) (label `deferred-scope`), and the pre-migration ledger is frozen at `docs/deferred-history.md`.
