# Orchestration spec — build artifacts

Produced in phase order (see `../orchestration-template-plan.md`).

## Phase 1 — ground-truth extraction (current)
Fan out one mapping agent per source unit in the reference system
(`/Users/craigperler/Development/heysoo/.claude/`), each filling
`fragment-template.md` for its file(s), every claim in §§4–7 citing
`absolute-path:line`. An adversarial verifier refutes each fragment; a
synthesis agent merges them into `as-built.md`. Full instructions: plan §1.

- Fragments land in `fragments/`.
- Synthesized spec is `as-built.md`.

## Phase 2 — target spec
`target.md` (not yet created): the implementation-agnostic design with the
engine/adapter split and banked fixes. Seeds `../../DEFERRED.md`.

## Starting Phase 1
Open a fresh session with THIS repo as cwd, then follow plan §1. The fan-out is
file-targeted — every agent receives an absolute path into the reference system,
so cwd being this (empty) repo is fine.
