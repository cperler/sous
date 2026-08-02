# sous

**What this is, in one breath:** you point it at a pile of GitHub issues, and it drives
each one through a staged pipeline — scope → implement → test → deliver → review — in an
isolated git worktree, with Claude (or Codex) doing the model work and a deterministic
Python engine doing everything else: sequencing, retries-with-learnings, dependency
ordering, cost attribution, and resume-after-crash. Out the other end come pull requests.
The engine itself never calls a model; every model interaction goes through a pluggable
adapter, so the same harness can run in-session on a Claude subscription or fully
headless via `claude -p` from cron.

**How it came to be** is a story worth reading before you adopt any of it:
["1,247 Recipes and Nothing to Eat"](https://craigperler.com/2026/07/26/1247-recipes-and-nothing-to-eat/)
is the postmortem of a dinner-planning app that quietly became a machine for building a
dinner-planning app. This repo is a translated export of that machine: the bash
orchestration harness built alongside the recipe site, extracted to a spec and rebuilt
in Python as a standalone, project-agnostic system. The harness is real, tested, and
live-proven (real issues driven to merged PRs, including on its own tracker). The
essay's warning stands: tools like this make it very cheap to generate more work, and
very easy to stop noticing that the work is about the tool. Use accordingly.

---

A reusable, project-agnostic orchestration harness for driving multi-stage,
dependency-aware coding tasks through Claude (and Codex) — runnable as an in-session
Workflow on the subscription, and headless by shelling out to `claude -p` / `codex exec`
(both behind an injectable transport seam, so another headless client can be dropped in).

It is a deliberate **Python rebuild** of an earlier bash orchestration system, extracted
to a spec and rebuilt around a clean engine/adapter split.

New here? Two entry points, depending on what you're doing:

- **`USING.md`** — the operator's guide: standing up your own project and running the loop,
  phase by phase (repo skeleton → adapter → issues → run → merge → triage).
- **`ARCHITECTURE.md`** — the contributor's map: the engine/adapter split, the six-stage
  pipeline, the front doors, the control loops, and where to start reading.

`CHEATSHEET.md` collects the skills, the per-phase commands, and the gotchas on one page.

**Status: built and live-proven.** Phases 1–5 complete plus engine-hardening passes, the
2026-07-01 review→execute cycle (context plane, per-task pipelines, session continuity,
checkpoints, approval gate), and the 2026-07-04 burn-down (deterministic test/deliver,
front doors, budgets/routing, salvage/warm-retry, ports, packaging, dashboard). The full
CI gate (pytest + ruff + mypy) is green. Live-proven end to end: real GitHub issues driven
to merged PRs on a real product repo, with clean lane-attribution audits, and self-hosted on
this repo's own tracker. Anything cut or thinned is tracked as a GitHub issue carrying a
`Source:` line; `CLAUDE.md` documents the discipline.

## What it does

Takes a task (e.g. a GitHub issue), runs it through a collapsed **6-stage pipeline** in
an isolated git worktree, and opens a PR — with retry-with-learnings, a circuit breaker,
dependency-aware batching, capacity throttling, full cost attribution, and durable
observability. A batch of tasks runs over a DAG with transitive cascade-blocking and
clean resume-after-kill.

The **6 stages** (`STAGE_ORDER`): `intake` → `scope` → `implement` → `test` → `deliver`
→ `review`, collapsed from the reference system's ~12–15. `simplify` is an additional
stage-vocabulary member SCOPE can opt a decomposed child into, not a seventh standing step.

## The load-bearing idea: engine / adapter split

- **The engine never calls a model.** It emits a `WorkItem` and ingests a `StageResult`
  (the contract seam in `orchestrator/schemas/work.py`). That makes execution modes
  interchangeable, runs resumable, and every model call structurally attributable.
- **Two orthogonal axes:** `execution_mode ∈ {interactive, headless, engine} × provider ∈
  {claude, codex, none}`. Billing is a derived property of the (mode, provider) pair, not a
  hardcoded branch. `codex` is always headless; `codex×interactive` is an explicit empty
  cell; `engine×none` is the deterministic no-model lane (intake setup, test runs, PR-open).
- **Two adapter families:** the **execution adapter** (`adapters/execution/` — how/where a
  call runs: the interactive Workflow shim, headless `claude -p`, `codex exec`) and the
  **project-config adapter** (`adapters/project/` — what a repo plugs in: commands, test
  taxonomy, agent roster, task source).
- **The dependency arrow points inward.** The engine owns both contracts as *ports*
  (`orchestrator/ports/execution.py`, `orchestrator/ports/project.py`) and adapters implement
  them; no module under `orchestrator/` imports `adapters` at all. The composition root
  reaches a concrete adapter by NAME — an `orchestrator.execution_lanes` entry point for the
  lane bundle (`orchestrator/lane_loader.py`), and path / dotted module / entry point for the
  project adapter (`orchestrator/project_loader.py`). Enforced by
  `tests/test_dependency_direction.py` and a ruff `TID251` ban, not by convention.

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
  project_init.py        `init-project` — phase 0: write + verify a NEW repo's skeleton
  scaffold.py            `orchestrator-scaffold` — generate a new project adapter
  stages.py / cli.py     stage prompts + the supervisor's CLI surface
  schemas/               versioned status + work-item/result schemas
  ports/                 the adapter CONTRACTS the engine owns: execution.py (Runner /
                         Registry), project.py (ProjectConfig / TaskSource / TaskSpec)
  lane_loader.py         resolves the execution-lane bundle BY NAME (entry point), so the
  project_loader.py      engine imports no adapter; same for the project adapter
adapters/                implementations of the ports above (nothing imports back inward)
  execution/             interactive shim, headless_claude, codex, runners (the bundle),
                         transport; base.py is a back-compat re-export of the port
  project/selfhost/      the reference adapter — this repo self-hosting (a NEW project's
                         adapter lives in the project's OWN repo, see below); plus the
                         shared github_issues.py task source and email_sink.py;
                         base.py likewise a shim
run_targets/             the Workflow shim (JS) + the adapter-bootstrap skill
tests/                   pytest suite (one test_<subsystem>.py per module)
docs/                    the frozen build record — design notes, plan, review passes
```

## The front doors: idea → issues → a run

A run consumes a DAG of tasks. Three front doors produce one, each pairing a deterministic
module (validate / rank / file — no model call) with a `.claude/skills/` skill that runs the
conversation:

- **`/brainstorm`** (`orchestrator/brainstorm.py`) — a fuzzy area → scored candidate ideas →
  a ranked shortlist you pick from. Small picks file as standalone issues; large ones feed
  spec-intake.
- **`/spec-intake`** (`orchestrator/spec_intake.py`) — a known idea → a validated,
  dependency-ordered spec → one filed issue per task, in topological order. Detailed below.
- **`/batch-plan`** (`orchestrator/batch_plan.py`) — a pile of *already-filed*, independently
  authored issues → a validated dependency-ordered plan applied straight to a run. Reuses
  spec-intake's DAG validation.

### spec-intake in detail

It turns *an idea* into small, dependency-ordered, independently-shippable
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
uv tool install "git+https://github.com/cperler/sous"
# or into a project's own venv
uv pip install "git+https://github.com/cperler/sous"
```

(PyPI publication isn't in scope — install from git.) The starter kit, stage schemas,
and reference adapters all ride inside the wheel, so `orchestrator-scaffold` and codex
full-validation work from an installed copy exactly as from a checkout.

A project plugs its adapter in one of three ways (in resolution order for `--project`):
a **directory path** (`../my-project/.orchestration` — the zero-packaging option, the
adapter lives in the project repo), a **dotted module** (`adapters.project.selfhost`), or
an **entry-point name** once a package registers one. To ship an adapter as an installable
package, register it under the `orchestrator.project_adapters` group — then
`--project <name>` resolves it:

```toml
# in the adapter package's pyproject.toml
[project.entry-points."orchestrator.project_adapters"]
myproj = "myproj_orchestration"                    # module: CONTRACT_VERSION + get_config
# or: myproj = "myproj_orchestration:MyProjConfig" # a ProjectConfig class (class-level CONTRACT_VERSION)
```

The engine's own `selfhost` reference adapter is registered exactly this way
(`--project selfhost`), which self-tests the mechanism.

## Running it

Two things vary independently: **how you launch** (from a plain terminal, or from inside
a Claude session) and **which lane the work runs on** (`execution_mode × provider`). The
engine is the same CLI in every case (`orchestrator/cli.py`); what differs is who dispatches
the model calls. Set these once so the examples stay short:

```bash
ROOT=runs/issue-42; RUN=issue-42; PROJECT=adapters.project.selfhost
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

#### Unattended: a queue file + `run-queue` (cron)

For a fully hands-off launch, feed batches through a **queue file** instead of adding tasks
by hand. The file is a `ralph-queue.json`-style JSON array of batch entries
(`{tasks, branch, enqueued_at}` — the as-built scheduler §1.8 format); `enqueue` appends one
entry (an atomic whole-array rewrite held under a cross-process advisory lock, so two
parallel producers can't lose an entry — and no engine is needed, so a cron job can top it
up), and `run-queue` is the unattended entrypoint that drains it batch-by-batch, driving
each derived run in-process to terminal.

```bash
# a producer (cron, CI, or a human) appends batches — no engine/store touched:
$ORCH enqueue --queue-file runs/queue.json --tasks "#42,#43" --branch batch-a

# the daemon drains the queue, deriving one run per batch (run id from enqueued_at):
$ORCH --root runs --project adapters.project.selfhost \
      run-queue --queue-file runs/queue.json --owner day-cron \
      --wait --idle-timeout 300
```

Each batch's run id is derived deterministically from its `enqueued_at`, so a driver that
crashes and is relaunched **reuses** the same run (create-or-reuse, idempotent adds) rather
than forking a duplicate. `--wait` idle-waits on an empty queue (polling every
`--poll-interval` seconds up to `--idle-timeout`, then exits) and sleeps through capacity /
rate-limit stalls; without it `run-queue` makes a single drain pass and returns. The consumer
claims the head entry **in place** (stamping `{run_id, owner, claimed_at, host, pid}`) and
pops it only once its derived run reached terminal, so no kill window can lose a batch; on
an ingest failure the claim is stripped instead and the same head is retried (never silently
dropped), with the failure surfaced. Everything below the ingest step is the same
already-resumable `Scheduler.run` loop mode 1 uses.

Give every concurrently configured consumer a distinct, stable `--owner`. `run-queue`
holds a non-blocking, process-lifetime lock named `consumer-<owner>.lock` in the queue
directory for the entire drain. That lock prevents overlapping invocations on the same
host with the same owner from adopting and double-driving one claim, and the kernel drops
it automatically after a crash so a relaunch can resume. `flock` does not provide reliable
cross-host exclusion on every shared filesystem, so use distinct owners for consumers on
different hosts too; the summary reports `consumer_guard: unavailable` on platforms where
the guard cannot be enforced.

If an owner is permanently retired or renamed while it holds the head, release the stale
claim explicitly (the batch remains queued and can then be claimed by another consumer):

```bash
$ORCH run-queue --queue-file runs/queue.json --release-claim
```

Release refuses when the claim records this host and that owner's consumer lock is still
held. Stop the live consumer first; `--force` is available for deliberate administrative
override. Claims from another host cannot be proven live by this host-local check, so
coordinate cross-host releases operationally.

### 2. From within a Claude session — interactive (subscription)

Invoke a supervisor **slash command** and Claude drives the run in-session. This runs the
same engine CLI but dispatches each stage via the in-session Workflow shim (`agent()`
calls), so the work bills to your Claude subscription instead of the API. This lane is
*only* launchable from inside a session — the interactive dispatch needs the in-session
shim, which a plain terminal doesn't have.

- **single task** → `/orchestrate-task-interactive`
- **batch** → `/orchestrate-batch-interactive`

These are real, registered skills — they live at `.claude/skills/<name>/SKILL.md` (the
layout Claude Code discovers), and `templates/project-default/skills/` holds byte-identical
copies that `orchestrator-scaffold` seeds into a new project (kept identical by
`tests/test_kit_skills_in_sync.py`). The skill runs the loop: `init-run` → `add-task` → `next` → dispatch
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
provider. These two subprocess transports are what ships; the runners take a `Transport`
callable, so a different headless client is an injection, not an engine change (tests use
that seam). You don't invoke these by hand — `run-headless` builds and runs one per stage —
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
$ORCH --root runs panel-report --limit 20  # cross-run panel yield + review cost
$ORCH retrospective   # failure patterns + what the retries learned (on a failed run)
$ORCH util            # 5h/7d account utilization (JSON) — the --util sensor
$ORCH statusline      # the same numbers as one line, for the status bar
$ORCH supervisor-context # fresh Claude Code context-window snapshot (interactive sensor)
$ORCH resume-supervisor  # release a lease-free context park from a fresh session
```

REVIEW can also identify a process lesson's concrete harness target (a stage template,
agent, skill, stage schema, or scaffold-kit asset). Those lessons are retained across runs
but excluded from product-task prompts. When the same target is criticized in two distinct
runs, the engine files one `meta-authoring` enhancement containing both evidence trails.
That issue runs through the ordinary pipeline, but is automatically held before DELIVER
until a human approves it; merging the resulting PR is the apply step.

`panel-report` reads the newest run stores directly, comparing panel and plain review
costs while breaking panel yield down by finder lens, verifier verdict, and cap hits. It is
observational: the panel replaces the single-reviewer path, so the report explicitly does
not claim what a single reviewer would have caught on the same diff. It also flags low panel
sample sizes and incomplete/unmetered cost data instead of presenting either as certainty.

**At-a-glance utilization while supervising runs (#61).** `orchestrator statusline`
prints one line — `⧗ 5h 87% (resets 3h0m) · 7d 41% (resets 4d9h)` — off the same
2-min usage cache the `util` sensor feeds (no extra network cost). Wire it into Claude
Code's status bar by adding a `statusLine` field to your user settings
(`~/.claude/settings.json`) or project settings — set `type` to `"command"` and point
`command` at the CLI (`padding` is optional and defaults to `0`):

```json
{ "statusLine": { "type": "command", "command": "orchestrator statusline" } }
```

Verified against Claude Code's [status line docs](https://code.claude.com/docs/en/statusline)
(2026-07-04); check that page for the current schema before copying.

It stays quiet (empty line, exit 0) whenever the probe is unavailable, so the bar
never shows an error.

The same status-line invocation captures Claude Code's `context_window` counters in a
small cwd-keyed temp cache. `orchestrator supervisor-context` reports the fresh snapshot;
interactive skills pass `--guard-supervisor-context` to `next`, which reserves 20% of the
window plus a conservative estimate of the exact next rendered prompt. If that headroom is
not available, the engine emits no WorkItem and takes no lease: it marks the run `parked`,
records `supervisor_parked` with a fresh-session resume command, and excludes the parked
tasks from stale alarms. Run `resume-supervisor` once from the fresh session.

A project that **doesn't exist yet** starts one step earlier: `orchestrator init-project
<name> --into <parent> --create-repo` writes the phase-0 skeleton (src layout, one passing
test, ruff + mypy configured), commits it, runs the skeleton's own verification commands,
and creates the GitHub repo only once they pass — because those exact commands become the
adapter's contract below. The `/new-project` skill runs that, the adapter bootstrap, and the
front door as one guided session — ending not at a bare repo but at filed, dependency-ordered
issues ready for a run.

Standing up a **new project** is an interview, not boilerplate: `orchestrator-scaffold
--detect <repo>` reads the repo's stack and prints a draft `profile.toml`;
`run_targets/adapter_bootstrap_skill.md` walks the detect → confirm → generate → verify
flow (and is re-callable to tune from run artifacts). The scaffold turns the profile into a
project-config adapter **and** seeds a stack-appropriate starter kit (agents, skills, hooks,
schemas from `templates/project-default/`) into the project's `.claude/`. The engine is
never touched. See **`USING.md`** phase 1 for the full walkthrough, including the two files
the profile can't infer (`task_source.py`, `classifier.py`).

The adapter is **owned by the project's repo**, not this one (the two-folder layout):
`orchestrator-scaffold --into <repo>` writes it to `<repo>/.orchestration/` (profile.toml
+ generated config.py + hand-tunable classifier.py / task_source.py), and the engine loads
it by path — `--project <repo>/.orchestration`. The adapter runs inside the engine's
process, so the project repo needs no Python packaging. Because an external adapter isn't
updated in lockstep with the engine, its generated `__init__.py` declares the
`CONTRACT_VERSION` it targets (`orchestrator/ports/project.py`); the loader refuses a mismatch
loudly, and `orchestrator --project <dir> validate` duck-checks the full ProjectConfig
surface without needing a run. The in-repo `adapters/project/selfhost` remains the
reference implementation, kept in lockstep by this repo's test suite.

## Developing

```bash
uv run pytest        # the suite
uv run ruff check .  # lint
uv run mypy          # type-check
```

All three are the required-green gate CI enforces (`.github/workflows/ci.yml`); a change
keeps every one of them green. `tests/test_docs_consistency.py` pins that this list stays
in sync with CI (and that these docs don't re-acquire a hardcoded test total).

The engine stays project-agnostic: new projects plug in via a project-owned
`.orchestration/` adapter (or `adapters/project/` for in-repo reference adapters), never
by editing the engine. Anything cut from scope is filed as an ordinary GitHub issue naming
the task that cut it — nothing is silently dropped.

## Docs

- **`USING.md`** — the operator's guide: the six phases of standing up a project and running
  the loop on it. Start here if you're *using* the harness rather than changing it.
- **`CHEATSHEET.md`** — skills, per-phase commands, lane selection, recovery, and gotchas on
  one page.
- **`ARCHITECTURE.md`** — the contributor's map of the system as built. Start here if you're
  changing the engine.
- **`CLAUDE.md`** — the working norms any change to this repo must respect.
- **Scope ledger** — the live ledger is
  [GitHub issues](https://github.com/cperler/sous/issues); the discipline
  and the label taxonomy live in `CLAUDE.md`, and the pre-migration ledger is frozen at
  `docs/deferred-history.md`.
- **`docs/`** — the frozen build record (original design notes, the phased plan, and the
  design-pass reviews). Historical by construction; `docs/README.md` indexes it and says
  what was deleted and why. Nothing there describes the current code.
