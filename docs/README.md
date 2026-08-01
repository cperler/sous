# docs/ — the frozen build record

**Nothing in this directory describes the current code.** For that, read:

| What you want | Where |
| --- | --- |
| A contributor's map of the system as it is | `../ARCHITECTURE.md` |
| How to install it, run it, and point it at a project | `../README.md` |
| Working norms for changing it | `../CLAUDE.md` |
| Open/deferred scope | [GitHub issues](https://github.com/cperler/orchestration-template/issues) (`../DEFERRED.md` for the discipline) |

What's kept here is the historical record of *how the system came to be* — the design
decisions, the phased plan, and the review passes that shaped it. It is retained because
the reasoning behind a decision outlives the decision, not because it is current.

- `orchestration-template.md` (2026-06-11) — the original design notes: the assessment of
  the bash orchestration system this was rebuilt from, the June 2026 billing-change
  analysis, and the extraction strategy.
- `orchestration-template-plan.md` (2026-06-12) — the phased implementation plan
  (Phases 1–5, all complete) and the §0 decision log, including why the engine is Python.
- `reviews/` — the design-pass and audit documents behind specific subsystems: the context
  plane, meta-authoring, the review workflow, roadmap reconciliation, deferred-scope gate
  reviews, and the run-level settings-persistence audit (the one document `CLAUDE.md` still
  cites as a live pattern reference).
- `deferred-history.md` — the pre-2026-07-01 deferred-scope ledger, frozen at the point it
  migrated to GitHub issues.

## A note on dangling references

These documents were written alongside `docs/orchestration-spec/` — a faithful extraction
of the *reference* bash system (another project's `.claude/`), plus the implementation-agnostic
target design derived from it. That extraction was **deleted on 2026-08-01**: the reference
project is no longer worked on, the extraction described its code rather than ours, and the
target design has been superseded by the built system that `ARCHITECTURE.md` documents. Links
to `docs/orchestration-spec/…` in the files above are therefore dead by design; they are left
in place rather than rewritten, since editing a frozen record to hide what it referenced makes
it a worse record. `git log -- docs/orchestration-spec` recovers it if ever needed.
