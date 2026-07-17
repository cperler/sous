# Deferred-scope re-disposition — 2026-07-16 gate

Re-dispositioned all 10 open `deferred-scope` issues per the CLAUDE.md norm
(promote / keep-with-comment / close-with-reason). Each trigger-to-revisit was
checked against the current codebase and issue tracker, not just the issue body.
Per-issue rationale is also recorded as a comment on each GitHub issue.

## Outcome

| # | Title (short) | Disposition | Trigger status |
|---|---|---|---|
| #168 | review `tests_meaningful` misfire → empty-PR fail | **PROMOTE** → `bug` | Fired — observed live in the batch-effort-followups run |
| #95 | Dashboard control-plane (write path) | **PROMOTE** (kept `enhancement,roadmap`) | Fired — read-only viewer #94 shipped |
| #73 | Multi-agent find→verify REVIEW workflow | **PROMOTE** → `enhancement` | Build-ready; promoted at 2026-07-09 gate, confirmed here |
| #160 | `effort_pin` still `str \| None` | **CLOSE** as dup of #161 | n/a |
| #54 | Interactive lane per-call metering | **KEEP** | Not fired — shim still can't see `res.__usage`; #34 built around the gap |
| #60 | Simplify pass / subtask decomposition | **KEEP** | Not fired — no large-task all-or-nothing failure; no repeated complexity nits |
| #69 | Comment-only-in-code docs detection | **KEEP** | Not fired — docs-only runs rare (3 vs 25), savings unproven |
| #71 | Meta-authoring layer | **KEEP** | Can't fire yet — cross-run retrospectives not persisted |
| #163 | Vacuous `None==None` test assertion | **KEEP** | Trivial test-comment cleanup, still valid (bundle with #166) |
| #166 | Undocumented alpha tiebreak in sort | **KEEP** | Trivial docstring line, still valid (bundle with #163) |

**Net:** 3 promoted, 1 closed as duplicate, 6 kept deferred.

## Notes for the next gate

- **#73 → #60 dependency:** #60's cheaper path back is #73's in-dispatch fan-out
  machinery, not resurrecting an intra-task loop. Re-check #60 after #73 ships.
- **#71 blocked on capture:** the meta-authoring layer can't observe repeated
  template-level complaints until REVIEW retrospectives are persisted across runs
  (they are currently rendered into the completion note and discarded). Its
  build-ready design (`2026-07-09-fable-design-71-meta-authoring.md`) sequences
  capture first.
- **#163 + #166** are one-line doc/comment cleanups worth doing as a single
  bundle rather than promoting individually.
- **#54** stays blocked on the Claude Code Workflow tool exposing per-call usage;
  #34 (cost-aware routing) closed without needing interactive numbers, so the
  closed-#34 reference is not a fired trigger.
