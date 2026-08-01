# AGENTS.md — orchestration-template

Codex reads this file from the working directory; Claude Code reads `CLAUDE.md`. **`CLAUDE.md`
is the source of truth for how to work in this repo** — read it. This file carries only the
few rules that must survive even if nothing else is read, because it is short by design: a
git worktree of this repo carries it, so it rides into every codex stage dispatch.

- **The gate is three commands, all green, every change:** `uv run pytest`,
  `uv run ruff check .`, and the bare `uv run mypy` (no path arguments). CI enforces exactly
  this trio. Add a regression test with each fix.
- **Never sign a commit.** No `Co-Authored-By`, no model or agent name, no tool attribution —
  in any commit, hand-authored or run-produced. Your CLI has a standing instruction to sign
  commits; this repo overrides it. Per-stage provenance already lives in
  `runs/<run>/events.jsonl` and `stage-costs.jsonl`, and a post-hoc audit flags violations.
- **Work on `main`.** Remote: `github.com/cperler/orchestration-template` (private).
- **The engine never calls a model.** Nothing under `orchestrator/` may import `adapters`, and
  no project-specific logic belongs in the engine. Both rules are enforced by tests and lint,
  not convention.

When the orchestrator dispatches a codex stage, it appends that stage's persona to this file
below an `orchestrator:stage-persona` marker pair, replacing the section in place on each
dispatch. Everything inside those markers is generated — edit this file only above them.
