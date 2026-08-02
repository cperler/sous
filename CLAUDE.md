# Orchestration Template

A project-agnostic orchestration harness — originally extracted from a bash system and
rebuilt in Python around an engine/adapter split. `README.md` is the project overview,
`ARCHITECTURE.md` the map of the system as built, and `docs/` the frozen build record
(historical only — see `docs/README.md`).

## Status
**In production use.** The rebuild is complete, the backlog is empty, and the harness is
now used for real work rather than built toward. All pytest cases green, ruff and mypy
clean; live-proven end to end (real issues → merged PRs, clean lane-attribution audits) and
self-hosted on this repo's own tracker. Ongoing work is incremental — pick from the issue
tracker or fix-forward.

## Working norms
- **Tests + lint + types, every change:** `uv run pytest`, `uv run ruff check .`, and
  the bare `uv run mypy` (no path arguments) must stay green (the same trio CI enforces —
  `.github/workflows/ci.yml`). `tests/` is intentionally outside the mypy gate via
  `[tool.mypy] files` in `pyproject.toml`; explicit paths surface pre-existing, out-of-gate
  errors. Add a regression test with each fix. **A run enforces this too, not just CI:**
  only pytest used to execute during a task (the deterministic TEST runner shells the
  `test_*_cmd` family and nothing else), so `SelfHostConfig.review_findings` — the #65
  policy-gate seam — runs ruff and mypy over the task worktree at REVIEW and returns a
  BLOCKING finding on red, which overrides an approving reviewer and re-opens the fix
  cycle with the tool output as learnings. A leg that cannot run at all degrades to
  advisory, so unverified never reads as green.
- **Nothing is silently dropped** (`gh issue list -R cperler/orchestration-template`).
  Anything cut, thinned, or found-missing while building gets an ordinary issue with a
  `**Source:** #N` line naming the task that cut it — that line, not a label, is what
  `triage-followups` matches on. **The label set is deliberately small and standard** —
  `bug`, `enhancement`, `docs`, `ux`, `chore`, `duplicate` — and every one of them means
  what it means anywhere else on GitHub. Do not invent a label for a workflow state; a
  label nobody can read without a glossary is the failure mode that retired
  `deferred-scope`. The single exception is FUNCTIONAL, not descriptive:
  **`meta-authoring`** is read by `Engine` (a task sourced from an issue carrying it
  persists `hold_before=deliver`), so it changes behavior rather than describing work.
  The engine itself labels sparingly: an `improvement` files as `enhancement`, a red
  post-merge trunk gate files as `bug`, and a review's non-blocking findings file
  **unlabeled** — the engine cannot tell whether a nit is a bug, a docs gap, or an
  enhancement, so it declines to guess and triage assigns one. Build narrative lives in
  commits, PRs, and issue comments, never in a ledger file. There is no `deferred-scope` and no
  gate-review ritual: both were rebuild-phase machinery for proving nothing was lost against
  a reference system that no longer exists, and half the label's population were review
  findings that never carried a deferral rationale at all. The frozen pre-2026-07-01 ledger
  is at `docs/deferred-history.md`.
- **When the agent keeps violating a rule, the rule is not the fix.** A directive the model
  ignores wants structural enforcement — a check the harness runs, a tool it cannot skip, a
  posture it cannot hold — or deletion. It does not want a louder restatement, which costs
  tokens in every prompt and buys nothing. #317→#322 is the shape: the no-attribution
  directive is prose, so a post-hoc audit (`commit_attribution.py`) verifies it and events
  the misses. This cuts the other way too — harness prose accretes a clause per incident and
  nothing removes them, so REVIEW's retrospective ask invites SUBTRACTIVE lessons (a rule
  that never fires, changes nothing, or contradicts another) as explicitly as additive ones.
- **Run-level settings must persist on the Run/Task doc, not engine memory.** Every CLI
  subcommand rebuilds the Engine from constructor DEFAULTS (`cli._engine` passes only
  store/ledger/project/router/registry), so a setting chosen at run-create time is lost by
  the next subcommand unless it is stored on `Run` (or `Task`) and re-read at the stage
  boundary that consults it (dispatch/retry/review-gate/filing/completion). A guard test
  (`tests/test_run_settings_persistence.py`) enforces that every `create_run` param is a
  `Run` field; the full audit + pattern is in
  `docs/reviews/2026-07-18-run-level-settings-persistence-audit.md` (#206).
- **The engine never calls a model and stays project-agnostic.** New projects plug in via
  a project-owned `<repo>/.orchestration/` adapter (loaded by path, contract-checked) or
  `adapters/project/<name>/` for in-repo reference adapters; new execution lanes via
  `adapters/execution/`. Don't add project-specific logic to `orchestrator/`.
- **The dependency arrow points inward — no module under `orchestrator/` may import
  `adapters`** (#273). The contracts are engine-owned ports (`orchestrator/ports/execution.py`,
  `orchestrator/ports/project.py`) that adapters implement; `adapters/*/base.py` are
  back-compat re-export shims for externally-scaffolded adapters (same objects, so the
  `EXPLICIT_EMPTY` identity check keeps working). When the composition root needs a CONCRETE
  adapter it resolves it by NAME, never by import: `orchestrator/lane_loader.py` (the
  `orchestrator.execution_lanes` entry point → `adapters.execution.runners`) and
  `orchestrator/project_loader.py`. Both a ruff `TID251` ban and
  `tests/test_dependency_direction.py` (AST-level, empty allowlist, catches lazy imports and
  `importlib.import_module("adapters…")`) fail on a violation — if you need an exception,
  argue it in review rather than appending to the allowlist.
- **Pure fold/state-machine functions return what they dropped; only the engine caller
  emits events.** The `state_machine` fold layer is pure (no wall-clock/random/I/O) so
  replay/resume is deterministic — never give it an event sink. When a fold silently drops
  or truncates something that should be observable (the "never silent" convention), have
  the pure function *return* a notice of what it dropped and let the engine call site emit
  the warning-grade event. Pattern established by #201 (`_absorb_outputs` → `apply_result`
  → engine emits `pr_field_dropped`); #289 extended it to the whole context plane — the
  fold returns a `FoldNotices` and the engine emits `context_value_truncated` (from
  `_cap_value`/`_cap_item`) and `context_key_evicted` (from `_enforce_context_ceiling`).
  #289 also fixed the cap that motivated it: per-item caps are chosen by FIELD MEANING
  (`_ITEM_CAP_BY_KEY`), so a SCOPE `plan`'s prose subtasks are no longer cut at the
  500-char incidental-list-item cap. #311 applied the same shape to a REFUSAL rather than a
  drop: `_lease_mismatch` is pure and returns `(reason_code, message)`, and both `record()`
  call sites (lock-free pre-check and the under-lock re-validation, whose event is emitted
  after the aborted transaction — never from inside the locked mutator) emit
  `result_rejected` before raising, so a rejected StageResult is loud in `events.jsonl`
  instead of only on the caller's stderr.
- **Commits carry NO model attribution trailer** (#317) — no `Co-Authored-By`, no model or
  agent name, in hand-authored and run-produced commits alike. Model self-report is
  unreliable (batch-headless-1 signed a commit `Claude Opus 4.5`, a model no stage of that
  run dispatched), and engine-stamped attribution was considered and rejected: routing is
  per-stage, so "the model" for a commit is ambiguous and any stamping policy is an arbitrary
  pick. Per-stage provenance already lives in `runs/<run>/events.jsonl` and
  `stage-costs.jsonl`. Because every claude/codex CLI carries a standing instruction to sign
  its commits, silence is not enough: `render_prompt` appends an explicit
  `_NO_ATTRIBUTION_DIRECTIVE` to every committing stage's prompt. A directive is not a
  guarantee, so #322 adds the post-hoc half: after every SUCCESSFUL checkpoint stage,
  `Engine._audit_commit_attribution` scans that stage's own commits (`<prev checkpoint or
  base_sha>..<checkpoint sha>`, pure detection in `orchestrator/commit_attribution.py`) and
  emits warning-grade `commit_attribution_trailer_found` per offending commit plus a
  `commit_attribution_scanned` receipt (clean and never-looked must not read alike).
  Report-only — it NEVER amends, because DELIVER pushes before its checkpoint lands and an
  engine-side amend would rewrite already-remote history. Work on `main`. Remote:
  `github.com/cperler/orchestration-template` (private; push `main` after committing).
- **Run logs are retained until the human deletes them.** Post-run cleanup removes the
  worktree, the task branch, and checkpoint tags — but NEVER the run's log dir under
  `runs/<run>/` (status/events.jsonl/stage-costs.jsonl/per-stage `stages/`/cost-summary).
  Those are the durable audit trail (`runs/` is gitignored — local, not committed). Do not
  `rm -rf runs/...` as part of cleanup; leave it for the human to prune explicitly.
- **The post-merge trunk gate is report-and-file, not merge orchestration** (#229, #216
  Option 2 half (b)). `Engine.trunk_gate(run_id, *, cwd, file_fix=True)` shells the project
  adapter's declared verification commands over an already-merged trunk checkout (only
  adapter argv — never a hardcoded pytest/ruff/mypy — so the engine stays project-agnostic
  and model-free) and, on red, best-effort files ONE `bug`-labeled remediation task,
  deduped on a prior `trunk_gate_fix_filed` event. The `trunk-gate` CLI subcommand exits
  non-zero on red for a CI/human wrapper. **Caller contract:** the invoker (human or CI
  wrapper) must ensure the merged-trunk checkout at `cwd` exists — the gate reports a
  missing `cwd` as red (`trunk_gate_error`/`cwd_not_found`, files nothing) rather than
  silently running the commands against the process's own tree, so it never verifies a tree
  other than the one it was asked to. Deliberately NOT built: no PR-merge orchestration,
  no pre-merge blocking gate (Option 2 half (a)), no auto-remediation RUN (file-only), and no
  automatic wiring into scheduler finalize (which is pre-merge). Someone/something external
  invokes it after the batch's PRs land.

## Running a batch: headless is the default lane
Use the **`orchestrate-batch-headless`** skill for ordinary batches. One `run-headless` driver
owns the run — backgrounded from the session, detached via fork+`os.setsid()`, or run in a
foreground terminal (the skill's modes (a)/(c)/(b), and note that a session-tracked background
task is reaped on a ~30-minute wall-clock cadence, so anything longer must be (c) or (b)); the
engine's `Scheduler` supervises and spawns `claude -p`
per stage, so no stage prompt or output passes through the session. Measured on
`batch-headless-1`: 92–96% cache hits on long stages (provider sessions chain via `--resume`),
real per-stage dollars in `stage-costs.jsonl`, `lane_audit` clean.

An all-codex batch requires the global `--provider codex` flag: `provider_tag` routes only
`routing.DEFAULT_CODEX_ELIGIBLE` (IMPLEMENT/TEST/SIMPLIFY), not the remaining stages. Even then,
`routing.engine_lane_required` vetoes DELIVER onto deterministic `ENGINE:none`, at `$0` and
without the model DELIVER's docstring refresh; the warning-grade
`stage_rerouted_to_engine_lane` records the move. The codex sandbox turns DELIVER's `git push`
into a keychain prompt that no unattended batch can answer (#364).

Reach for `orchestrate-batch-interactive` only when a human needs to watch each stage live.
That lane runs every stage through the session context AND records `$0.00`/zero tokens —
the Workflow shim cannot report usage, so all 15 pre-2026-07-30 interactive runs are
financially invisible. Cost-shaping inputs themselves ARE now captured on every lane (#314):
each dispatch persists its rendered prompt to `stages/<task>/<stage>-attempt<N>.prompt.txt`
and stamps `session_ref` + `prompt_sha256`/`prompt_chars`/`prompt_file` onto
`stage_dispatched`, so prompt bloat, cross-stage prefix drift, and session-continuity rate
are all answerable from a finished `runs/<run>/` (`events_audit` reports the continuity
block; a dispatch predating #314 counts as `unknown`, never a false 0%).

The driver owns the run for its whole duration: Ctrl-C kills the `claude -p` children via the
process group. Monitor from a second terminal. **The driver is no longer silent (#323):** it
writes `runs/<run>/driver.jsonl` (start record with pid/ppid/argv/resolved settings, a
heartbeat per tick and per sleep slice, an exit record for every catchable termination
including a trapped SIGTERM/SIGINT/SIGHUP) and mirrors each line to stderr, while stdout
stays one JSON status document. `status`'s `driver` block merges the pid claim with the last
heartbeat (`alive`, `heartbeat_age_s`, `last_state`), so "sleeping out a capacity stall" and
"died 40 minutes ago" no longer look identical — and after an uncatchable SIGKILL the last
heartbeat still bounds the time of death and names what the driver was doing. **Re-invoking `run-headless` after a kill now
resumes it (#313):** `Scheduler.run` stamps a driver claim (host/pid) on the Run doc and, at
the next startup, reclaims the leases that claim's now-dead process left — clearing
`pending_work_item_id` while leaving the stage `RUNNING`, so `next_work` re-dispatches the
SAME attempt from the last checkpoint and no retry budget is spent (each release is evented
as `dispatch_reclaimed`, which `events_audit` counts as closing its `stage_dispatched`).
Same-owner ONLY: an unclaimed run (the per-task CLI supervisor holds live leases exactly that
way), a live driver, or a foreign-host claim reclaims nothing, because stealing a live lease
would double-dispatch the stage. In that case the loop no longer exits looking finished — it
returns `scheduler.exit_reason = blocked_on_orphaned_dispatches` (warning event +
notification, and `run-headless` exits non-zero), and the escape hatch is still the terminal
`orchestrator abandon`. `watch --activity` distinguishes a stalled stream from a dead driver.

## Live runs against an external product repo (HARD CHECKPOINT)
A live run against a repo other than this one writes to that repo and opens a PR there.
**The human picks the specific issue and approves the run before any write or PR.** Do not
select the task or open a PR autonomously. This is the human half of the engine's approval
gate — the engine parks; humans release.

**Experimental-repo exception.** A repo listed below has been explicitly designated
experimental by Craig, and batches against it may run without per-issue approval: once
Craig says "run the batch", task selection within that batch and the PRs it opens need no
further sign-off. The designation is granted only by adding the repo to this list (a
committed edit — never inferred from conversation, a repo's name, or its newness), and the
other norms still hold there: merges stay human, and issue *filing* still shows the plan
first (spec-intake/brainstorm's own gates).

Experimental repos: *(none yet)*

## Project adapters
`adapters/project/selfhost` is the only in-repo adapter: this repo driving its own GitHub
issues, and the reference implementation of `orchestrator/ports/project.py`. An external
project's adapter belongs in **its own repo** under `<repo>/.orchestration/`
(`orchestrator-scaffold --into <repo>`), loaded by path and contract-version-checked — not
added here.

## Engine language
Python (uv, pytest, ruff). Reasoning in `docs/orchestration-template-plan.md` §0.
