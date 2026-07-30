# Orchestration Template — Build Workspace

Deliberate rebuild of an orchestration harness: extracted from an existing bash
system (Hey Soo!'s `.claude/`) and rebuilt in Python around an engine/adapter split.
See `README.md` for the project overview and `docs/` for the design doc, the
implementation plan, and the as-built/target spec.

## Status
**Built.** Phases 1–5 complete (per-task engine → batch scheduler → second execution
mode + codex → dogfood/generalize) plus two engine-hardening passes, a workflow
code-review pass, and the 2026-07-01 review→execute cycle (context plane, per-task
pipelines schema v2, session continuity, checkpoints, approval gate — see
`docs/reviews/`). All pytest cases green, ruff clean; live-proven on real heysoo
issues. Ongoing work is incremental — pick from the issue tracker or fix-forward.

## Working norms
- **Tests + lint + types, every change:** `uv run pytest`, `uv run ruff check .`, and
  `uv run mypy` must stay green (the same trio CI enforces — `.github/workflows/ci.yml`).
  Add a regression test with each fix.
- **GitHub issues are the scope ledger** (`gh issue list -R cperler/orchestration-template`).
  Nothing is silently dropped: anything cut, thinned, or found-missing gets an issue labeled
  `deferred-scope` (with source / why / trigger-to-revisit), re-dispositioned at each gate
  (promote / keep-with-comment / close-with-reason). File or close issues as you build or
  defer. `DEFERRED.md` documents the discipline; the pre-2026-07-01 ledger is frozen at
  `docs/deferred-history.md`.
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
- **Commits** end with a `Co-Authored-By` trailer naming the authoring model (e.g.
  `Claude Opus 4.8 (1M context)`, `Claude Fable 5`) — attribution is accurate, not fixed. On a
  RUN-produced commit the engine settles this, not the model: `render_prompt` gives every
  committing stage the exact trailer lines built from the dispatched model ids
  (`stages.commit_trailers` + `model_table.attribution_identity`), crediting the implement
  model and the committing stage's own — because a model cannot reliably report its own
  version and #317 caught two commits signed with one no stage of the run dispatched. Work on
  `main`. Remote: `github.com/cperler/orchestration-template` (private; push `main`
  after committing).
- **Run logs are retained until the human deletes them.** Post-run cleanup removes the
  worktree, the task branch, and checkpoint tags — but NEVER the run's log dir under
  `runs/<run>/` (status/events.jsonl/stage-costs.jsonl/per-stage `stages/`/cost-summary).
  Those are the durable audit trail (`runs/` is gitignored — local, not committed). Do not
  `rm -rf runs/...` as part of cleanup; leave it for the human to prune explicitly.
- **The post-merge trunk gate is report-and-file, not merge orchestration** (#229, #216
  Option 2 half (b)). `Engine.trunk_gate(run_id, *, cwd, file_fix=True)` shells the project
  adapter's declared verification commands over an already-merged trunk checkout (only
  adapter argv — never a hardcoded pytest/ruff/mypy — so the engine stays project-agnostic
  and model-free) and, on red, best-effort files ONE `deferred-scope` remediation task,
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
Use the **`orchestrate-batch-headless`** skill for ordinary batches. The human runs one
foreground `run-headless` driver; the engine's `Scheduler` supervises and spawns `claude -p`
per stage, so no stage prompt or output passes through the session. Measured on
`batch-headless-1`: 92–96% cache hits on long stages (provider sessions chain via `--resume`),
real per-stage dollars in `stage-costs.jsonl`, `lane_audit` clean.

Reach for `orchestrate-batch-interactive` only when a human needs to watch each stage live.
That lane runs every stage through the session context AND records `$0.00`/zero tokens —
the Workflow shim cannot report usage, so all 15 pre-2026-07-30 interactive runs are
financially invisible. Cost-shaping inputs are also only partly captured in the timeline
(#314: dispatched prompts unpersisted, `session_ref` absent from `stage_dispatched`).

The driver owns the run for its whole duration: Ctrl-C kills the `claude -p` children via the
process group, and the scheduler does not currently recover from the orphaned leases that
leaves (#313). Monitor from a second terminal.

## Live runs against the product repo (HARD CHECKPOINT)
A live run writes to the real product repo (heysoo) and opens a PR. **The human picks the
specific (small, low-risk) issue and approves the run before any write or PR.** Do not
select the task or open a PR autonomously. heysoo PRs from these runs are for testing.

## Reference system (read-only — read in place, do NOT copy in)
The system being spec'd lives in another repo: `/Users/craigperler/Development/heysoo/.claude/`
  - scripts:        `.claude/scripts/*.sh`
  - shared engine:  `.claude/scripts/lib/orchestrator-common.sh`
  - schemas:        `.claude/scripts/schemas/*.json`
  - real run logs:  `/Users/craigperler/Development/heysoo/logs/implement-roadmap-task/`
This is a rebuild, not a port — do not fork the bash into this repo. The `docs/orchestration-spec/`
as-built fragments/sections describe THIS reference system (faithful extraction), not our code.

## Engine language
Python (uv, pytest, ruff). Reasoning in `docs/orchestration-template-plan.md` §0.
