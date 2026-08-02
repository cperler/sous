# Using the harness on your own project

The operator's guide: taking a project from "I have an idea" to "the harness is driving
merged PRs into it." `README.md` explains what the system *is*; `ARCHITECTURE.md` maps how
it's built. This file is the sequence you actually follow.

Six phases. You do phase 0 once per project, phase 1 once (and re-run occasionally to
tune), and phases 2–5 as a repeating loop.

| Phase | What it produces | Repeats? |
|---|---|---|
| 0. Repo skeleton | A repo with a remote and three green verify commands | Once |
| 1. Adapter bootstrap | `<repo>/.orchestration/` | Once, re-tuned later |
| 2. Idea → issues | Dependency-linked GitHub issues | Every cycle |
| 3. Issues → a run | A run carrying the DAG | Every cycle |
| 4. Run the batch | PRs + a full audit trail | Every cycle |
| 5. After the batch | Merged trunk, triaged follow-ups | Every cycle |

A running example: a hypothetical `sample-project` tool that ingests data from an external
API and computes signals over it.

---

## Phase 0 — the repo skeleton

The harness drives *increments* into a repo. Every run ends with: make a change, run the
project's tests, run its linter, open a PR against an issue. That sentence presumes three
things a run cannot produce, because a run needs them to start:

1. **A git repo with a GitHub remote.** Issues are the task ledger.
2. **A detectable stack.** Phase 1 sniffs the repo to draft its config. An empty folder
   tells it nothing.
3. **Verification commands that actually run.** The `test` and `review` stages shell out to
   real commands and read exit codes. An unconfigured type-checker is a gate that silently
   passes forever.

This phase is *not* building the product — no business logic. It is a skeleton, and
`orchestrator init-project` writes it:

```bash
uv run orchestrator init-project sample-project \
    --into ~/Development \
    --description "Ingest data from an external API and compute signals over it." \
    --create-repo --visibility private
```

Add `--dry-run` to see the resolved plan without writing anything. Python is the only
stack today.

What lands:

```
sample-project/
├── .gitignore             includes runs/ — the audit trail is local-only
├── README.md              a stub that tells you to replace it with two honest paragraphs
├── pyproject.toml         deps + ruff and mypy configured
├── src/sample_project/
│   ├── __init__.py
│   └── version.py
└── tests/
    └── test_version.py    one test that passes
```

The one trivial test exists so the test command exits 0 rather than "no tests collected" —
an ambiguous initial state makes the first real run's failure hard to read. The README stub
matters more than it looks: a run's `scope` stage reads the repo for context, so replacing
it with two honest paragraphs measurably improves early runs.

### It verifies itself, in this order

1. Writes the skeleton (refusing a non-empty dir unless `--force`, and never overwriting a
   file it owns).
2. `git init` and an initial commit.
3. **Runs the skeleton's own verification commands** — `uv sync`, `uv run pytest`,
   `uv run ruff check .`, `uv run mypy`.
4. **Only on green**, creates the GitHub repo and pushes.

Step 3 is the point of the whole command. Writing a `pyproject.toml` that *configures* mypy
is not the same as a repo where `uv run mypy` exits 0, and those exact commands become the
adapter's contract in phase 1 — a skeleton that can't pass them hands the harness gates it
can never satisfy. `verified: true` in the report means they really ran.

On red, the report names the failing command and carries its output, and **no GitHub repo is
created**. Fix the repo rather than dropping the command from the profile in phase 1.

**Phase 0 is done when the report says `ok: true` and `verified: true`.** Those three
commands are its real product.

### Doing it by hand

Nothing stops you writing the skeleton yourself — the harness only cares that the three
commands are green and the repo has a remote. The command exists because the failure mode
is quiet: an unconfigured type-checker or a missing `[tool.mypy] files` entry produces a
gate that passes forever without checking anything.

> **The `/new-project` skill** runs phases 0, 1, and 2 as one guided session — skeleton,
> adapter, then the idea carried through `/brainstorm` (if it's still fuzzy) and
> `/spec-intake` into filed, dependency-ordered issues. It leaves you at the start of
> phase 3 with work queued. Use it when you want the conversation; use the CLI directly
> when you don't.

---

## Phase 1 — bootstrap the adapter

The engine knows nothing about your project — not your test command, not what a failure
looks like in your test output, not that your work items live in GitHub issues. All of that
is one small Python package, the **project-config adapter**, and it lives in *your* repo at
`<repo>/.orchestration/`. The engine loads it by path and is never edited.

The interview is **detect → confirm → generate → verify**
(`run_targets/adapter_bootstrap_skill.md` walks it, and is re-callable to tune later).

### Detect

```bash
uv run orchestrator-scaffold --detect ~/Development/sample-project --name sample-project
```

Writes nothing. Prints a draft `profile.toml`: languages (from `pyproject.toml` /
`package.json` / `go.mod` / `Cargo.toml`), commands refined by the detected package manager,
an agent roster, and a task-source guess (`github-issues` if there's a GitHub remote, else
`local-file`). Detect-then-confirm — you correct a filled-in draft rather than answering
cold.

> **Create the GitHub repo before you detect.** The task-source guess reads the remote, so
> detecting a remote-less repo writes `task_source = "local-file"` — the wrong answer for an
> issue-driven project, and easy to skim past in the draft. `init-project --create-repo`
> gets the ordering right for you.

### Confirm

Check the parts detection can't be sure of: did it miss or over-call a stack? Are those
really how this repo installs, tests, and type-checks? GitHub Issues or a local
`tasks.json`? Keep the detected implement agent or swap it? Save the corrected profile.

### Generate

```bash
uv run orchestrator-scaffold --name sample-project \
    --profile /tmp/sample-project-profile.toml \
    --into ~/Development/sample-project
```

Two things land in your repo:

- `<repo>/.orchestration/` — `profile.toml` (the source of truth), `config.py` (a
  *generated view* of the profile — never hand-edit it; edit the profile and re-run), plus
  `classifier.py` and `task_source.py` as write-once starting points.
- `<repo>/.claude/` — the stack-appropriate slice of `templates/project-default/`: agent
  personas, supervisor skills, per-stage output schemas, example format/safety hooks.

The generation is deterministic, idempotent, and **additive** on re-run — it extends a
project without clobbering hand-edits.

### Finish the two files the profile can't infer

**`task_source.py`** — the generated default reads tasks from a local JSON file. If your
profile says `github-issues`, swap in a real GitHub-Issues source; copy the shape from
`adapters/project/selfhost/task_source.py`. This is what turns "issue #7" into a runnable
task and posts completion evidence back.

**`classifier.py`** — when a stage runs your tests and they fail, something must decide
*what kind* of failure that was (unit / e2e / shell), because the engine retries those
differently. It also maps changed-file → impacted tests. The generated default matches
`^FAILED <name>`, roughly right for pytest. Tune it once, against real failure output.

Hooks seeded into `<repo>/.claude/hooks/*.json` are examples — merge them into the
project's `.claude/settings.json`.

### Verify

```bash
uv run orchestrator --project ~/Development/sample-project/.orchestration validate
```

Duck-checks the entire `ProjectConfig` surface and the contract version without needing a
run. A half-finished adapter fails here rather than twenty minutes into a batch.

> **Contract version.** The generated `__init__.py` stamps the `CONTRACT_VERSION` it was
> built against. Your adapter lives in a different repo from the engine, so they drift; when
> the engine's contract changes the loader refuses to start and says so. Fix: re-run the
> scaffold.

**From here on, every `orchestrator` command carries
`--project ~/Development/sample-project/.orchestration`.**

### Where you run all of this from: the cockpit model

**This repo (`orchestration-template`) is the cockpit; project repos are the workpieces.**
Sessions and commands start *here* and reach outward via `--project`:

- The front-door skills — `/new-project`, `/brainstorm`, `/spec-intake`, `/batch-plan`,
  the orchestrate-* runners, `/triage-followups` — live in **this repo's** `.claude/skills/`.
  A session started inside `~/Development/sample-project` does not have them: typing
  `/spec-intake` there hits nothing. Start the session in `orchestration-template`.
- The scaffolded project's own `.claude/` gets only the four **run-lane supervisor skills**
  (plus agents, schemas, hooks) — the pieces a *stage dispatch* needs when the engine runs
  work inside that repo's worktree. It is deliberately not a second cockpit.
- Run state follows the cockpit too: `runs/<run>/` lives under whatever `--root` you pass,
  conventionally this repo's `runs/`, regardless of which project the run drives.

One engine, one place to sit, any number of projects reached by `--project` — that's the
same engine/adapter split the rest of the system is built on, applied to your terminal.

---

## Phase 2 — idea → issues

A run starts from an issue. Something must turn an idea into well-scoped, dependency-ordered
issues. That's the front doors: **you converse and decompose; deterministic code validates,
orders, and files.** Issues are never opened by hand.

### Which front door

- **`/spec-intake`** — a known idea → a validated spec → one filed issue per task. The right
  door for a new project.
- **`/brainstorm`** — a fuzzy area → scored, ranked candidate ideas → your picks (small ones
  filed directly, large ones fed to spec-intake). Its usual evidence base is the codebase,
  the backlog, and run history, so on an established project it's the natural door for
  "what next." On a brand-new one those are empty and the product's own domain is the
  evidence base instead — that works (`evidence` is optional and ranking is on
  impact/effort/risk), it's just a different kind of input. Skip it whenever the idea is
  already shaped enough to decompose.
- **`/batch-plan`** — for issues that *already exist*, filed independently with no encoded
  edges. See phase 3.

### What spec-intake does

**Interrogates briefly** — the two or three questions that most change the decomposition,
not an interview. For the example: which data source first? Batch report or live monitor?
CLI or UI in v1?

**Decomposes** into small, independently-shippable tasks — one PR's worth of work each with
a clear "done" test. Prefer more, smaller tasks. Dependencies must be *real* (t2 consumes
t1's output), never incidental ordering.

```
t1  HTTP client for the external API + config, recorded fixtures
t2  domain data model + response parsing       (needs t1)
t3  append-only local snapshot store           (needs t2)
t4  the core signal computation over a snapshot (needs t2)
t5  CLI: fetch, store, print a summary table   (needs t3, t4)
t6  alert/threshold rule + its config          (needs t4)
```

**Writes a spec JSON** (`orchestrator/schemas/spec.json`). Each task body uses `## Scope`,
`## Acceptance criteria`, `## Out of scope`.

> Those headings are a contract, not decoration. The acceptance gate in phase 5 parses the
> exact `## Acceptance criteria` heading back out, one bullet per criterion. A criterion
> buried in prose is a criterion nothing can check.

**Validates and plans, then stops.**

```bash
uv run orchestrator spec validate spec.json
uv run orchestrator spec plan spec.json
uv run orchestrator --project "$PROJECT" spec file spec.json --dry-run
uv run orchestrator --project "$PROJECT" spec file spec.json
```

`validate` and `plan` are pure — no writes, no `--project`. Filing opens real issues, which
is outward-facing, so it waits for explicit human confirmation.

**Files** in topological order, writing a `Depends-on: #N` line into each dependent's body,
applying each task's labels plus a `spec:<slug>` batch label, and archiving the spec to
`./specs/<slug>.json` with the local-id → issue-ref map.

**Keep that archive** — phase 5's acceptance gate reads it.

---

## Phase 3 — issues → a run

Two things must happen: create a run, and add each issue to it as a task with its edges and
its lane. There are two routes.

### Route A — plain `add-task` (spec-originated batches)

```bash
uv run orchestrator --root runs --shared-root --run "$RUN" --project "$PROJECT" \
    init-run --lane full
uv run orchestrator --root runs --shared-root --run "$RUN" --project "$PROJECT" \
    add-task --task "#7"
```

One `add-task` per issue. **The task source supplies each task's `depends_on`** — spec-intake
already wrote `Depends-on: #N` into the bodies — so the engine builds the correct DAG with no
further analysis.

### Route B — `/batch-plan` (inferred edges, or per-task lane pins)

Use it when the edges must be *inferred* (issues filed independently over weeks, no shared
author) or when you want per-task lane and model pins.

`batch-plan candidates` fetches open issues as JSON with `depends_on` pre-populated from any
existing `Depends-on:` lines. You analyze; `validate` checks schema, cycles, duplicate ids,
self-edges, and model/provider pins; you present the plan and **stop**; `apply` adds every
task in topological order and emits a `batch_planned` event.

Never hand-write `add-task --model` — a plan field keeps `apply`'s topological ordering.

### Choosing lanes

The `pipeline` per task is the main cost dial:

- `micro` — docs-only or pure config. Also mark `--deterministic-stages test,deliver`: a docs
  change needs no model to run tests or write a PR.
- `lite` — small, mechanical, localized (the example's t1).
- `full` — risky, cross-cutting, or ambiguous (the example's t4, where being wrong is
  expensive).

Optional per-task pins: `model` (e.g. `fable` for architecture-heavy design work) and
`effort`.

### Two rules learned the hard way

**Don't add an edge just because two things *could* run in sequence.** An unnecessary edge
serializes work that would otherwise parallelize.

**If two tasks' fixes touch the same region of code, fold them into one task** rather than
DAG-edging them. Two tasks converging on the same lines produce a redundant-merge mess even
when the ordering is technically right. One task, one PR.

### Sanity-check before launching

```bash
uv run orchestrator --root runs --shared-root --run "$RUN" --project "$PROJECT" \
    dispatchable --util auto --max-concurrent 3
```

Confirm the DAG is what you intended and that `limit > 0`. A `limit` of 0 means you're
capacity-stalled; launching will just spin.

### Run-level settings must be chosen at `init-run`

Every subcommand rebuilds the engine from constructor defaults, so a setting not stored on
the Run doc is gone by the next command. These cannot be added later:

`--lane`, `--budget-usd`, `--review-workflow`, `--max-filed-followups`,
`--progress-comments`, `--route-by-cost`, `--route-by-capacity`,
`--cross-provider-fallback`, `--warm-retry`.

For a first batch on a new project, set `--budget-usd` deliberately — it's the circuit
breaker (soft warning at 80%, hard PAUSE at the budget).

---

## Phase 4 — run the batch

**Headless is the default lane** (`/orchestrate-batch-headless`). One driver owns the run and
the engine's scheduler spawns `claude -p` per stage, so no stage prompt or output passes
through your session, provider sessions chain with `--resume` (measured 92–96% cache hits on
long stages), and real per-stage dollars land in `stage-costs.jsonl`.

Use `/orchestrate-batch-interactive` only when a human needs to watch each stage live. That
lane routes everything through session context *and* records `$0.00`/zero tokens — the
Workflow shim cannot report usage.

The driver command is the same in every launch mode:

```bash
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT run-headless --wait
```

`--wait` sleeps through capacity stalls and rate-limit cooldowns instead of returning on the
first one. `--max-concurrent` defaults to 3.

### Three launch modes

**(a) Backgrounded from the session.** Convenient, and you get an exit notification. But
tracked background tasks are reaped on a 30-minute wall-clock-aligned schedule, and since
launch time is arbitrary relative to that boundary, a driver's life is uniformly 0–30
minutes. **Do not use this if the batch could exceed ~25 minutes.**

**(b) Handed to the human, foreground.** The right mode when the batch is long, when you
want to run it yourself, or for a live run against an external product repo. That terminal
owns the run for its whole duration.

**(c) Detached** — fork + `os.setsid()`, escaping the reaper with `PPID 1`. Right for long
unattended batches. Trade: no exit notification, so liveness must be polled and killing is
manual.

> **Hard checkpoint.** A live run against a repo other than this one writes to that repo and
> opens PRs there. **The human picks the specific issues and approves before any write or
> PR** — never selected autonomously. Mode (b) is the sanctioned shape.
>
> The exception: a repo explicitly designated **experimental** in `CLAUDE.md`'s list may
> batch without per-issue approval once you say go. The designation is a committed edit to
> that list, never something inferred — see the checkpoint section there for exactly what
> it relaxes (task selection and PR-opening) and what it doesn't (merges, issue-filing
> confirmation).

### Monitoring

```bash
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT dashboard --watch
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT watch --activity
```

The driver also narrates itself to `runs/<run>/driver.jsonl` and stderr: a start record, a
heartbeat per tick carrying utilization and dispatch limit (and while sleeping, the wait
reason), and an exit record. `status`'s `driver` block merges the pid claim with the last
heartbeat — `alive`, `heartbeat_age_s`, `last_state` — so "sleeping out a capacity stall" and
"died forty minutes ago" no longer look alike. A long silence is a stalled or dead driver,
not a quiet one.

### If the driver dies

Re-invoke the **exact same** `run-headless` command. It resumes: leases left by the dead
driver are reclaimed, the same attempt re-dispatches from its last checkpoint, and no retry
budget is spent. If it stops holding leases it may not reclaim (another live driver, a
foreign host), it exits non-zero with
`scheduler.exit_reason = blocked_on_orphaned_dispatches` rather than looking finished. The
terminal escape hatch is `orchestrator abandon`.

### Reading the result

```bash
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT status
```

Gate on four things:

- `run_state` is `completed` or `failed`
- `lane_audit.clean == true` — every recorded call attributed, none unattributed
- `events_audit` — `dispatched == recorded`. A non-zero `outstanding` alongside
  `"clean": true` means orphaned leases, not a healthy run
- `driver` — `alive`, `heartbeat_age_s`, `last_state`, `exit_reason`

---

## Phase 5 — after the batch

The engine's job ends with open PRs. This phase is human-driven, and it's where the
expensive mistakes live.

### Merge in dependency order

t1 before t2 before t5. The PRs were built on a DAG; merging out of order reintroduces the
conflicts the engine avoided.

### Verify every issue actually closed

When a PR closes multiple issues, the deliver stage writes a comma-list (`Closes #7, #8`).
**GitHub honors only the first ref.** The rest stay open silently and look like unfinished
work forever. List the issues and confirm — don't trust the PR body.

### Clean up — but never the run logs

Remove worktrees, task branches, and checkpoint tags. **Never `rm -rf runs/<run>/`.** That
directory is the durable audit trail: `status`, `events.jsonl`, `stage-costs.jsonl`,
per-stage prompts and outputs, cost summary. It's gitignored, so keeping it costs nothing.
Prune it yourself, deliberately — nothing automated should.

### Trunk gate

```bash
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT \
    trunk-gate -C /path/to/merged-trunk-checkout
```

Each PR was verified green *in isolation* against its own branch. Six individually-green
changes can still produce a red trunk once merged together.

The gate shells out to your adapter's declared verification commands — only adapter argv,
never a hardcoded pytest/ruff/mypy, so the engine stays project-agnostic — over an
**already-merged trunk checkout**. On red it best-effort files one `bug`-labeled
remediation task, deduped against a prior filing, and exits non-zero so it wraps cleanly into
CI.

**Caller contract:** you must ensure the merged checkout at `-C` exists. A missing path is
reported as red (files nothing) rather than silently running the commands against the
process's own tree.

Deliberately narrow: it does not orchestrate merges, does not block pre-merge, and does not
auto-remediate. It reports and files; something external invokes it after the PRs land.

### Acceptance pass (spec-originated batches)

```bash
uv run orchestrator --project "$PROJECT" spec conformance ./specs/<slug>.json
```

Per-task review checked each task against *its own* issue. Nothing has yet checked the
assembled whole against the spec you wrote. Six green tasks is not "the thing I asked for
exists."

The command owns the deterministic half — every spec task's issue, its state, any
discoverable PR, and the acceptance criteria parsed from the body — and exits non-zero while
any issue is still open.

Then do the half code can't: walk **each acceptance criterion against the actual merged
diffs**, reading the code rather than the checkmarks. A closed issue means a task shipped,
not that its promise was kept. File a `spec-gap` issue for anything genuinely unmet, quoting
the criterion and citing the spec slug — and stop for human confirmation before filing, same
rule as phase 2.

### Triage what the run auto-filed

```
/triage-followups
```

A run files issues as it goes — non-blocking review findings, improvement ideas from the
evidence-out seam, and cut-scope issues a task filed for things it consciously chose not to
do (matched by their `Source:` line). That auto-filing has no human gate by design, so
without triage the tracker fills with machine-generated noise.

This is that gate. Walk them **one at a time**, each explained from its source finding and
the code it points at, and decide: keep / close / promote / edit. Re-runnable later.

### Learn from the run

```bash
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT retrospective
uv run orchestrator --root runs --shared-root --run RUN --project PROJECT cost-report
uv run orchestrator kb add
```

`retrospective` surfaces recurring failure patterns and what retries learned; `cost-report`
shows per-stage spend and the session-reuse win (`--by-effort` splits by stage/effort/model).
`kb add` records a lesson that future runs carry.

This is also the moment to re-run the phase 1 bootstrap in **tune mode**. It reads
`retrospective.md` and `cost-report.md` and proposes evidence-based deltas: a stage that keeps
failing the same way suggests a different agent for that role or a missing classifier rule; a
cheap file-patching stage suggests routing it to a cheaper lane. Additive — it won't clobber
hand-edits.

---

## The steady state

```
idea → /brainstorm → /spec-intake → add-task/batch-plan → run-headless
     → merge → trunk-gate → conformance → /triage-followups → retrospective ↺
```

Phase 0 never repeats. Phase 1 is revisited to tune. Phases 2–5 are the loop.

See `CHEATSHEET.md` for the commands and skills on one page.
