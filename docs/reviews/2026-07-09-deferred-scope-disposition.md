# 2026-07-09 Deferred-Scope Disposition Pass

Gate re-disposition of the **20 open `deferred-scope` issues** (per `DEFERRED.md`
discipline: promote / keep-with-comment / close-with-reason). Every premise below was
re-verified against `main` @ `0e588f3` — file:line evidence is current, not the
(now-stale) line numbers in the issue bodies. **Nothing has been enacted** — the
commands are staged in §4 for a human to run.

Two discoveries that shaped the verdicts:

1. **Parallel work in flight.** `git worktree list` shows `task/139` (`4dbd7b5`,
   "persist per-stage reasoning effort on the stage record") and `task/140` (`8d77b94`,
   "instruct the interactive supervisor to honor the WorkItem's effort") — a concurrent
   batch is already executing #139 and #140. They are dispositioned *in-flight*, not
   promoted, so nothing here double-dispatches them. The `task/139` diff was checked:
   it does **not** touch the abandon synthetic (`grep dispatched.effort` hits only the
   commit message), so #138 stays open — but becomes a one-liner once #139 merges.
2. **#95's own revisit-trigger has fired.** Its body says "revisit when the read-only
   viewer lands" — #94 landed (`3343641`). It stays Craig's-call (control-plane +
   HARD CHECKPOINT surface), with a promote-to-design-pass recommendation.

---

## 1. Verdict summary

| Disposition | Issues | Count |
|---|---|---|
| **Promote — build-ready designs** | #71, #73 | 2 |
| **Promote — hygiene micro-batch** | #112, #114, #118, #119, #121, #125, #126, #131 (+#138 gated on #139 merge) | 9 |
| **In flight — no action, do not re-dispatch** | #139, #140 | 2 |
| **Keep-with-comment (trigger not fired / blocked)** | #54, #60, #69, #95 | 4 |
| **Close-with-reason** | #122, #127, #132 | 3 |

All 20 accounted for. Net effect if §4 is run: 3 closed, 13 leave the deferred pile
into active scope, 4 remain deferred with refreshed trigger comments.

---

## 2. Per-issue evidence and rationale

### Promote — build-ready designs (the two re-homed roadmap items)

| # | Verified premise | Rationale |
|---|---|---|
| #71 meta-authoring layer | Design doc `docs/reviews/2026-07-09-fable-design-71-meta-authoring.md` landed (`0e588f3`); its stated dependencies are all live on `main` (`orchestrator/learnings_kb.py` from #72, the approval gate, the evidence-out seam). | The deferral reason ("build-fresh, needs design") is fully discharged — Fable design pass done, deps in place. Promote to build; per the working split, this is an Opus execute-cycle item. |
| #73 find→verify review workflow | Companion design doc `...-73-review-workflow.md` landed same commit; confirms today one REVIEW dispatch = one WorkItem = one `review.json` and settles the architecture (fan-out below the seam, deterministic engine-side fold). | Same: the design gate this was parked behind is passed. Promote to build. |

### Promote — hygiene micro-batch (verified-valid mechanical fixes)

All eight are auto-filed review nits whose premises still hold on `main`, each a
sub-30-minute mechanical change. Individually none justifies a run; batched they are
one micro/lite-lane run — and good dogfood for `/batch-plan`. Recommended follow-up
after §4: run `/batch-plan` over `112 114 118 119 121 125 126 131`, micro/lite lane,
no inter-task deps (add #138 to a later batch once #139 merges).

| # | Verified premise (main @ 0e588f3) | Fix shape |
|---|---|---|
| #112 | `queue_file.py:225-230` `_run_exists` catches ALL `StatusStoreError` → `False`; corrupt store reads as "run not found" and `create_run` could clobber it. | Narrow the except (not-found sentinel or `store.exists()`). The only batch item with real (edge) data-loss potential. |
| #114 | `queue_file.py:303` `assert entry is not None` — stripped under `python -O`. | `if entry is None: raise RuntimeError(...)`. |
| #118 | `deterministic_deliver.py:95-112` `_comment_fix_cycle` fires on every PR reuse; body always says "review-fix commits". | Gate on `review_cycles > 0` or genericize the body. |
| #119 | Explicit-arg override is tested (`test_deterministic_test_deliver.py:426`) and the router is tested directly (`test_cost_budget.py:81-83`), but no test covers the middle of the precedence chain: routing-decision `deterministic_stages` overriding the lane-preset default in `add_task`. | One test. |
| #121 | `cli.py:472-473` `--watch`/`--serve` are independent flags; `--serve` silently wins. | `add_mutually_exclusive_group`. |
| #125 | `queue_file.py:128` `open(lock_file, "w")` truncates the sentinel per acquisition. | `"a"` mode. |
| #126 | `peek_head` docstring (`queue_file.py:~193`) says nothing about being unlocked / single-consumer. | One docstring line. |
| #131 | `engine.py:1993` `_finalize_task_terminal(..., disposition: str)`. | `Literal["rejected", "failed"]`. |
| #138 | Abandon synthetic (`engine.py:~2134`) echoes `model=dispatched.model` but not effort; **fix requires the `StageRecord.effort` field `task/139` adds.** | One-liner (`effort=dispatched.effort`) *after* #139 merges — do not batch it before then. |

### In flight — comment only, do not re-dispatch

| # | Evidence |
|---|---|
| #139 | Branch `task/139` @ `4dbd7b5` implements exactly this (adds `effort` to `StageRecord`, stamps at `begin_stage`, folds at `apply_result`). Closes on merge. |
| #140 | Branch `task/140` @ `8d77b94` adds the supervisor effort guidance to the skill. Closes on merge. |

### Keep-with-comment — trigger not fired, or still blocked

| # | Verified state | Why keep |
|---|---|---|
| #54 interactive metering | `run_targets/workflow_shim.js:93-94` still documents the KNOWN LIMITATION: per-call usage is not on the agent result object; rows stay `metered:false`. | Still blocked on the Workflow runtime exposing a per-call usage channel. (Partial signal now exists — the Workflow tool's turn-level `budget.spent()` — but that is turn-aggregate, not per-agent-call attribution.) Trigger unchanged. |
| #60 simplify pass / subtask DAG | No subtask decomposition on `main`; triggers (all-or-nothing large-task failure; reviews repeatedly flagging complexity) have not fired. | Keep. New context worth recording: #73's design puts fan-out machinery *inside* a dispatch — a future simplify pass or implement judge-panel could ride that seam instead of resurrecting the intra-task loop. Re-check at the gate after #73 ships. |
| #69 comment-only-in-code detection | Docs-only tag is still path-based; trigger (docs-only runs frequent in real batches) has not fired. | Keep unchanged. |
| #95 dashboard control plane | Its revisit-trigger **has fired** (#94 read-only viewer landed, `3343641`). | Craig's-call by design — it is a write surface colliding with the live-run HARD CHECKPOINT (auth story, UI-as-client-of-the-approval-gate). Recommendation: promote to a Fable *design pass* (same treatment as #71/#73), not directly to build. Staged comment records the fired trigger + recommendation; the promote decision is left to Craig. |

### Close-with-reason

| # | Verified state | Reason to close |
|---|---|---|
| #122 poll-interval override | `web_dashboard.py:277` `var POLL_MS = 4000`, still hardcoded. | The issue itself calls it "firmly out of scope for a v1 local viewer"; no usage signal since. Rather than carry a standalone nit, fold the pointer into #95 (any control-plane work will rebuild the dashboard's config surface anyway). Staged commands add the pointer comment to #95 **before** closing, so nothing is silently dropped. |
| #127 cross-process lock test | `test_queue_file.py:85` still threads-based. | A multiprocessing test would exercise `fcntl.flock`'s OS-guaranteed cross-process semantics, not our code; the threads test already covers our locking discipline (each thread opens its own fd, so flock serializes them just as processes would). Cost exceeds information value. Re-file only if a real cross-process queue corruption is ever observed. |
| #132 redundant `write_task_index` | `engine.py:2222` inline write + `_surface_rejection` (`engine.py:1968`) re-render on the rejected path — and the code comment at `engine.py:2219-2221` already documents the double render as intentional. | Benign (identical content, rejected-abandon path only, two small file writes), and the site is self-documenting. Below the tracking threshold; carrying the issue costs more than the double write. |

---

## 3. Notable

1. **The pile is 60% auto-filed review follow-ups** (12 of 20 carry the "_Filed
   automatically…_" footer) — the #17 evidence-out loop is producing most of the
   deferred inventory. The hygiene-batch pattern (accumulate → batch-fix on the micro
   lane at a gate) looks like the sustainable disposal path for that stream.
2. **Effort attribution (#96's tail) is converging on its own**: #139/#140 in flight
   in a parallel batch, #138 a one-liner behind them. The disposition here deliberately
   sequences #138 *after* #139 to avoid a conflict with the in-flight worktree.
3. **Every code-level premise survived re-verification** — zero of the 20 had been
   silently fixed in the interim. The auto-filed issues are accurate against a moving
   tree, which is a good sign for trusting the evidence-out seam.

---

## 4. Ready-to-run script block — NOT EXECUTED

> Staged for a human. **None have been run.** Effects: 3 closes, 15 comments,
> 13 `deferred-scope` label removals. Review before pasting.
> Repo: `cperler/orchestration-template`.

```bash
R=cperler/orchestration-template  # zsh-safe: flag stays literal below

# ---- Promote: build-ready designs -----------------------------------------------
gh issue comment 71 -R $R --body 'Disposition 2026-07-09: PROMOTE to build. Fable design pass landed (docs/reviews/2026-07-09-fable-design-71-meta-authoring.md, 0e588f3) and its dependencies are live on main (learnings KB #72 orchestrator/learnings_kb.py, approval gate, evidence-out seam). Ready for an execute cycle; removing deferred-scope.'
gh issue edit 71 -R $R --remove-label deferred-scope

gh issue comment 73 -R $R --body 'Disposition 2026-07-09: PROMOTE to build. Fable design pass landed (docs/reviews/2026-07-09-fable-design-73-review-workflow.md, 0e588f3): fan-out below the seam (runner/shim, one dispatch), deterministic engine-side fold at record() time. Ready for an execute cycle; removing deferred-scope.'
gh issue edit 73 -R $R --remove-label deferred-scope

# ---- Promote: hygiene micro-batch (run /batch-plan over these afterward) --------
gh issue comment 112 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (queue_file.py:225-230 still catches all StatusStoreError). The one batch item with real edge data-loss potential: corrupt store reads as run-not-found and create_run could clobber it. Fix: narrow not-found sentinel or store.exists().'
gh issue edit 112 -R $R --remove-label deferred-scope

gh issue comment 114 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (assert moved to queue_file.py:303, premise unchanged). Fix: replace assert with an explicit raise so the guard survives python -O.'
gh issue edit 114 -R $R --remove-label deferred-scope

gh issue comment 118 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (deterministic_deliver.py:95-112, _comment_fix_cycle fires on every reuse with the review-fix wording). Fix: gate on review_cycles > 0 or genericize the body.'
gh issue edit 118 -R $R --remove-label deferred-scope

gh issue comment 119 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3: explicit-arg override is tested (test_deterministic_test_deliver.py:426) and the router directly (test_cost_budget.py:81-83), but the routing-decision-overrides-preset leg of the precedence chain has no test. Fix: one test.'
gh issue edit 119 -R $R --remove-label deferred-scope

gh issue comment 121 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (cli.py:472-473, flags still independent; --serve silently wins). Fix: add_mutually_exclusive_group.'
gh issue edit 121 -R $R --remove-label deferred-scope

gh issue comment 125 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (queue_file.py:128 still opens the lock sentinel in w mode). Fix: open in a mode.'
gh issue edit 125 -R $R --remove-label deferred-scope

gh issue comment 126 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (peek_head docstring still silent on the unlocked / single-consumer precondition). Fix: one docstring line.'
gh issue edit 126 -R $R --remove-label deferred-scope

gh issue comment 131 -R $R --body 'Disposition 2026-07-09: PROMOTE into the hygiene micro-batch. Verified on main @ 0e588f3 (engine.py:1993, disposition: str). Fix: Literal["rejected", "failed"].'
gh issue edit 131 -R $R --remove-label deferred-scope

# #138 promotes too, but is SEQUENCED BEHIND the in-flight #139 (task/139 adds the
# StageRecord.effort field this fix reads). Do not include it in the same batch run.
gh issue comment 138 -R $R --body 'Disposition 2026-07-09: PROMOTE, gated on #139 merging. Verified: the in-flight task/139 branch (4dbd7b5) adds StageRecord.effort but does NOT touch the abandon synthetic (engine.py ~2134 still echoes model only). Once #139 lands this is a one-liner: effort=dispatched.effort in the synthetic StageResult. Do not batch before the merge.'
gh issue edit 138 -R $R --remove-label deferred-scope

# ---- In flight: record only, no dispatch ----------------------------------------
gh issue comment 139 -R $R --body 'Disposition 2026-07-09: IN FLIGHT — task/139 @ 4dbd7b5 implements this (StageRecord.effort stamped at begin_stage, folded at apply_result). No re-dispatch; closes on merge. Note: #138 is gated behind this merge.'
gh issue edit 139 -R $R --remove-label deferred-scope

gh issue comment 140 -R $R --body 'Disposition 2026-07-09: IN FLIGHT — task/140 @ 8d77b94 adds the supervisor effort guidance to orchestrate-task-interactive. No re-dispatch; closes on merge.'
gh issue edit 140 -R $R --remove-label deferred-scope

# ---- Keep-with-comment: triggers re-affirmed -------------------------------------
gh issue comment 54 -R $R --body 'Disposition 2026-07-09: KEEP (still blocked). workflow_shim.js:93-94 still documents the KNOWN LIMITATION — per-call usage is not on the agent result object. Partial new signal: the Workflow tool now exposes turn-level budget.spent(), but that is turn-aggregate output tokens, not per-agent-call attribution, so ledger rows stay honestly metered:false. Trigger unchanged.'

gh issue comment 60 -R $R --body 'Disposition 2026-07-09: KEEP (trigger not fired). New context for the next gate: the #73 design (docs/reviews/2026-07-09-fable-design-73-review-workflow.md) puts multi-agent fan-out INSIDE a single dispatch at the runner seam — a future simplify pass or implement judge-panel could ride that machinery instead of resurrecting an intra-task loop. Re-check after #73 ships.'

gh issue comment 69 -R $R --body 'Disposition 2026-07-09: KEEP (trigger not fired). Docs-only tag remains path-based by design; no evidence yet that docs-only runs are frequent enough in real batches for comment-only detection to pay for its language-aware parsing risk.'

gh issue comment 95 -R $R --body 'Disposition 2026-07-09: KEEP, but the revisit trigger HAS FIRED — the read-only viewer (#94) landed (3343641). Recommendation: promote to a Fable DESIGN pass (same treatment as #71/#73), not directly to build — the open questions are auth for a localhost write surface and making the UI a client of the existing approval gate, never a bypass of the live-run HARD CHECKPOINT. Absorbs #122 (poll-interval override) as a line item: any control-plane work rebuilds the dashboard config surface, so fold --poll-ms in there.'

# ---- Close-with-reason ------------------------------------------------------------
gh issue close 122 -R $R --reason 'not planned' --comment 'Disposition 2026-07-09: CLOSE — folded into #95 as a line item (comment added there). The issue itself scoped this out of the v1 local viewer and no usage signal has appeared; any dashboard control-plane work rebuilds the config surface where --poll-ms belongs. Nothing dropped: #95 carries the pointer.'

gh issue close 127 -R $R --reason 'not planned' --comment 'Disposition 2026-07-09: CLOSE — a multiprocessing test would exercise fcntl.flock cross-process semantics the OS guarantees, not our code; the threads test (test_queue_file.py:85, per-thread fds) already covers our locking discipline, which is the part we own. Re-file if a real cross-process queue corruption is ever observed.'

gh issue close 132 -R $R --reason 'not planned' --comment 'Disposition 2026-07-09: CLOSE — verified benign on main @ 0e588f3: identical content rendered twice on the rejected-abandon path only, and the site is already self-documenting (engine.py:2219-2221 comment explains the re-render from the read-back artifact). Below the tracking threshold.'
```

**After running §4:** `/batch-plan` over `112 114 118 119 121 125 126 131` (micro/lite
lane, no inter-task deps); queue #138 separately once #139 merges; decide the #95
design-pass promotion.

_End — read-only analysis; only this file was written._
