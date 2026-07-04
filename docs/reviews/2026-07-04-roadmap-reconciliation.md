# 2026-07-04 Roadmap / Issue Reconciliation

Read-only reconciliation after the 2026-07-04 burn-down (`da77f66..ed59df2`, tests 361→725).
Every classification below is verified against the current `main` tree (file:line) or the
closing commit sha. Conservative bias: where a bullet is only partially demonstrable, it is
marked PARTIAL, not BUILT. **Nothing in this document has been enacted** — the closing/
comment commands are staged in the final section for a human to run.

Evidence base: 715 `def test`s across `tests/`, `orchestrator/cli.py` subcommand table,
`orchestrator/cost_policy.py`, `orchestrator/routing.py`, `orchestrator/engine.py`
evidence-out seam, `orchestrator/scheduler.py`, `adapters/execution/transport.py`, and the
closing commits listed in `git log da77f66..ed59df2`.

---

## 1. Roadmap issues

### #17 — Roadmap A: close the self-improvement loops

| Bullet | Status | Evidence |
|---|---|---|
| Follow-up task creation | **BUILT** | `engine.py:2124` `_file_review_followups` files each non-blocking review finding as a `deferred-scope` follow-up issue (`file_followup`); wired at `engine.py:2108`. Live proof: the auto-filed issues #69/#70 footed "_Filed automatically…_". |
| Reflection capture, loop closed | **BUILT** | `engine.py:2166` `_file_review_improvement` files the review's forward-looking improvement as an `enhancement` issue; review.json carries `improvement`+`retrospective`, rendered into the completion note (commit `5b1c684`). |
| Dogfood the harness on itself | **BUILT** | `adapters/project/selfhost/config.py` defaults the task source to this repo's tracker; the auto-filed follow-ups land here. |
| Meta-authoring layer, reconsidered | **UNBUILT** | No cc-orchestration-writer / writing-skills agent; grep for a self-editing meta-agent finds nothing in `orchestrator/`/`adapters/`. |
| Cross-run / cross-task learnings KB | **UNBUILT** | `retrospective.py`/`test_learning_enrichment.py` accumulate learnings *within a run's retries only*; no persistent cross-run knowledge store injected into later tasks. |

**Counts: 3 built / 0 partial / 2 unbuilt.**
**Disposition:** keep open with the two remaining bullets (meta-authoring layer; cross-run
learnings KB). Both are "build-fresh" and independent — recommend re-homing each to its own
issue so the umbrella can close. The success-path loop is live in production.

### #19 — Roadmap C: faster / cheaper levers

| Bullet | Status | Evidence |
|---|---|---|
| Cost-optimizing routing policy | **BUILT** | `cost_policy.py` `CostRouter` + `COST_ROUTING_BANDS` (`:81`) route thinning-budget runs to LITE/MICRO presets and prefer `$0` deterministic TEST/DELIVER runners (`:135`); capacity-aware cheap-dispatch band (`dafea7b`, #12); per-task `:codex` route (`routing.py:42`); a-priori estimate table (`cost_policy.py:34`, #34). Routes by budget-fraction + estimate nudge + mechanical-stage→`$0`, rather than a literal per-stage "difficulty" score — the lever exists. |
| Parallel worktree execution | **BUILT** | `port_registry.py` per-task port blocks (`53d77b7`, #5) + `scheduler.py` concurrent dispatch (`max_concurrent=3`, capacity-derived `dispatch_limit`, `:53`). |

**Counts: 2 built / 0 partial / 0 unbuilt.**
**Disposition:** **close as satisfied.** Both levers are built and tested. (Optional nuance
to note in the closing comment: cost routing keys off budget-fraction, not an explicit
per-stage difficulty score — if a difficulty-scored router is still wanted, file it fresh.)

### #20 — Roadmap D: under-used AI-tooling leverage

| Bullet | Status | Evidence |
|---|---|---|
| Workflows inside stages (review as find→verify; implement judge-panel) | **PARTIAL** | Review gained *additive lenses*: code-review + `review:spec` + design-review (`stages.py:180` `_DESIGN_REVIEW_LENS`, #62), convergence auto-approval (#15), independent test-validate (#13 half), separate reviewer roles (`heysoo/config.py:38-39`). But no *internal multi-agent find→verify workflow* inside a stage and no implement judge-panel — the described mechanism is not there. |
| Reasoning-effort per stage | **UNBUILT** | grep `reasoning_effort\|thinking` over `orchestrator/`+`adapters/` = 0 hits. |
| Vision for UI tasks | **UNBUILT** | grep `vision\|screenshot` = 0 hits; the design lens is text-criteria only. |
| Custom slash commands | **PARTIAL** | `/orchestrate-task-interactive`, `/orchestrate-batch-interactive`, `spec-intake`, `brainstorm`, `batch-plan` are discoverable skills (`63af857`); CLIs `watch --activity`/`tail`/`dashboard` exist. The specific `/orch-status`/`/orch-monitor` ergonomic wrappers are not built; no `monitor` subcommand (`grep -c '"monitor"' cli.py` = 0). |
| MCP servers to replace gh/git | **UNBUILT** | grep `mcp` over `orchestrator/`+`adapters/` = 0 hits; still shells `gh`/`git`. |
| ~~Prompt-cache-aware structuring~~ | **BUILT** | Already checked in body (render sections + session continuity). |

**Counts: 1 built / 2 partial / 3 unbuilt (of 6).**
**Disposition:** keep open. Recommend re-homing bullet 1 (workflows-inside-stages) to its own
issue — the comment already flags it as "the biggest unpulled quality lever" and it is the
largest remaining item. Remaining bullets: 1, 2, 3, 5, and the `/orch-status` half of 4.

### #21 — Roadmap E: codex / provider parity

| Bullet | Status | Evidence |
|---|---|---|
| ~~Provider-aware model table~~ | **BUILT** | Already checked in body. |
| Codex-native persona surface (AGENTS.md / fold persona into WorkItem prompt) | **UNBUILT** | grep `AGENTS\.md\|codex.*persona` over `orchestrator/`+`adapters/` = 0 hits. Codex-routed stages still don't receive the kit's personas. |

Additional parity work resolved off-checklist (comments): schema-validate-and-retry for codex
**done** (`c937a5e`, shared `_schema_retry_loop`), cross-provider fallthrough codex→claude
**done** (`22cc847`, #7), and the adjacent codex `cached_input_tokens`→`cache_read` mapping
**done** (`transport.py:503`, `2b50c8b`).

**Counts: 1 built / 0 partial / 1 unbuilt.**
**Disposition:** the umbrella is down to a single concrete bullet. Recommend re-home the
codex-native-persona bullet to its own focused issue and **close #21**; or, conservatively,
keep open carrying only that one bullet.

### #22 — Roadmap F: DX / observability

| Bullet | Status | Evidence |
|---|---|---|
| Terminal live monitor `orchestrator monitor <run>` | **PARTIAL** | No `monitor` subcommand. But #66 (`2f0a624`) landed `watch --activity` (live per-task activity + stream-stall), `tail`/`tail --follow` (`cli.py:168`), and stream probe; #6 (`7a4efa0`) landed cross-session `dashboard --watch` (`cli.py:252`); `cost-report` gives per-stage cost. The unified single-run live stage-tree view the bullet names is not one command. |
| `ARCHITECTURE.md` + Mermaid + reading guide | **UNBUILT** | No `ARCHITECTURE.md` anywhere (`find -iname ARCHITECTURE.md` → only `docs/deferred-history.md` mentions it); no `mermaid` under `docs/`. |

**Counts: 0 built / 1 partial / 1 unbuilt.**
**Disposition:** keep open with both bullets. Recommend re-homing bullet 2 (`ARCHITECTURE.md`,
a pure-docs, explicitly "not a top-model" task) to its own issue.

### #18 — Roadmap B: spec → software front door (not in the assigned five, reconciled for completeness)

| Bullet | Status | Evidence |
|---|---|---|
| Spec/requirements → task-graph decomposition | **BUILT** | `orchestrator spec` front door (`6f28fc1`, `cli.py:179`); `spec_intake.py` + `schemas/spec.json`. |
| Acceptance / spec-conformance gate | **UNBUILT** | Per-task `review:spec` lens exists, but no *final whole-delivery* acceptance gate; `spec.json:21` stores acceptance criteria only as body text. |
| A-priori cost/time estimation + budget | **BUILT** | `cost_policy.py` `ESTIMATE_USD` + `spec plan/file --budget-usd --strict` (`cli.py:186-200`, #34). |

**Disposition:** keep open with the single remaining bullet (acceptance/conformance gate).

---

## 2. Remaining open non-roadmap issues

| # | Title | Premise valid vs tonight's code? | Recommendation |
|---|---|---|---|
| #70 | Schema-retry sub-calls not separately stream-teed | Yes — a real limitation of `_schema_retry_loop`; filed 2026-07-04. | still-valid (fresh deferral) |
| #69 | Comment-only-in-code detection for docs-only tag | Yes — `stages.py` docs-only tag is path-based, doesn't detect comment-only code edits; filed 2026-07-04. | still-valid (fresh deferral) |
| #68 | Adopt deterministic TEST+DELIVER in micro/lite lane presets | Yes — deterministic runners exist (#33) but aren't wired into micro/lite presets; filed 2026-07-04. | still-valid (fresh deferral) |
| #61 | Statusline / always-on usage display | Yes — no statusline surface. | **Craig's-call** (do not close) |
| #60 | Simplify pass + per-subtask decomposition | Yes — no subtask DAG / quality-tier decomposition. | **Craig's-call** (do not close) |
| #54 | Interactive lane per-call metering | Yes — interactive ledger rows remain unmetered; now *stated honestly* (`ed0d010`) but not metered. | still-valid / Craig's-call (keep open) |
| #35 | Exercise the batch/DAG scheduler lane end-to-end | Partially advanced — e2e *harness* landed (`3d760e8`, `test_batch_e2e.py`), but a real human-gated live batch run is still pending. | **Craig's-call** (human-gated live batch; do not close) |
| #3 | Port the BATS test corpus bulk | Yes — 715 py tests, but the ~550 BATS cases are not ported. | **Craig's-call** (do not close) |
| #1 | Queue-file ingestion / unattended (cron) batch | Yes — no queue/cron ingestion. Note Craig's stated preference for skill-driven in-session runs over headless/cron. | **Craig's-call** (do not close) |

No open non-roadmap issue is a clean "close-as-satisfied": all are either fresh deliberate
deferrals, honestly-stated known limitations, or explicit Craig's-call items.

---

## 3. Verdict summary

**Per roadmap (built / partial / unbuilt → disposition):**

- **#17 A** — 3 / 0 / 2 → keep open (re-home meta-authoring + cross-run KB to own issues).
- **#19 C** — 2 / 0 / 0 → **close as satisfied.**
- **#20 D** — 1 / 2 / 3 → keep open (re-home workflows-inside-stages, the biggest lever).
- **#21 E** — 1 / 0 / 1 → re-home the codex-persona bullet + close, or keep open on that one item.
- **#22 F** — 0 / 1 / 1 → keep open (re-home `ARCHITECTURE.md` docs task).
- **#18 B** (adjacent) — 2 / 0 / 1 → keep open on the acceptance-gate bullet.

**Close-as-satisfied recommendations (evidence one-liners):**

- **#19 (Roadmap C)** — cost-optimizing routing (`cost_policy.py` `CostRouter`/bands +
  `$0` deterministic runners + `:codex` route) and parallel worktree execution
  (`port_registry.py` #5 + `scheduler.py` `max_concurrent`) are both built and tested.
- **#21 (Roadmap E)** — *candidate* close: only the codex-native-persona bullet remains
  (schema-retry, cross-provider fallthrough, and cache-read mapping all landed); close only
  if the persona bullet is re-homed first.

**Surprising / worth flagging:**

1. The self-improvement loop is closing on *itself*: #69 and #70 in the open list were
   auto-filed by the review evidence-out seam (`engine.py:2124`) — the #17 loop operating in
   production, not hand-filed.
2. The #21 comment's "adjacent, not on this roadmap" codex `cached_input_tokens` cache-read
   gap was itself fixed the same night (`transport.py:503`, `2b50c8b`) — the tracker closed a
   note it had only just written.
3. Roadmap D's headline lever (**workflows inside stages** — review as a real multi-agent
   find→verify workflow) is still unbuilt; the many review commits (#62/#15/#13) added
   *lenses and reviewer roles*, which reads like the item but isn't the internal workflow.
4. Roadmap B (#18) is open but nearly done — spec front door and a-priori budget both
   shipped; only the final acceptance/conformance gate remains.

---

## 4. Ready-to-run script block — NOT EXECUTED

> The commands below are staged for a human. **None have been run.** They only add comments
> and (for #19) close one issue. Review before pasting. Repo: `cperler/orchestration-template`.

```bash
# ---- #19 Roadmap C: close as satisfied -----------------------------------------
gh issue comment 19 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04 (post da77f66..ed59df2): both levers BUILT.

| Bullet | Status | Evidence |
|---|---|---|
| Cost-optimizing routing policy | BUILT | cost_policy.py CostRouter + COST_ROUTING_BANDS route thinning-budget runs to LITE/MICRO and prefer $0 deterministic TEST/DELIVER runners; capacity cheap-dispatch band (#12); :codex route (routing.py:42); a-priori estimate table (#34). |
| Parallel worktree execution | BUILT | port_registry.py per-task port blocks (#5) + scheduler.py concurrent dispatch (max_concurrent, capacity dispatch_limit). |

Nuance: routing keys off remaining-budget-fraction + estimate nudge + mechanical-stage->$0, not a literal per-stage difficulty score. If a difficulty-scored router is still wanted, file it fresh. Closing this roadmap umbrella as satisfied.'
gh issue close 19 -R cperler/orchestration-template --reason completed

# ---- #17 Roadmap A: keep open, record status + re-home the two build-fresh bullets ----
gh issue comment 17 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04: 3 BUILT / 2 UNBUILT.

| Bullet | Status | Evidence |
|---|---|---|
| Follow-up task creation | BUILT | engine.py:2124 _file_review_followups (deferred-scope follow-ups); #69/#70 are live auto-filed output. |
| Reflection capture, loop closed | BUILT | engine.py:2166 _file_review_improvement + review.json improvement/retrospective (5b1c684). |
| Dogfood on itself | BUILT | selfhost adapter defaults to this tracker; auto-filed follow-ups land here. |
| Meta-authoring layer | UNBUILT | no self-editing meta-agent. |
| Cross-run learnings KB | UNBUILT | learnings accumulate within-run only (retrospective.py); no persistent cross-run store. |

Recommend re-homing the two remaining build-fresh bullets to their own issues so this umbrella can close.'

# ---- #20 Roadmap D: record status; flag workflows-inside-stages for its own issue ----
gh issue comment 20 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04: 1 BUILT / 2 PARTIAL / 3 UNBUILT.

| Bullet | Status | Evidence |
|---|---|---|
| Workflows inside stages | PARTIAL | review gained additive lenses (code/spec/design #62, convergence #15, test-validate #13) + reviewer roles, but no internal multi-agent find->verify workflow and no implement judge-panel. |
| Reasoning-effort per stage | UNBUILT | grep reasoning_effort = 0. |
| Vision for UI tasks | UNBUILT | grep vision/screenshot = 0; design lens is text-only. |
| Custom slash commands | PARTIAL | orchestrate-task/batch + spec/brainstorm/batch-plan skills exist; watch/tail/dashboard CLIs exist; no /orch-status /orch-monitor, no monitor subcommand. |
| MCP servers for gh/git | UNBUILT | grep mcp = 0; still shells gh/git. |
| Prompt-cache-aware | BUILT | (already checked). |

Recommend re-homing "workflows inside stages" (the biggest unpulled quality lever) to its own issue.'

# ---- #21 Roadmap E: record status; only the codex-persona bullet remains ----
gh issue comment 21 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04: 1 BUILT / 1 UNBUILT — umbrella down to one bullet.

| Bullet | Status | Evidence |
|---|---|---|
| Provider-aware model table | BUILT | (already checked). |
| Codex-native persona surface (AGENTS.md) | UNBUILT | grep AGENTS.md/codex-persona = 0; codex-routed stages get no kit persona. |

Off-checklist parity all landed: codex schema-retry (c937a5e), cross-provider fallthrough (22cc847, #7), codex cached_input_tokens->cache_read (2b50c8b, transport.py:503). Recommend re-homing the codex-persona bullet to its own issue and closing this umbrella; or keep open carrying only that item.'

# ---- #22 Roadmap F: record status; re-home the ARCHITECTURE.md docs bullet ----
gh issue comment 22 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04: 0 BUILT / 1 PARTIAL / 1 UNBUILT.

| Bullet | Status | Evidence |
|---|---|---|
| Terminal live monitor (orchestrator monitor <run>) | PARTIAL | no monitor subcommand, but #66 landed watch --activity + tail --follow + stream probe and #6 landed dashboard --watch; the unified single-run live stage-tree view is not one command. |
| ARCHITECTURE.md + Mermaid + reading guide | UNBUILT | no ARCHITECTURE.md, no mermaid under docs/. |

Recommend re-homing the ARCHITECTURE.md docs task (explicitly not a top-model item) to its own issue.'

# ---- #18 Roadmap B (adjacent): record status; acceptance gate remains ----
gh issue comment 18 -R cperler/orchestration-template --body 'Reconciliation 2026-07-04: 2 BUILT / 1 UNBUILT.

| Bullet | Status | Evidence |
|---|---|---|
| Spec -> task-graph decomposition | BUILT | orchestrator spec front door (6f28fc1), spec_intake.py + schemas/spec.json. |
| Acceptance / conformance gate | UNBUILT | per-task review:spec lens only; no final whole-delivery acceptance gate. |
| A-priori cost/time + budget | BUILT | cost_policy.ESTIMATE_USD + spec plan/file --budget-usd --strict (#34). |

Keep open carrying only the acceptance-gate bullet.'
```

_End — read-only analysis; only this file was written._
