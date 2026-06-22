# Rebuild Retrospective (Phases 1–5)

> Folds the build learnings back into the template. Companion to `as-built.md`
> (the extracted bash system), `target.md` (the design), and `../../DEFERRED.md`.

## The result

The orchestration system was rebuilt from a ~17k-line bash harness into a
deterministic Python engine + pluggable adapters, and **proven general**: a
structurally-different second project (this repo — a pure Python/uv/pytest/ruff
library with no frontend/lambda/E2E) is driven by the **same engine with zero
engine changes** — only a new `adapters/project/selfhost` adapter and a different
(local-file) task source (`tests/test_phase5.py`). The bootstrap (`orchestrator-scaffold`)
generates a working adapter skeleton for any new repo.

Live proof on a real product repo: one Hey Soo! issue (3a) and a 3-task concurrent
batch (3b) were driven end-to-end through the interactive lane to real PRs
(#556–#559), every model call attributed (`lane_audit.clean`), no double-execution.

## What held up (the load-bearing design calls)

- **Engine never calls a model.** It emits `WorkItem`s and ingests `StageResult`s;
  runners are the only model-calling layer. This made "every call recorded on its
  attributed lane" a *structural* property (closes as-built D6), and let the same
  engine serve interactive, headless, and codex lanes.
- **The contract-first seam** (`WorkItem`/`StageResult`, judge-panel pick in Phase 2)
  delivered interchangeable modes + resumability by construction. The Phase 4
  conformance test (interactive ≡ headless trajectory) is the payoff.
- **Normalized status schema** with `started_at` always present made the crash-marker
  unambiguous and resume clean.
- **JSON-first, render later.** Structured `StageResult`s are the contract; Markdown
  (`cost-summary.md`, per-stage `.md`, index) is a thin render layer — better than the
  as-built's scraping markdown out of logs.

## What the code review caught (don't ship a foundation unreviewed)

The high-effort review *before* 3b found real bugs the live run had masked (the
supervisor path sidestepped them): capacity jitter exceeding its cap, replayed-result
acceptance, attempt-reset-on-crash, an unlocked status-store lost-update race, the
**transitive cascade being dead code in the engine**, broken multi-task finalization,
and the issue body dropped from prompts. Lesson: a green live run ≠ a correct engine;
review the foundation at the phase boundary, before building on it.

## Banked Phase-2 fixes that landed

Collapsed 6-stage map (from the real ~12–13); single model/pricing table (current
values, fixing the stale $15/$75 + `opus-4-7` pins); transitive cascade (fix D14);
codex success = **full schema validation** (fix §2 #5, not required-keys-present);
orthogonal `execution_mode × provider` lane axes; `FAILURE_CLASSIFIER` + GitHub-Issues
task source as build-fresh adapters; the `phpdoc-writer` bug → a generic docstring agent.

## Still deferred (see `../../DEFERRED.md`)

Real `_migrate` v0→v1 (no v0 files exist yet); retrospective *auto*-generation;
queue-file / unattended (cron) mode; codex routing hardening beyond the validation fix;
**bundling project JSON schemas** so codex full-validation has a schema in real runs
(injected in tests today); the BATS-corpus → pytest bulk port.

## Process notes for the next adapter author

- Standing up a new project = `orchestrator-scaffold --name <p>` then the
  Keep/Modify/Replace/Delete audit (`run_targets/adapter_bootstrap_skill.md`). If you
  edit anything under `orchestrator/`, a project concern leaked into the engine — fix
  the adapter instead.
- Gate discipline paid off: stop at each phase boundary for sign-off; live runs that
  write to a real repo need human-picked tasks + explicit approval.
