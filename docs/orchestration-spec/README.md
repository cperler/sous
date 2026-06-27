# Orchestration spec — build artifacts

The Phase 1–2 artifacts of the rebuild (see `../orchestration-template-plan.md`).
**Both phases are complete** and the engine was built from them; the rebuild is done
(see the repo `README.md` and `../../DEFERRED.md` for current status). These remain as
the design record — they are not living docs of the running code.

## Phase 1 — ground-truth extraction (complete)
One mapping agent per source unit in the **reference** system
(`/Users/craigperler/Development/heysoo/.claude/`) filled `fragment-template.md` for its
file(s), every §§4–7 claim citing `absolute-path:line`; an adversarial verifier refuted
each; a synthesis agent merged them.

- Fragments: `fragments/` — per-source extractions.
- Consolidated sections: `sections/` — the merged, topic-organized spec.
- Synthesized spec: `as-built.md`.

These describe the **read-only reference bash system**, not this repo's Python code — they
are a faithful extraction and stay accurate as long as the reference doesn't change.

## Phase 2 — target spec (complete)
`target.md`: the implementation-agnostic design the rebuild was built from — the
engine/adapter split, the orthogonal lane axes, the collapsed 6-stage map, the versioned
status schema, and the banked fixes. The traceability table (§2) dispositions every
as-built behavior; `defer` rows seeded `../../DEFERRED.md`. Where the build deviated from
the target, the delta lives in `../../DEFERRED.md` + git history, not here.

## Phase 5 retrospective
`retrospective.md` — the dogfood/generalize retrospective.
