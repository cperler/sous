# Design pass — #73 multi-agent find→verify review workflow (Fable)

Design for issue #73 (Roadmap D re-home: REVIEW as a real internal multi-agent workflow
— parallel finder lenses → adversarial verify → synthesize — not a single reviewer with
richer criteria), in the style of `2026-07-01-fable-design-pass.md`. References are to
symbols, not line numbers. Assumes as-built `main` as of 2026-07-09. Companion doc:
`2026-07-09-fable-design-71-meta-authoring.md` (#71) — independent.

**What exists is deliberately not this.** #62/#15/#13 added *lenses and roles*:
`_DESIGN_REVIEW_LENS` is appended text, `review:spec`/`review:design` are roster entries
in `heysoo/config.py` that nothing reaches (the `STAGE_SPECS[Stage.REVIEW].agent_role`
is the fixed string `"review"`), convergence auto-approval (`_review_verdict`) and
`tests_meaningful` (#13) are verdict math over a *single* reviewer's output. Today one
REVIEW dispatch = one WorkItem = one mega-prompt = one model = one `review.json`. The
workflow — independent finders that cannot see each other, an adversary that tries to
kill each finding, a verdict assembled from what survives — does not exist anywhere
(grep confirms: the shim's `parallel()` fans across *different* WorkItems only).

**The architecture decision, up front: fan out below the seam, synthesize above it.**

- **Fan-out lives in the runner** (execution adapter / Workflow shim), inside a single
  dispatch. Engine-side fan-out (N WorkItems per stage) was considered and rejected: the
  dispatch lease is singular (`pending_work_item_id`), a `StageRecord` holds one
  `output` per attempt, and the scheduler/resume model assumes one outstanding item per
  task — N-items-per-stage is a schema-and-state-machine rewrite for zero quality gain.
- **Synthesis lives in the engine** as a deterministic pure fold at `record()` time.
  Runner-side synthesis was considered and rejected: it would need two implementations
  (JS in `workflow_shim.js`, Python in the transports) that *will* drift, and the
  verdict — what blocks, what converges — belongs where the rest of the verdict math
  lives. The engine folding model outputs deterministically is exactly the context-plane
  precedent; "the engine never calls a model" is untouched because synthesis is
  arithmetic over sub-results, not a model call.

> The workflow is invisible above the seam except for attribution: one WorkItem out,
> one StageResult back, and the recorded output is canonical `review.json` produced by
> a deterministic engine-side fold — never by a synthesizer model. Every sub-call gets
> its own ledger row: no model call is unattributed, including the ones inside a
> dispatch.

---

## 1. The plan: engine-rendered, carried on the WorkItem

**Problem.** For replay/audit, prompts must be engine-rendered and byte-stable
(`render_prompt` discipline); but the runner is what executes the workflow. The bridge
is a *plan*: data on the WorkItem, authored by the engine, executed below the seam.

**Design.**

- `WorkItem` gains `plan: ReviewPlan | None` (new frozen model in
  `orchestrator/schemas/work.py`). `ReviewPlan` carries:
  - `finders: tuple[FinderSpec, ...]` — each `{lens: str, prompt: str, agent: str |
    None, schema_ref: "review_findings"}`, prompt fully rendered by the engine.
  - `verify_template: str` — a prompt template with mechanical slots
    (`{finding}`, `{diff_hint}`) the runner fills per finding. Same precedent as
    `_corrective_prompt` in `_schema_retry_loop`: runners may do slot substitution,
    never authorship.
  - `verify_schema_ref: "review_verdict"`, `dedupe_rule: "fingerprint-v1"` (names the
    shared rule so both lanes normalize identically).
- **Finder set, resolved deterministically at `next_work`** (new
  `render_review_plan(...)` in `stages.py`, beside `render_prompt`):
  - `find:code` — correctness/regressions lens; agent `agent_for(REVIEW, "review")`.
  - `find:spec` — built-vs-asked conformance lens; agent
    `agent_for(REVIEW, "spec")` (this finally makes the latent `review:spec` roster key
    reachable; `agent_for` returning None falls back to the base reviewer persona).
  - `find:design` — included only when `_has_frontend_change(context["files_changed"])`;
    body is `_DESIGN_REVIEW_LENS` reframed as a finder; agent
    `agent_for(REVIEW, "design")`.
  - `find:tests` — test-meaningfulness as its own independent dispatch: the strong form
    #13 explicitly deferred ("a separate reviewer dispatch judging test meaningfulness").
  Each finder prompt = the shared context sections (`render_prompt`'s cache-stable
  framing: project commands → task spec → folded context) + only its lens instruction.
  Finders are blind to each other by construction.
- **Identity:** the plan is part of what the work *is*, so it folds into
  `compute_content_hash` (alongside stage+prompt+schema+model+lane+attempt). It is not
  routing metadata like `session_ref`/`cwd` — two dispatches with different finder sets
  are different work.
- **Docs-only interplay:** when `context["change_class"] == "docs-only"` (ENGINE-lane
  trusted via `DETERMINISTIC_ONLY_KEYS`), `find:tests` is omitted and the fold treats
  `tests_meaningful` as satisfied — same relaxation, same trust boundary as today.
  Lens *additions* may key off model-influenced context (`files_changed`) because more
  scrutiny is the safe direction; only relaxations demand the ENGINE-lane tag.

**Schemas** (new, in `orchestrator/schemas/stages/`, mirrored into the kit):

- `review_findings.json` — `{findings: [{severity: critical|important|suggestion,
  file, line, description*, suggested_fix}], tests_meaningful?, improvement?,
  retrospective?}`. Findings reuse the issue-object vocabulary of `review.json` so
  `_issue_fingerprint` applies unchanged.
- `review_verdict.json` — `{fingerprint*, verdict*: confirmed|refuted, reasoning*}`.

---

## 2. Runner execution: find → dedupe → adversarial verify

**Design (headless claude transport first; the contract is lane-agnostic):**

- Run all finders (concurrency is a runner freedom, not a contract — sequential is
  correct, parallel is faster); each sub-call goes through the existing
  `_schema_retry_loop` against `review_findings.json`, with per-sub-call stream tee to
  `stages/<task>/<stage>-attempt<N>.<phase>.stream.jsonl` (`phase` =
  `find:code`, `verify:3`, …).
- Mechanically dedupe findings across finders by `dedupe_rule` (the
  `_issue_fingerprint` normalization: `file:description`, whitespace-collapsed,
  casefolded, 160 chars). Duplicate-verification is only waste, not a correctness
  concern — the engine re-dedupes authoritatively at synthesis.
- **Adversarial verify:** each deduped `critical`/`important` finding gets one verifier
  sub-call — `verify_template` filled with the finding, instructed to *refute it with
  evidence from the working tree* and to confirm only what it cannot kill.
  `suggestion`-severity findings skip verification (they can't block; the severity gate
  in `_review_verdict` already auto-approves all-suggestion reviews).
- **Failure direction:** a verifier that errors, times out, or returns an unmatchable
  fingerprint leaves its finding **confirmed** — verification may only *remove*
  scrutiny it has affirmatively earned; failing open toward "blocking" is the safe
  direction (mirrors the fail-OPEN convention, pointed the right way for a gate).
- The runner returns **one StageResult** carrying:
  - `sub_results: {findings_by_lens: {...}, verdicts: [...]}` — raw, unfolded.
  - `sub_calls: list[SubCall]` (new on `StageResult`) — per sub-call
    `{phase, model, usage: TokenUsage, duration_s, session_id, stream_file}`.
  - `output=None` for a plan-bearing review — the engine's fold owns `output`.
- A finder that fails terminally after schema retries fails the whole dispatch normally
  (one stage, one attempt, existing retry machinery) — no partial-panel verdicts.

---

## 3. Synthesis: a pure fold in the engine

**Design.** New module `orchestrator/review_workflow.py`,
`synthesize(sub_results) -> dict` producing canonical `review.json`; `Engine.record()`
calls it when the result carries `sub_results` for a REVIEW stage, then proceeds into
the *unchanged* downstream: `_merge_policy_findings`, `_review_verdict` (convergence
fingerprints, severity gate), `_apply_review_rejection`, evidence-out.

Deterministic rules, in order:

- `issues` = confirmed `critical`/`important` findings, deduped by fingerprint, stable
  sort (severity rank, then fingerprint).
- `non_blocking` = all `suggestion` findings **plus refuted findings** (prefixed
  `refuted:` with the verifier's reasoning) — an adversary killing a finding must not
  silently erase it; evidence-out (`_file_review_followups`) still files it for a human
  to see, closing the false-negative loop.
- `tests_meaningful` = `find:tests`'s report; only an explicit `false` is vacuous
  (fail-OPEN preserved); docs-only omits the lens and the fold writes `true`.
- `approved` = `issues` is empty **and** tests not vacuous. Nothing else. The verdict is
  a pure function of what survived — no synthesizer model, so a model can never talk
  the panel's findings back out of the verdict.
- `improvement` / `retrospective` = first non-null in fixed lens order (`find:code`,
  `find:spec`, `find:design`, `find:tests`) — one each, so #71's capture path sees the
  same single-retrospective shape as today.

Byte-stable given the same `sub_results` — replay reproduces the recorded output. The
raw `sub_results` persist in the per-stage log (`write_stage_log`) as evidence; the
folded output is what the status doc, context plane, and convergence math consume.
Fix cycles are untouched: a rejection cascades via `reset_for_fix_cycle` exactly as
now, and the re-review re-runs the whole plan; convergence compares synthesized
fingerprints against `last_review_rejection` with zero changes to the math.

---

## 4. Attribution: sub-calls become first-class ledger rows

**Problem.** `CostLedger.record` writes one row per StageResult. N sub-calls inside one
dispatch would repeat the #70 hole (schema-retry sub-calls under-attributed) at 6–10×
the size — the exact "no unattributed model call" invariant the seam exists for.

**Design.** When a result carries `sub_calls`, the ledger writes **one row per
sub-call** — each with the shared `work_item_id`/stage/attempt plus a `phase`
discriminator — and no aggregate row (sums are the report's job, and double-counting is
worse than adding). Rows are priced from the engine's `model_table` as always, never the
runner's self-report. `_schema_retry_loop` retries *within* a sub-call keep riding that
sub-call's `schema_retries` count. `cost-report`/`status` group by `work_item_id`, so a
workflow review reads as one stage with a visible internal breakdown.

---

## 5. Lanes and selection

- **headless×claude** — reference implementation (transport-level, §2).
- **interactive×claude** — `workflow_shim.js` grows a per-WorkItem branch: a
  plan-bearing item runs finders via its existing `agent()`/`parallel()` primitives
  (this is precisely the "shim grows stage-internal fan-out support" trigger the issue
  named), fills verify slots mechanically, and returns the same
  `sub_results`/`sub_calls` StageResult shape. It does **no folding** — synthesis
  happens in Python when the supervisor persists via `orchestrator record`, so the two
  lanes share one implementation. The interactive≡headless conformance test extends to
  a plan-bearing review.
- **codex** — v1 ignores the plan and dispatches the single-reviewer prompt (codex exec
  has no sub-agent primitive). `next_work` therefore only attaches a plan when the
  resolved lane supports it (a runner capability flag in the execution registry, like
  `EXPLICIT_EMPTY`); since the plan is in `content_hash`, lane and hash stay consistent.
- **Selection/rollout:** off by default. A per-run `review_workflow` flag
  (`Engine.__init__`/CLI, like `cross_provider_fallback`) opts in; cost/capacity policy
  can veto — low `dispatch_band`/thinning budget forces the single-reviewer path, and
  micro/lite presets never use it (they exist to be cheap). The plan-less path must
  remain byte-identical to today — it is the permanent fallback, not scaffolding.

---

## Tests

(a) `render_review_plan`: frontend `files_changed` ⇒ `find:design` present; docs-only ⇒
no `find:tests`; plan folds into `content_hash` (two finder sets ⇒ two hashes).
(b) `synthesize` as a pure fold — table-driven: confirmed critical ⇒ rejected; all
refuted ⇒ approved with `refuted:` non-blocking entries; verifier-missing ⇒ finding
survives as blocking; explicit `tests_meaningful=false` ⇒ vacuous even with zero issues;
byte-identical output on repeated folds.
(c) Fake runner returning `sub_results` ⇒ `record()` transitions identical to an
equivalent hand-written single review; convergence auto-approval fires on synthesized
fingerprints across a fix cycle.
(d) Ledger: one row per sub-call, all sharing `work_item_id`, distinct `phase`s;
cost-summary total = Σ sub-calls; no aggregate double-count row.
(e) Regression: `review_workflow` off ⇒ WorkItem, prompt, hash, rows byte-identical to
pre-change behavior.
(f) Interactive≡headless conformance over a plan-bearing review (fake `agent()` shim
harness vs fake transport).
(g) A finder failing terminally after schema retries fails the dispatch (one attempt
consumed), and the retry re-dispatches the full plan.

---

**Explicitly not now:** the implement judge-panel (same plan-on-WorkItem seam, separate
design once review proves the pattern); engine-side N-WorkItems-per-stage fan-out; a
codex-lane workflow; a synthesizer model call; per-finder reasoning-effort tuning
(#141's effort table applies at the dispatch level first); cross-finder debate rounds
or re-verification loops (one find pass, one verify pass — loop-until-dry is a quality
pattern for a later evidence-driven pass).

**Suggested build order:** schemas + `ReviewPlan`/`sub_calls` contract types →
`synthesize` (pure, test it exhaustively first — it is the verdict) → ledger sub-rows →
headless transport execution → shim branch + conformance test → selection flag/policy.
The contract types land first because both lanes and the fold compile against them; the
fold lands before any runner so a fake runner can drive the full engine path in tests
from day one.

---

## Open questions (recommendations inline — decide at build time)

1. **Verify scope:** verify only `critical`/`important` (recommended — suggestions
   cannot block, and verifier calls are the cost driver) vs. verify everything.
2. **Refuted findings filed as follow-ups:** recommend **yes** (as designed —
   `non_blocking` with the `refuted:` prefix); if it proves noisy on real PRs, demote to
   stage-log-only and re-disposition with evidence.
3. **Where the flag defaults on:** recommend FULL-preset runs only after two live runs
   show the panel catching findings the single reviewer missed (the eval evidence the
   2026-07-01 pass keeps asking for); until then, explicit opt-in per run.
4. **Finder cap:** the set is fixed at ≤4 by construction; if project adapters later
   want custom lenses, that is a `ProjectConfig` hook (`review_lenses`?) — do not build
   it speculatively.
