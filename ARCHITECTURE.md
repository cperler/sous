# Architecture — a contributor's map

A one-page map of where things live and how they fit. It grounds every claim in the
code (paths are real — grep them). For the *why* and the historical record see
`docs/orchestration-spec/` (target + as-built spec) and `docs/reviews/` (design notes).

## The load-bearing idea: engine / adapter split

**The engine never calls a model.** It emits a `WorkItem`, a supervisor dispatches it on
some execution lane, and the engine ingests the returned `StageResult`. That contract seam
(`orchestrator/schemas/work.py`) is what makes execution lanes interchangeable, runs
resumable after a kill, and every model call structurally attributable (no ledger row can
be skipped). The engine imports no adapter; adapters import the engine's schemas, never the
reverse.

```
                 orchestrator/  (the engine — deterministic, project-agnostic, no model call)
                 emits WorkItem ──▶            ◀── ingests StageResult
                        │                                  ▲
        ┌───────────────┴──────────────┐                   │
        ▼                              ▼                    │  dispatch on a lane
  adapters/execution/            adapters/project/          │
  HOW/WHERE a call runs:         WHAT a repo plugs in:      │
   interactive (Workflow shim)    base.py  (the contract)   │
   headless_claude (claude -p)    heysoo/  selfhost/        │
   codex (codex exec)             github_issues.py          │
   deterministic_* (ENGINE lane,  external repos plug in    │
     no model — setup/test/         via <repo>/.orchestration/
     deliver)                       (path-load) or an entry point
        │                                                   │
        └──▶ model conversations live in .claude/skills/ ───┘
             (front-door + supervisor skills; run_targets/ holds their
              sources + the Workflow shim JS; templates/project-default/
              is the scaffold kit seeded into a new project's .claude/)
```

- **Two orthogonal axes:** `execution_mode × provider`. `ExecutionMode ∈ {interactive,
  headless, engine}`, `Provider ∈ {claude, codex, none}` (`orchestrator/schemas/enums.py`).
  Billing is a derived property of the (mode, provider) cell, not a hardcoded branch. `codex`
  is always headless; `codex×interactive` is an intentional empty cell; the `engine×none`
  cell is the deterministic, no-model lane.
- **Two adapter families.** `adapters/execution/` = how/where a call runs (the interactive
  `interactive.py` shim, `headless_claude.py`, `codex.py`, and the deterministic
  `deterministic_setup.py` / `deterministic_test.py` / `deterministic_deliver.py`; wired by
  `base.py` + `runners.py`, dispatched through `transport.py`, with `review_panel.py` fanning
  a plan-bearing REVIEW out into finder/verifier sub-calls below the seam). `adapters/project/` = what a
  repo supplies (commands, test taxonomy, agent roster, task source): `base.py` is the
  contract, `heysoo/` and `selfhost/` are the in-repo reference adapters, `github_issues.py`
  a shared task source. A new external project's adapter lives in **its own repo** under
  `<repo>/.orchestration/` (loaded by path, contract-version-checked) or ships as a package
  registering an `orchestrator.project_adapters` entry point.
- **Per-stage tool posture, translated per lane** (#272). `StageSpec.tool_policy` declares what
  a stage's dispatch may *do* in the engine's own provider-neutral words (`allow_file_writes`,
  `allow_command_execution`) — never a claude tool name — and each transport translates it:
  claude `--disallowedTools Write,Edit,NotebookEdit`, codex `--sandbox read-only` (on the
  resume call too, so continuity can't revert the posture). Only **REVIEW** declares one:
  writes denied, **command execution deliberately retained**, because an adversarial verifier
  refutes a finding by running the suite. Panel finders/verifiers inherit it. `--disallowedTools`
  is genuinely enforced under `--dangerously-skip-permissions` (the tool is absent from the
  toolset, not merely prompted) — and that flag deliberately **stays**: headless dispatch is
  non-interactive by construction, there is no human to answer a prompt and a prompt would hang
  the run, so the fix narrows the toolset rather than restoring interactive gating. A lane that
  cannot translate the posture (the interactive shim) declares
  `CapabilityDescriptor.enforces_tool_policy = False` and the engine emits one warning-grade
  `tool_policy_unenforced` event per dispatch, so declared-but-unenforced is never silent.
  Because the posture is derived from the stage and lane (both already hashed), it is dispatch
  metadata excluded from `content_hash` like `cwd`/`session_ref`.

## The pipeline

Six collapsed stages (`Stage` in `orchestrator/schemas/enums.py`), collapsed from the
reference system's ~12–15:

```
  intake ──▶ scope ──▶ implement ──▶ test ──▶ deliver ──▶ review
  (worktree,  (plan,    (edit +      (run     (push +     (approve /
   baseline)   feasible?) commit)     suite)   open PR)    reject → fix cycle)
```

- **Per-task pipeline (schema v2/v3).** `STAGE_ORDER` is only the display order and the FULL
  preset; the state machine (`orchestrator/state_machine.py`) walks each task's own
  `Task.pipeline`, never the constant. Lane presets `full | lite | micro` (`LANE_STAGES`)
  resolve to a concrete pipeline at `add_task`; e.g. `micro` drops scope and test.
- **Deterministic stage executors** run on the `engine×none` lane — no model, $0. Intake
  (`deterministic_setup.py`) creates the worktree/branch and captures the test baseline;
  `deterministic_test.py` runs the suite and classifies failures; `deterministic_deliver.py`
  pushes and opens/reuses the PR. Mechanical work is scripts, not model calls (heysoo #227:
  an LLM asked to run `git worktree add` answers in prose and fails schema validation).
- **Dispatch/record contract.** `Engine.next_work()` emits an immutable `WorkItem` whose
  `content_hash` (over stage+prompt+schema+model+lane+attempt) is its idempotency key; the
  task holds a **dispatch lease** (`pending_work_item_id`) so an in-flight or crashed-mid-stage
  task is never re-picked. `Engine.record()` ingests the `StageResult` under a locked
  read-modify-write, re-checking the lease. Cost is computed from the engine's own
  `model_table`, never the runner's self-report (`orchestrator/cost_ledger.py`).
- **Context plane.** Stages hand data forward through an engine-owned whitelist, not free
  text: `CONTEXT_KEYS` in `state_machine.py` names exactly which structured keys each stage
  folds into `task.context` (e.g. intake → `branch`/`worktree`/`baseline_failures`, deliver
  → `pr_url`). The fold is bounded (per-value caps + a 16 KB whole-context ceiling evicted
  heaviest-key-first) and deterministic, so replay reproduces it. `DETERMINISTIC_ONLY_KEYS`
  (`change_class`) fold only from the ENGINE lane — a model can't claim "docs-only" to relax
  its own review.

## The three front doors (upstream of any run)

A run consumes a DAG of tasks; three front doors produce one. Each has a deterministic module
(validate/rank/file — no model) paired with a `.claude/skills/` skill that runs the model
conversation:

- **brainstorm** (`orchestrator/brainstorm.py`, `schemas/brainstorm.json`) — a fuzzy area →
  scored candidate ideas → a ranked shortlist the human picks from. Small picks file as
  standalone enhancement issues; large ones feed spec-intake. The upstream-of-upstream.
- **spec-intake** (`orchestrator/spec_intake.py`, `schemas/spec.json`) — a known idea →
  validated, dependency-ordered spec → files each task as a GitHub issue in topological
  order, translating local `depends_on` ids into real `Depends-on: #N` refs.
- **batch-plan** (`orchestrator/batch_plan.py`, `schemas/batch_plan.json`) — a pile of
  *already-filed*, independently-authored issues → a validated dependency-ordered plan
  applied to a run via `Engine.add_task`. Reuses spec-intake's DAG validation.

## Control loops (all in the engine; adapters supply no logic)

- **Review gate + fix cycles + convergence.** A REVIEW result reporting `approved=false`
  triggers a fix cycle: `reset_for_fix_cycle` re-opens implement→…→review (bounded by
  `max_review_cycles`). Convergence auto-approval (`_review_verdict`): a re-review whose
  blocking issues are a subset of the prior rejection's — no net-new findings — ends the
  loop. Deterministic project policy findings merge in via the `review_findings` hook (#65)
  and force `approved=false`.
- **Failure taxonomy → recovery.** `orchestrator/retry.py` does retry-with-learnings
  (learnings appended, newest last) behind a *structured* circuit breaker (a hash over the
  normalized failure set — timestamps/paths/numbers scrubbed — so the breaker actually trips).
  The engine layers on: **salvage** (keep commits an attempt made before a TIMEOUT/RATE_LIMIT/
  INFRA death, since that code isn't the defect — `SALVAGEABLE_FAILURE_STATUSES`), **warm-retry**
  (opt a failed attempt's provider session into the retry), **infra-reset** (`max_infra_resets`),
  and a **rate-limit cooldown** (park the task `not_before` a timestamp; the scheduler sleeps on
  the soonest cooldown instead of spinning). Checkpoint tags let a retry hard-reset the worktree
  to the last good state.
- **Capacity + cost bands.** `orchestrator/capacity.py` turns the current 5h utilization
  (`--util`, sensed by `usage_probe.py`) into the binding `dispatch_limit` (how many tasks may
  run) and a `dispatch_band` (which model a fresh dispatch runs on) — the execution adapter's
  own cap is merely a ceiling. `orchestrator/cost_policy.py` is the USD sibling: remaining
  budget fraction routes new tasks to cheaper lanes / the $0 deterministic runners. Distinct
  levers: rate-limit headroom vs dollars.
- **Cross-provider fallthrough.** A `provider_unavailable` result (codex CLI missing / auth
  expired) can fall through to claude when the run opts in (`cross_provider_fallback`); off, it
  degrades to a normal retry-then-fail.
- **Human approval gate.** A task parks in the non-terminal `BLOCKED_ON_HUMAN` state — via an
  explicit `hold`, a scope stage reporting `feasible=false`, or exhausted fix cycles — and the
  run cannot silently complete past it. The only exits are `Engine.approve()` (writes a durable
  approval artifact recording who/what) and `Engine.reject()` (task ends
  `completed_with_rejections`, rejection surfaced to the task source). This is the engine-side
  enforcement of the live-run checkpoint: autonomous paths park; humans release.
- **Abandon path for a killed-mid-dispatch run.** When a run dies while a dispatch is
  outstanding (`pending_work_item_id` held), every state-changing path correctly refuses —
  `record` demands the matching result, `hold`/`reject` demand a quiescent/held task — leaving
  the lease stuck. `Engine.abandon()` (`orchestrator abandon`) is the sanctioned finalize: it
  synthesizes the lease-matching abandonment internally (honest $0 cost row, `dispatch_abandoned`
  stage log + event), clears the lease, and drives the task terminal (`failed`, or
  `rejected` → `closed_infeasible` with the rejection artifact), then runs the same
  cascade/release-ports/harvest/finalize effects `reject` does. A liveness guard (the `#66`
  stream probe) refuses while the dispatch's provider stream grew within `--min-idle-s`; `--force`
  overrides when the operator knows the process is dead.
- **Cross-run learnings KB.** `orchestrator/learnings_kb.py` persists a shared
  `<runs-root>/learnings-kb.jsonl` across runs: terminal tasks harvest their learnings
  (classified, fingerprint-deduped), and each new task's FIRST stage recalls relevant prior
  entries into the `prior_learnings` context key — read-only advisory text, folded once per
  task, rendered (hedged) into every stage prompt. `orchestrator kb capture|apply|show|gc`
  is the manual surface.
- **Batch scheduler.** `orchestrator/scheduler.py` is a thin hub-and-spoke loop over the DAG
  (`orchestrator/dag.py` — transitive cascade-blocking). Each tick dispatches the
  dependency-satisfied, non-terminal tasks within the capacity limit; a batch-wide circuit
  breaker pauses the run after N consecutive task failures (only genuine execution failures
  count — a human `reject` doesn't), and a paused run refuses to schedule until
  `orchestrator unpause`. All state is persisted by the engine, so a fresh scheduler on the
  same run dir resumes where a kill left off.

## Observability

Every run is a self-contained directory (`runs/<run>/`, gitignored, **retained until the
human deletes it** — cleanup never touches it). `--root runs` is the natural spelling
everywhere: per-run commands auto-nest the store under `<root>/<run>/` when the root is a
shared runs-root (holds other runs' stores or the learnings KB), so run dirs and the
cross-run `learnings-kb.jsonl` share one parent:

```
  runs/<run>/
    status-<run>.json, status-<run>-<task>.json   run + per-task documents
    events.jsonl                                   append-only audit sidecar
    stage-costs.jsonl                              per-call cost ledger rows
    cost-summary.md, cost-report.md                rendered rollups
    stages/<task>/NN-<stage>.{json,md}             per-stage record + prose
    stages/<task>/<stage>-attempt<N>.stream.jsonl  full raw provider stream (retained
    stages/<task>/index.md                          evidence — teed live, never pruned)
```

- **CLIs** (`orchestrator/cli.py`): `status` (progress + cost + lane-attribution audit),
  `watch` (poll one run to terminal, alerting on stalls), `tail` (live tail of a running
  stage's stream via `stream_probe.py`), `dashboard` (`dashboard.py` — cross-session board of
  all runs, "what needs a human" lifted to an attention band), `cost-report`, `retrospective`,
  `util` (probe the account's 5h/7d utilization, feeds `--util`), `statusline` (one-line
  utilization for the Claude Code status bar, off the same usage cache).
- **Seams** (`adapters/project/base.py`, all duck-typed/best-effort): `notify` /
  `emit_notification` for stall + transition alerts (`alerting.py`), `publish_progress` /
  `publish_note` to post progress to the task source, `file_followup` to file follow-up
  issues. A raising or missing hook never breaks a run.
- **Retrospective** (`orchestrator/retrospective.py`): on a failed run, folds
  `events.jsonl` + per-stage logs into per-task failure trails, the cascade map, and recurring
  error patterns (same structured signature the breaker uses).

## Where do I start reading

1. `orchestrator/schemas/enums.py` + `schemas/work.py` — the vocabulary and the
   `WorkItem`/`StageResult` contract everything keys off.
2. `orchestrator/engine.py` — `next_work` (emit) and `record` (ingest); the whole control
   flow hangs off these two methods.
3. `orchestrator/state_machine.py` — stage transitions, the context-plane fold, fix-cycle reset.
4. `orchestrator/scheduler.py` — how a batch fans the engine over a DAG.
5. `tests/` (838 cases) — the behavioural spec; a `test_<subsystem>.py` exists per module, and
   reading one is the fastest way to see a subsystem's contract exercised.

Then, for depth and history: `docs/orchestration-spec/target.md` (the implementation-agnostic
target the rebuild was built from), `docs/orchestration-spec/as-built.md` (the read-only
extraction of the reference bash system), and `docs/reviews/` (the design notes behind the
context plane, per-task pipelines, session continuity, and the front doors).
