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
- **Tests + lint, every change:** `uv run pytest` (835 cases) and `uv run ruff check .`
  must stay green. Add a regression test with each fix.
- **GitHub issues are the scope ledger** (`gh issue list -R cperler/orchestration-template`).
  Nothing is silently dropped: anything cut, thinned, or found-missing gets an issue labeled
  `deferred-scope` (with source / why / trigger-to-revisit), re-dispositioned at each gate
  (promote / keep-with-comment / close-with-reason). File or close issues as you build or
  defer. `DEFERRED.md` documents the discipline; the pre-2026-07-01 ledger is frozen at
  `docs/deferred-history.md`.
- **The engine never calls a model and stays project-agnostic.** New projects plug in via
  a project-owned `<repo>/.orchestration/` adapter (loaded by path, contract-checked) or
  `adapters/project/<name>/` for in-repo reference adapters; new execution lanes via
  `adapters/execution/`. Don't add project-specific logic to `orchestrator/`.
- **Commits** end with the authoring model's own `Co-Authored-By` trailer (e.g.
  `Claude Opus 4.8 (1M context)`, `Claude Fable 5`) — attribution is accurate, not fixed. Work on
  `main`. Remote: `github.com/cperler/orchestration-template` (private; push `main`
  after committing).
- **Run logs are retained until the human deletes them.** Post-run cleanup removes the
  worktree, the task branch, and checkpoint tags — but NEVER the run's log dir under
  `runs/<run>/` (status/events.jsonl/stage-costs.jsonl/per-stage `stages/`/cost-summary).
  Those are the durable audit trail (`runs/` is gitignored — local, not committed). Do not
  `rm -rf runs/...` as part of cleanup; leave it for the human to prune explicitly.

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
