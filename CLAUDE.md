# Orchestration Template — Build Workspace

Deliberate rebuild of an orchestration harness: extracted from an existing
bash system and rebuilt in Python. See `docs/` for the design doc, the
implementation plan, and the spec-in-progress.

## Current phase
Phase 1 — ground-truth extraction.
See `docs/orchestration-template-plan.md` §1 and `docs/orchestration-spec/README.md`.

## Reference system (read-only — read in place, do NOT copy in)
The system being spec'd lives in another repo:
`/Users/craigperler/Development/heysoo/.claude/`
  - scripts:        `.claude/scripts/*.sh`
  - shared engine:  `.claude/scripts/lib/orchestrator-common.sh`
  - schemas:        `.claude/scripts/schemas/*.json`
This is a rebuild, not a port — do not fork the bash into this repo.

## Engine language
Python (uv, pytest). Reasoning in plan §0.
