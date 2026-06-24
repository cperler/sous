# Deferred-Scope Ledger

The MVP-first condition: nothing cut is silently dropped. One row per deferred item.
Seeded in Phase 2 from every `defer` row in the `target.md` §2 traceability table (the
single funnel — nothing reaches "deferred" without a row here). **Reviewed at every phase
gate** alongside the lane-attribution audit: each row is re-dispositioned — promote into the
next phase, keep deferred, or retire (move to "Retired" with a written reason; never delete).

## Gate reviews
- **2026-06-23 — post-Phase-5 gate.** Phases 1–5 + two engine-hardening passes are built
  (123 tests green). Re-dispositioned all rows below. Outcome: **2 retired** (codex routing
  hardening — built in Phase 4 + the review-fix schema-validation wiring; non-wired skill
  disposition — decided), **1 promoted** (queue-file / unattended mode — its "headless lane
  built" trigger has fired), **6 kept** (still genuinely unbuilt).
- **2026-06-23 — post-gate build.** Queuing was deprioritized as rarely-used in practice,
  so built kept rows instead: **rich per-stage cost reports + the session-reuse-win
  measurement** and **retrospective auto-generation** (both retired below). 4 kept rows remain.
- **2026-06-24 — log-corpus audit.** Sampled 33 of 61 real bash-orchestrator run logs
  (4 parallel readers) against the rebuild. Confirmed the test-validate "verify" half was
  thinned → **built it as an engine gate** (commit `478ce7d`). Found 5 further code-verified
  gaps (added to Active above): graceful fallback wiring (dead code), capacity-aware
  downgrade, committed-work timeout recovery, infra-failure reset, review-loop convergence.
  Over-flagged by the readers but confirmed already-present (no action): lite/micro lanes,
  transitive cascade, capacity throttle, baseline capture, cost JSONL, codex-eligible stages.
- **2026-06-24 — post-audit build.** Built the audit's top finding: **graceful model fallback**
  (capacity-aware downgrade + rate-limit re-dispatch on a cheaper model), closing the dead
  MODEL_CHAIN/allow_fallback wiring (commit `5f67a1f`, retired below). The cross-provider
  codex→claude fallthrough slice stays Active. 4 audit rows remain (provider fallthrough,
  committed-work recovery, infra-failure reset, review convergence).

## Active

| Item | Source (as-built §) | Why deferred | Status (2026-06-23 gate) | Trigger to revisit |
|---|---|---|---|---|
| **Queue-file ingestion / unattended (cron) batch mode** | scheduler §1.8 (`ralph-queue.json`) | Unattended launch is the credit/headless lane, not the interactive MVP; the design centers attended runs | **PROMOTED → actionable.** The headless lane (`headless_claude` + `codex` runners, in-process `registry_runner`) is built, so the trigger has fired. The scheduler is already resumable/idempotent; what's missing is a queue-file ingestion entrypoint + a cron/launch wrapper that feeds task ids into `Scheduler.run`. | Now — top build candidate |
| In-flight `brainstorm_*` feature (#505) | runtime §orphan | Present in schema + prompt but never written in real output; not landed at the reference HEAD | **KEEP.** Still absent from the reference HEAD; no collapsed-graph stage wants it yet. | If/when a brainstorm stage is actually wanted in the collapsed graph |
| BATS test corpus bulk (~550+ cases beyond core engine logic) | fragment 14 | Engine-logic cases port to pytest in 3a; product/integration/E2E cases need their module/adapter first | **KEEP (partial).** Engine-logic cases are covered (123 pytest cases); the product/integration/E2E corpus still needs the corresponding adapter before porting. | When the corresponding engine module or project adapter is built |
| Capacity-throttle jitter tuning | capacity policy (OC:2077) | Port the clamp+jitter mechanism now; tune the jitter window after real data | **KEEP.** Mechanism shipped; the live batch (heysoo PRs #556–559) is too small to tune the jitter window. | After a larger real multi-task batch yields throughput/cost data |
| Port-registry parallel-worktree concurrency for the headless lane | helpers (`port-registry.sh`) | Interactive mode uses in-session workflow concurrency; OS-process port allocation is only needed for parallel headless worktrees | **KEEP (partial).** `registry_runner` does in-process `ThreadPoolExecutor` parallel dispatch, but OS-process port allocation + per-dispatch worktree isolation for parallel headless runs is unbuilt. | When parallel headless worktree runs land (pairs with the promoted unattended mode) |
| Monitor poll/render/liveness dashboard surplus | monitor D5 (~60–70%) | Made free by in-session `/workflows` on the default lane; only the unattended/observability slice was ported | **KEEP.** `events.jsonl` timeline + markdown renderers cover the in-session need; a standalone cross-session dashboard is unbuilt. | If a headless/cross-session run needs a standalone dashboard (pairs with unattended mode) |
| Codex→claude **provider** fallthrough (cross-provider) | log-audit 2026-06-24 (routing) | The model-chain + rate-limit fallback are now wired (retired below), but a codex error/rate-limit degrades to a normal failure rather than re-routing to the claude provider — codex models aren't in the (claude) MODEL_CHAIN. | **KEEP (the remaining slice).** Cross-provider fallthrough is a bigger change (new provider chain + lane swap); the within-provider model fallback covers the common case. | When codex is a primary route and its reliability matters |
| **Timeout/crash recovery via committed-work detection** | log-audit 2026-06-24 (resume) | Old runs checked git for committed work on a timed-out stage and reclassified `timed_out → completed`; we blindly re-dispatch on resume (risk: redo/duplicate already-committed work). Heysoo #548 is a live bug in their version. | **NEW (substance).** Correctness + wasted-work gap on the resume path; somewhat adapter-coupled (needs git inspection). | When resume robustness matters / a real run hits a stage timeout |
| **Infra-failure classification + reset loop** | log-audit 2026-06-24 (test loop) | Old runs distinguished "test runner broke" (exit code ≠ parsed failures) from real failures, counted consecutive infra failures, and ran `infra_reset` after N. We have the `infra_reset()` command but no wiring; a broken runner reads as a normal test failure. | **NEW (substance, medium).** Prevents false-fail death spirals. The classifier interface can express it. | When a real run hits flaky infra / false-fail spirals |
| Review-loop convergence auto-approval (net-new-issue detection) | log-audit 2026-06-24 (review loop) | Old runs iterated review and auto-approved once remaining issues were all net-new (prior fixed). | **NEW — bundle with the known-thinned iterative review/quality loop.** Only meaningful once that loop is restored; the convergence math is the valuable half. | If/when the iterative review+simplify loop is restored |

## Retired

| Item | Source | Retired (date) | Reason |
|---|---|---|---|
| Graceful fallback wiring (rate-limit → model chain) + capacity-aware model downgrade | log-audit 2026-06-24 | 2026-06-24 | **Built (commit `5f67a1f`).** `engine._dispatch_model` consumes the previously-dead `MODEL_CHAIN`/`fallback_after`/`allow_fallback`: at capacity it degrades to `ModelTable.cheapest()` instead of raising; a `ResultStatus.RATE_LIMITED` result re-queues the same stage+attempt on the next cheaper model (no retry burned, breaker untouched) via `Task.pending_fallback_model`, degrading to a normal failure at the floor. `transport.is_rate_limited()` lets the headless+codex runners classify 429/overload/usage-limit errors so the fallback fires on a live run. (Cross-provider codex→claude fallthrough kept Active above.) |
| Codex routing hardening (per-task tag tuning, `CODEX_ELIGIBLE_STAGES`, conformance beyond the §4 full-validation fix) | execution adapter / ADR-062 | 2026-06-23 | **Built in Phase 4 + the review-fix wiring.** `routing.py` carries `codex_eligible_stages` (default `{implement, test}`) and the per-task `:codex` `provider_tag`; `codex.py` enforces full JSON-Schema validation (`jsonschema` Draft202012Validator), wired through `build_registry(codex_schema_provider=…)` from the project's `schema_for`. Any further conformance tuning is a normal change, not deferred scope. |
| Non-wired skill disposition (9: brainstorming, executing-plans, investigating-codebase, review-ui, systematic-debugging, using-git-worktrees, write-docblocks, writing-agents, writing-skills) | fragment 12 / config-agents-skills | 2026-06-23 | **Decided.** Only the pipeline-wired run targets were ported (`supervisor_skill`, `scheduler_skill`, `adapter_bootstrap_skill`). The 9 methodology/product-specific skills were dropped from the harness; they remain available as project-config drop-ins for an adapter that wants them, so nothing is lost. |
| Rich per-stage cost reports + session-reuse-win measurement | cost ledger (OC:2704) | 2026-06-23 | **Built.** `CostLedger.analysis()` (per-stage/-task breakdown + the cache-read-savings-net-of-write-premium session-reuse win vs. an uncached counterfactual), `render_cost_report()` → `cost-report.md` written on `status()`/finalize, and the `cost-report` CLI subcommand. Validates the collapsed-stage cost thesis from the data every ledger row already carries. |
| Retrospective auto-generation (`emit_failure_retrospective` + `detect_failure_patterns`) | 2b (OC:814/870) | 2026-06-23 | **Built.** `orchestrator/retrospective.py` (`detect_failure_patterns` recomputes the circuit-breaker error signature over the per-stage logs → within-task-plateau / cross-task grouping; `build_retrospective` assembles the per-failed-task trail + cascade map), `render_retrospective()` → `retrospective.md` auto-written at finalize only when the run failed, and the `retrospective` CLI subcommand. |
