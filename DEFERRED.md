# Deferred-Scope Ledger

The MVP-first condition: nothing cut is silently dropped. One row per deferred item.
Seeded in Phase 2 from every `defer` row in the `target.md` §2 traceability table (the
single funnel — nothing reaches "deferred" without a row here). **Reviewed at every phase
gate** alongside the lane-attribution audit: each row is re-dispositioned — promote into the
next phase, keep deferred, or retire (move to "Retired" with a written reason; never delete).

| Item | Source (as-built §) | Why deferred | Earliest phase | Trigger to revisit |
|---|---|---|---|---|
| Queue-file ingestion / unattended (cron) batch mode | scheduler §1.8 (`ralph-queue.json`) | Unattended launch is the credit/headless lane, not the interactive MVP; the design centers attended runs | Phase 4 | When the headless execution lane is built (the unattended slice) |
| Non-wired skill disposition (9: brainstorming, executing-plans, investigating-codebase, review-ui, systematic-debugging, using-git-worktrees, write-docblocks, writing-agents, writing-skills) | fragment 12 / config-agents-skills | Not pipeline-wired; methodology-port / product-specific / stale — keep/refine/drop is a Phase-2-deferred decision | Phase 3 | When porting the supervisor skill set + the `adapting-claude-pipeline` bootstrap |
| In-flight `brainstorm_*` feature (#505) | runtime §orphan | Present in schema + prompt but never written in real output; not landed at the reference HEAD | Phase 3+ | If/when a brainstorm stage is actually wanted in the collapsed graph |
| BATS test corpus bulk (~550+ cases beyond core engine logic) | fragment 14 | Engine-logic cases port to pytest in 3a; product/integration/E2E cases need their module/adapter first | Phase 3a → 5 | When the corresponding engine module or project adapter is built |
| Capacity-throttle jitter tuning | capacity policy (OC:2077) | Port the clamp+jitter mechanism now; tune the jitter window after real data | Phase 5 | After a real multi-task batch yields throughput/cost data |
| Codex routing hardening (per-task tag tuning, `CODEX_ELIGIBLE_STAGES`, conformance beyond the §4 full-validation fix) | execution adapter / ADR-062 | Codex is a Phase-4 second-provider deliverable | Phase 4 | When the codex runner is implemented |
| Retrospective auto-generation (`emit_failure_retrospective` + `detect_failure_patterns`) | 2b (OC:814/870) | Observability nicety, not core to a task landing a PR | Phase 5 | After MVP; when failure analytics are wanted |
| Rich per-stage cost reports (beyond the basic ledger + summary) | cost ledger (OC:2704) | Basic ledger + summary ship in MVP; richer reporting is additive | Phase 5 | When cost analysis / the session-reuse-win comparison is run |
| Port-registry parallel-worktree concurrency for the headless lane | helpers (`port-registry.sh`) | Interactive mode uses in-session workflow concurrency; OS-process port allocation is only needed for parallel headless worktrees | Phase 4 | When parallel headless runs land |
| Monitor poll/render/liveness dashboard surplus | monitor D5 (~60–70%) | Made free by in-session `/workflows` on the default lane; only the unattended/observability slice was ported | Phase 4 | If a headless/cross-session run needs a standalone dashboard |

## Retired
*(none yet — items move here with a written reason rather than being deleted)*
