# Scope ledger → GitHub issues

**The deferred-scope ledger is now the issue tracker:**
<https://github.com/cperler/orchestration-template/issues>

The discipline is unchanged, only the medium moved:

- **Nothing is silently dropped.** Anything cut, thinned, or found-missing gets an issue,
  labeled **`deferred-scope`**, with the same fields the old rows carried: source, why
  deferred, and a *trigger to revisit*.
- **Gate reviews** = sweep every open `deferred-scope` issue and re-disposition it —
  promote (pick it up), keep (comment why), or close with a written reason (never delete).
- **Labels:** `deferred-scope` (cut/thinned scope) · `roadmap` (candidate ideas — promote
  an item to its own issue when picked up) · `bug` · `friction` (harness pain hit during
  real use, often filed from another project) · `dx`.
- **Build narrative** lives in commits, PRs, and issue comments — not in a ledger file.
- **Dogfood loop:** the selfhost adapter (`adapters/project/selfhost`) defaults to this
  tracker as its task source, so an open issue is directly orchestratable:
  `uv run orchestrator … --project adapters.project.selfhost add-task --task "#12"`.

The pre-migration ledger (gate-review log, Retired table with reasons, and the Active
rows as they stood when migrated to issues #1–#22 on 2026-07-01) is frozen at
[`docs/deferred-history.md`](docs/deferred-history.md).
