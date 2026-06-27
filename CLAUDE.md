# Orchestration Template — Build Workspace

Deliberate rebuild of an orchestration harness: extracted from an existing bash
system (Hey Soo!'s `.claude/`) and rebuilt in Python around an engine/adapter split.
See `README.md` for the project overview and `docs/` for the design doc, the
implementation plan, and the as-built/target spec.

## Status
**Built.** Phases 1–5 complete (per-task engine → batch scheduler → second execution
mode + codex → dogfood/generalize) plus two engine-hardening passes and a workflow
code-review pass. 146 pytest cases green, ruff clean; live-proven on real heysoo issues.
Ongoing work is incremental — pick from `DEFERRED.md` or fix-forward.

## Working norms
- **Tests + lint, every change:** `uv run pytest` (146 cases) and `uv run ruff check .`
  must stay green. Add a regression test with each fix.
- **`DEFERRED.md` is the scope ledger.** Nothing is silently dropped: anything cut,
  thinned, or found-missing gets a row, re-dispositioned at each gate (promote / keep /
  retire-with-reason). Update it when you build or defer something.
- **The engine never calls a model and stays project-agnostic.** New projects plug in via
  `adapters/project/<name>/`; new execution lanes via `adapters/execution/`. Don't add
  project-specific logic to `orchestrator/`.
- **Commits** end with the `Co-Authored-By: Claude Opus 4.8 (1M context)` trailer. Work on
  `main`; the `phase-3a-engine` branch is kept fast-forwarded to `main` (no git remote).

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
