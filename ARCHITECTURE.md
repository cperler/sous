# Architecture — a contributor's map

A one-page map of where things live and how they fit. It grounds every claim in the
code (paths are real — grep them). For the *why* and the historical record see `docs/`
(the frozen build record — design notes, the phased plan, and the design-pass reviews).

## The load-bearing idea: engine / adapter split

**The engine never calls a model.** It emits a `WorkItem`, a supervisor dispatches it on
some execution lane, and the engine ingests the returned `StageResult`. That contract seam
(`orchestrator/schemas/work.py`) is what makes execution lanes interchangeable, runs
resumable after a kill, and every model call structurally attributable (no ledger row can
be skipped).

**The dependency arrow points inward** (#273): `adapters/` imports `orchestrator/`, and no
module under `orchestrator/` imports `adapters` at all. The engine OWNS both adapter
contracts as *ports* — `orchestrator/ports/execution.py` (`Runner`/`CapabilityDescriptor`/
`Registry`/`default_registry`) and `orchestrator/ports/project.py` (`ProjectConfig`/
`TaskSource`/`TaskSpec`/`ADAPTER_CONTRACT_VERSION`) — and an adapter implements them.
Concrete adapters are reached by NAME, never by import, at the composition root:
`orchestrator/lane_loader.py` resolves the execution-lane bundle through the
`orchestrator.execution_lanes` entry point (this repo registers `adapters.execution.runners`
there), and `orchestrator/project_loader.py` resolves a project adapter by path, dotted
module, or `orchestrator.project_adapters` entry point. `adapters/execution/base.py` and
`adapters/project/base.py` survive as re-export shims so adapters scaffolded before the move
keep importing; they re-export the *same* objects, never redefinitions. The rule is enforced,
not just documented: `tests/test_dependency_direction.py` walks the AST of every engine
module (catching lazy imports and `importlib.import_module("adapters…")` too) with an empty
allowlist, and ruff's `TID251` ban fails the lint gate at the line where such an import is
written.

```
                 orchestrator/  (the engine — deterministic, project-agnostic, no model call)
                 emits WorkItem ──▶            ◀── ingests StageResult
                 orchestrator/ports/  ← the contracts; imports NOTHING outward
                   execution.py  Runner / CapabilityDescriptor / Registry
                   project.py    ProjectConfig / TaskSource / TaskSpec
                        ▲                                  ▲
        ┌───────────────┴──────────────┐                   │
        │        implement the ports   │                   │  dispatch on a lane
  adapters/execution/            adapters/project/          │
  HOW/WHERE a call runs:         WHAT a repo plugs in:      │
   interactive (Workflow shim)    selfhost/                  │
   headless_claude (claude -p)    github_issues.py          │
   codex (codex exec)             external repos plug in    │
   deterministic_* (ENGINE lane,    via <repo>/.orchestration/
     no model — setup/test/         (path-load) or an entry point
     deliver)                                               │
   runners.py = the lane BUNDLE, resolved by name           │
     (orchestrator.execution_lanes entry point)             │
        │                                                   │
        └──▶ model conversations live in .claude/skills/ ───┘
             (front-door + supervisor skills; run_targets/ holds the
              Workflow shim JS + the adapter-bootstrap skill;
              templates/project-default/ is the scaffold kit seeded into a
              new project's .claude/ — its skill copies are byte-identical
              to .claude/skills/, pinned by a test;
              templates/project-skeleton/ is a DIFFERENT thing — the phase-0
              repo skeleton init-project writes for a project that does not
              exist yet, before any adapter exists to seed a kit into)
```

- **Two orthogonal axes:** `execution_mode × provider`. `ExecutionMode ∈ {interactive,
  headless, engine}`, `Provider ∈ {claude, codex, none}` (`orchestrator/schemas/enums.py`).
  Billing is a derived property of the (mode, provider) cell, not a hardcoded branch. `codex`
  is always headless; `codex×interactive` is an intentional empty cell; the `engine×none`
  cell is the deterministic, no-model lane.
- **Two adapter families, one contract home.** Both contracts live inward under
  `orchestrator/ports/` (#273); `adapters/` holds only implementations.
  `adapters/execution/` = how/where a call runs (the interactive `interactive.py` shim,
  `headless_claude.py`, `codex.py`, and the deterministic `deterministic_setup.py` /
  `deterministic_test.py` / `deterministic_deliver.py`; bundled by `runners.py`, dispatched
  through `transport.py`, with `review_panel.py` fanning a plan-bearing REVIEW out into
  finder/verifier sub-calls below the seam). `adapters/project/` = what a repo supplies
  (commands, test taxonomy, agent roster, task source): `selfhost/` is the in-repo
  reference adapter (this repo self-hosting), `github_issues.py` a shared task source. A new external
  project's adapter lives in **its own repo** under
  `<repo>/.orchestration/` (loaded by path, contract-version-checked) or ships as a package
  registering an `orchestrator.project_adapters` entry point.
- **Per-stage tool posture, translated per lane** (#272, widened/decided by #327).
  `StageSpec.tool_policy` declares what a stage's dispatch may *do* in the engine's own
  provider-neutral words (`allow_file_writes`, `allow_command_execution`) — never a claude tool
  name — and each transport translates it: claude `--disallowedTools Write,Edit,NotebookEdit`,
  codex `--sandbox read-only` (on the resume call too, so continuity can't revert the posture)
  unless REVIEW is already inside the disposable workspace described below.
  **SCOPE and REVIEW** declare one (#303): both read the repo and return a document, so both get
  writes denied with **command execution deliberately retained** — a scoper reads and greps, an
  adversarial verifier refutes a finding by running the suite. Panel finders/verifiers inherit
  it. `--disallowedTools` is genuinely enforced (the tool is absent from the toolset, not merely
  prompted), independent of the permission gate below.
- **REVIEW execution isolation** (#301). Every in-process headless REVIEW transport call runs
  in an independent throwaway local clone seeded with the live worktree payload (including
  ignored dependencies and dirty state, excluding its linked `.git`). The clone has its own
  object database and no `origin`, so shell writes, caches, Codex persona materialization, and
  accidental Git writes cannot alter the judged tree. A plan's finder/verifier calls each get
  a fresh clone rather than inheriting a prior panel member's artifacts. Projects that opt into
  ports also allocate one temporary block per call; the panel holds all of them until it ends,
  so sequential sub-calls cannot reuse a block, then releases them on every exit path. Setup or
  port exhaustion fails the call instead of falling back to the task worktree/block. Within
  that disposable checkout Codex uses `workspace-write`, allowing pytest/build caches despite
  its coarse sandbox; Claude keeps its finer write-tool deny-list. Interactive REVIEW remains
  outside this in-process runner boundary and retains #302's explicit unenforced posture.
- **Permission gate: a lane decision, not a constant** (#304, superseding #272's "the flag
  stays"). `--dangerously-skip-permissions` used to be appended to *every* headless dispatch
  from `transport.py`. It is now derived: `CapabilityDescriptor.permission_posture` declares a
  lane's default (`PermissionPosture.BYPASS` on every shipped lane, so unpoliced argv is
  byte-identical to pre-#304), and a **write-denying stage posture tightens it to RESTRICTED** —
  resolution is monotone, a stage can only tighten. #272's reasoning still holds for BYPASS
  (headless dispatch is non-interactive; nobody can answer a prompt and a prompt would hang the
  run) — what changed is that a read-only stage no longer needs blanket permission to avoid one.
  RESTRICTED emits no bypass flag and instead **pre-grants exactly the tools the posture allows**,
  read from the same `ToolPolicy` bits the deny-list reads — `--allowedTools Bash,BashOutput,
  KillShell` for a write-denied stage, the write tools too for a writing stage on a RESTRICTED
  lane, so withholding blanket permission never silently withholds a stage's own declared
  authority (with no TTY, granted-or-denied are the only safe states). Probed against the CLI,
  `Bash` runs (in subagents too), default read tools run unlisted, `Write` is refused by the
  deny-list, and an ungranted tool is refused in-band rather than stalling. codex has no blanket grant to withhold (`codex exec` is
  sandboxed on every path we emit and the true bypass is never used), so there a lane-level
  RESTRICTED changes nothing and a write-denying stage reaches `--sandbox read-only` unless its
  REVIEW call is already contained by #301's disposable workspace. A lane
  that must never hold blanket permission (shared/production checkout) now declares that on its
  descriptor instead of editing the transport.
- **Where a lane can't enforce, the degradation is explicit** (#302 — decided, not deferred
  again). interactive×claude keeps `CapabilityDescriptor.enforces_tool_policy = False`, because
  `run_targets/workflow_shim.js` calls `agent()` with model/effort/agentType/schema and no tool
  restriction — declaring `True` would be the same silent over-promise ruled out for
  `supports_plan` (#288). But the warning event is no longer the whole answer: it reports the gap
  to the human *afterwards* while the dispatch runs as if no posture existed. So on a
  non-enforcing lane `render_prompt` states the posture **in-band**, rendered from the policy
  itself (the `_NO_ATTRIBUTION_DIRECTIVE` shape: when the engine can't remove the capability, it
  overrides the standing instruction to use it), and the per-dispatch `tool_policy_unenforced`
  event stays alongside it. Prompt convention is a weaker guarantee than a removed tool, which is
  why it is scoped to the one lane with nothing better; it retires when `agent()` gains a tool
  option (the same change that flips the flag to `True`).
  Because posture and permission gate are both derived from the stage and lane (both already
  hashed), they are dispatch metadata excluded from `content_hash` like `cwd`/`session_ref`.

## The pipeline

The standing six-stage pipeline, collapsed from the reference system's ~12–15:

```
  intake ──▶ scope ──▶ implement ──▶ test ──▶ deliver ──▶ review
  (worktree,  (plan,    (edit +      (run     (push +     (approve /
   baseline)   feasible?) commit)     suite)   open PR)    reject → fix cycle)
```

`simplify` is an additional stage-vocabulary member, not a seventh step in the standing
FULL preset. SCOPE may opt a decomposed child with `quality_tier: full` into
`intake → implement → simplify → test → deliver → review`; `light` omits simplify and
`none` omits both simplify and review. The pass has its own WorkItem, checkpoint, agent,
timeout, and ledger row, so its cost and failures are visible without restoring the old
opaque quality loop.

- **Per-task pipeline (schema v2–v4).** `STAGE_ORDER` is the display order; the state
  machine (`orchestrator/state_machine.py`) walks each task's own
  `Task.pipeline`, never the constant. Lane presets `full | lite | micro` (`LANE_STAGES`)
  resolve to a concrete pipeline at `add_task`; e.g. `micro` drops scope and test.
- **Deterministic stage executors** run on the `engine×none` lane — no model, $0. Intake
  (`deterministic_setup.py`) creates the worktree/branch and captures the test baseline;
  `deterministic_test.py` runs the suite and classifies failures; `deterministic_deliver.py`
  pushes and opens/reuses the PR. Mechanical work is scripts, not model calls (an LLM asked
  to run `git worktree add` answers in prose and fails schema validation).
- **Dispatch/record contract.** `Engine.next_work()` emits an immutable `WorkItem` whose
  `content_hash` (over stage+prompt+schema+model+lane+attempt) is its idempotency key; the
  task holds a **dispatch lease** (`pending_work_item_id`) so an in-flight or crashed-mid-stage
  task is never re-picked. `Engine.record()` ingests the `StageResult` under a locked
  read-modify-write, re-checking the lease: a result whose `content_hash`/work-item/stage/
  model/attempt does not answer the outstanding dispatch is refused (`ContractError`) and
  audited as a warning-grade `result_rejected` event — never folded silently (#311). Cost
  is computed from the engine's own
  `model_table`, never the runner's self-report (`orchestrator/cost_ledger.py`).
- **Context plane.** Stages hand data forward through an engine-owned whitelist, not free
  text: `CONTEXT_KEYS` in `state_machine.py` names exactly which structured keys each stage
  folds into `task.context` (e.g. intake → `branch`/`worktree`/`baseline_failures`, deliver
  → `pr_url`). The fold is bounded (per-value caps + a 16 KB whole-context ceiling evicted
  heaviest-key-first) and deterministic, so replay reproduces it. `DETERMINISTIC_ONLY_KEYS`
  (`change_class`) fold only from the ENGINE lane — a model can't claim "docs-only" to relax
  its own review.
- **SCOPE decomposition.** A large task may return a validated `subtasks` DAG with local
  ids, agent roles, quality tiers, implementation budgets, and local dependency edges.
  The engine files durable child tasks through the task-source `create_task` hook, records
  the local-id mapping after every acknowledged filing, and registers each child on the
  existing run DAG. The original task becomes an umbrella that depends on the child leaves:
  independent children can complete even if another branch fails, dependency failures use
  the existing transitive cascade, and the umbrella auto-completes only after every leaf
  succeeds. An unsupported or failed source parks the parent for approval instead of
  silently falling through to monolithic implementation. Filing is a saga around an
  external side effect, so it gets two distinct guards: a per-parent decomposition lock in
  the store (`with_decomposition_lock`) serializes the whole saga across processes and
  re-reads the parent's mapping inside the lock, so concurrent reconcilers cannot each file
  their own issue for one subtask (#354); a deterministic body marker plus the source's
  `list_tasks` hook then closes the remaining create/record CRASH window, best-effort, for
  sources that support lookup.

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

Upstream of all three, but deliberately **not** a fourth front door — it produces a repo,
not tasks — is **project_init** (`orchestrator/project_init.py`, `init-project`,
`templates/project-skeleton/`): the phase-0 skeleton for a project that does not exist yet,
paired with the `new-project` skill on the same deterministic-module + skill split. It
writes, commits, and then runs the skeleton's own verification commands, creating the
GitHub repo only on green — those commands become the adapter's declared contract in the
bootstrap step, so a skeleton that cannot pass them would hand the engine gates it can
never satisfy.

## Control loops (all in the engine; adapters supply no logic)

- **Review gate + fix cycles + convergence.** A REVIEW result reporting `approved=false`
  triggers a fix cycle: `reset_for_fix_cycle` re-opens implement→…→review (bounded by
  `max_review_cycles`). Convergence auto-approval (`_review_verdict`): a re-review whose
  blocking issues are a subset of the prior rejection's — no net-new findings — ends the
  loop. Deterministic project policy findings merge in via the `review_findings` hook (#65)
  and force `approved=false`. An improvement dispositioned `fixup` uses the same bounded
  tail reset after REVIEW, carrying the request into IMPLEMENT and re-running delivery on
  the existing PR. The task retains the request outside the reset REVIEW record; only a
  later approving review marks it applied, while a repeated/unapplicable fixup parks.
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
- **Interactive supervisor context.** Claude Code supplies context-window counters to the
  configured `orchestrator statusline` command, which caches a fresh cwd-keyed snapshot.
  Guarded interactive `next` evaluates its exact rendered prompt in memory and reserves a
  conservative prompt estimate plus 20% of the window before it writes the prompt artifact,
  emits a WorkItem, or commits `pending_work_item_id`. Insufficient/stale sensing parks the
  run in non-terminal `PARKED` with a `supervisor_parked` event and resume command. Parking
  is refused while any batch lease remains; the supervisor stops refilling, drains results,
  and retries at the lease-free boundary. A parked run is neither stale nor waiting on a
  human decision; `resume-supervisor` releases it in a fresh session.
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
- **Retire path for a superseded run.** `Engine.retire()` (`orchestrator retire`) finalizes a
  whole run the human deliberately SUPERSEDED — the issue was amended and the batch rebuilt
  as a successor run. It is run-level, needs NO lease (the common case is a cleanly-recorded
  run the human walked away from; `--force` covers an outstanding one, billing it unmetered
  like `abandon`), and drives every non-terminal task — including `blocked_on_human`, which
  no other path can move without a rejection — to the terminal `superseded` state, then to
  run state `superseded` with a `run_superseded` event carrying the reason and successor run
  id. The defining property is that it publishes NOTHING to the task source: a superseded
  run's issues are typically live in the successor run, so `reject`/`abandon --rejected`
  marking them infeasible would be actively wrong. It also skips cascade-blocking (a
  dependent is superseded by the same call, not failed) and the `task_failed` alert, but does
  release ports and harvest learnings. Unlike the derived rollups, the run state is DECLARED,
  so a later finalize can never rewrite it. The run log dir is retained as always — this is
  about state, not cleanup. Previously the only workaround was `hold`, which silenced the
  stale alarms but left the run `running` forever, permanently occupying the monitor's
  "needs you" list.
- **Task spec snapshot + sanctioned refresh.** A task's `title`/`body` are snapshotted onto the
  Task doc once, at `add_task` (`add-task` / `batch-plan apply`), and every stage prompt for the
  rest of the run renders from that copy — that is deliberate: it is what makes a run reproducible
  and stage prompts byte-stable. What it cost was amendability: editing the upstream issue mid-run
  reached nothing and nothing said so, so the only workarounds were rebuilding the run or
  hand-patching the status JSON behind the engine. `Engine.refresh_spec()`
  (`orchestrator refresh-spec --task '#N'`) is the sanctioned move: re-resolve the task source,
  write the new title/body under the per-task lock, and emit `task_spec_refreshed` carrying a diff
  summary — emitted even when nothing changed, so "verified identical" and "never looked" don't
  read alike. `--check` is a dry run. It refuses a terminal task, and refuses while a dispatch
  lease is outstanding because that stage's prompt was ALREADY rendered from the old copy (the
  `#256` failure: a plan contradicting its own task's spec); `--force` overrides and stamps
  `leased_dispatch` on the event. Refreshing is never automatic — the engine does not re-resolve
  per stage. Staleness is visible rather than hidden: `add_task` records `spec_captured_at` /
  `spec_source_updated_at` / `spec_fingerprint`, and `orchestrator status --check-spec` (opt-in,
  like `--activity`, so the cheap poll path stays offline) flags a task whose upstream content
  fingerprint has diverged. An unreachable source degrades to `spec_check_error`, never a failed
  status dump.
- **Cross-run learnings KB.** `orchestrator/learnings_kb.py` persists a shared
  `<runs-root>/learnings-kb.jsonl` across runs: terminal tasks harvest their learnings
  (classified, fingerprint-deduped), and each new task's FIRST stage recalls relevant prior
  entries into the `prior_learnings` context key — read-only advisory text, folded once per
  task, rendered (hedged) into every stage prompt. `orchestrator kb capture|apply|show|gc`
  is the manual surface. REVIEW process retrospectives use a detector-only `process` kind:
  they never enter task prompts. `orchestrator/meta_authoring.py` groups those observations
  by their optional stage-template/agent/skill/schema/kit target and, after the same target
  appears in two distinct runs, files one evidence-backed `meta-authoring` task through the
  adapter's `file_followup` seam. `<runs-root>/meta-proposals.jsonl` prevents refiling.
- **Meta-authoring delivery gate.** Tasks sourced from issues labeled `meta-authoring`
  persist `hold_before=deliver`. They may scope, implement, test, and review normally, but
  `next_work` parks them `BLOCKED_ON_HUMAN` before DELIVER. Approval is keyed to the exact
  `before:deliver` hold, so an earlier scope/review approval cannot authorize delivery; the
  eventual PR merge remains the independent apply gate.
- **Batch scheduler.** `orchestrator/scheduler.py` is a thin hub-and-spoke loop over the DAG
  (`orchestrator/dag.py` — transitive cascade-blocking). Each tick dispatches the
  dependency-satisfied, non-terminal tasks within the capacity limit; a batch-wide circuit
  breaker pauses the run after N consecutive task failures (only genuine execution failures
  count — a human `reject` doesn't), and a paused run refuses to schedule until
  `orchestrator unpause`. All state is persisted by the engine, so a fresh scheduler on the
  same run dir resumes where a kill left off.
- **Pre-merge batch integration gate.** At the all-tasks-terminal boundary, the engine
  resolves completed leaf-task branches in stable dependency order and merges them over
  current trunk in a disposable detached worktree. Merge conflicts are red immediately;
  otherwise the composite runs the adapter's install command and then the same declared
  unit/e2e/shell/lint/type commands as the post-merge trunk gate. The result is
  report-and-file rather than merge orchestration:
  it is persisted and included in the final notification, and one deduplicated `bug` is filed
  on red, while the run's state remains the honest rollup of its task outcomes. A dedicated
  per-run lock makes repeated/concurrent finalizers reuse the receipt instead of re-running
  the gate. Filing persists a stable-key intent before the external call; the GitHub and
  local-file sources create-or-look-up by that key, so recovery cannot duplicate an issue if
  the process dies before the filing receipt. Dispatch eligibility reconciles an
  all-terminal/RUNNING run so restarting after a kill during the gate resumes finalization
  rather than exiting early.

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
    driver.jsonl                                   the DRIVER's own telemetry (#323):
                                                   start (pid/ppid/argv/settings),
                                                   a heartbeat per tick + per sleep
                                                   slice, and an exit record
    stage-costs.jsonl                              per-call cost ledger rows
    batch-integration-gate.json                    pre-merge composite gate detail (#370)
    approval-<run>-<task>.json                     human gate decision
    cost-summary.md, cost-report.md                rendered rollups
    stages/<task>/NN-<stage>.{json,md}             per-stage record + prose
    stages/<task>/<stage>-attempt<N>.stream.jsonl  full raw provider stream (retained
    stages/<task>/<stage>-attempt<N>.prompt.txt    the prompt that dispatch was sent —
    stages/<task>/index.md                          same stem as its stream (#314);
                                                    evidence — teed live, never pruned)
```

- **CLIs** (`orchestrator/cli.py`): `status` (progress + cost + lane-attribution audit),
  `watch` (poll one run to terminal, alerting on stalls), `tail` (live tail of a running
  stage's stream via `stream_probe.py`), `dashboard` (`dashboard.py` — cross-session board of
  all runs, "what needs a human" lifted to an attention band; `--watch` polls in the
  terminal and `--serve` binds `web_dashboard.py` as a local HTTP server), `cost-report`, `retrospective`,
  `util` (probe the account's 5h/7d utilization, feeds `--util`), `statusline` (one-line
  utilization plus context-window capture for the Claude Code status bar),
  `supervisor-context` (read that fresh payload), and `resume-supervisor` (release a
  lease-free interactive context park in a fresh session).
- **Driver telemetry** (`orchestrator/driver_log.py`, #323): `Scheduler.run` — the
  long-lived foreground process that owns a run — writes `driver.jsonl` and mirrors it to
  stderr. A heartbeat precedes each tick's dispatch and repeats every
  `--heartbeat-interval` seconds while sleeping (naming the wait reason and the
  utilization that gated it), so a driver waiting out a capacity stall is distinguishable
  from a wedged one; SIGTERM/SIGINT/SIGHUP are trapped for a last-gasp exit record and
  then re-raised unchanged. `status`'s `driver` block merges the #313 pid claim with the
  log's last heartbeat (`alive`, `heartbeat_age_s`, `last_state`), so a dead driver does
  not present identically to a working one.
- **Seams** (`orchestrator/ports/project.py`, all duck-typed/best-effort): `notify` /
  `emit_notification` for stall + transition alerts (`alerting.py`), `publish_progress` /
  `publish_note` to post progress to the task source, `file_followup` to file follow-up
  issues. A raising or missing hook never breaks a run.
- **Alerting payloads + email** (#359): the per-task pair is symmetric — `task_failed` from
  `_terminal_effects`, and `task_completed` from `_on_task_completed` (the choke point BOTH
  `record`'s success path and the decomposition-parent path pass through, so it fires exactly
  once). Both carry `Engine._notification_facts`: pr_url/pr_number, title, per-stage outcomes,
  the task's metered cost (with #319's unmetered count alongside, never a confident $0), and
  a pointer to the retained `runs/<run>/`; `task_completed` adds the `render_completion_note`
  markdown already published to the PR (reused, not re-authored — the engine never calls a
  model), and `run_finalized` adds a per-task roster so a batch digest is renderable. The
  derived blocks are best-effort and a thinned payload is evented
  (`notification_facts_degraded`), so a sink treats them as optional. DELIVERY stays an
  adapter concern: `adapters/project/email_sink.py` is a stdlib-`smtplib` sink, env-configured
  (`ORCHESTRATOR_SMTP_*` / `ORCHESTRATOR_NOTIFY_EMAIL_TO`, nothing checked in), absent unless
  configured, kind-filterable, and always short-timeout — the engine's `notify_failed` guard
  covers a raising sink, but only a timeout covers one that HANGS. Wired into the selfhost
  adapter; before this it had no `notify` at all, so every dogfood batch was silent.
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
5. `tests/` — the behavioural spec; a `test_<subsystem>.py` exists per module, and
   reading one is the fastest way to see a subsystem's contract exercised. Run the gate the
   way CI does: `uv run pytest`, `uv run ruff check .`, `uv run mypy`.

Then, for history: `docs/` (indexed by `docs/README.md`) — the original design notes, the
phased plan, and the design-pass reviews behind the context plane, per-task pipelines,
session continuity, meta-authoring, and the review workflow. It is a frozen record: where it
disagrees with the code, the code is right.
