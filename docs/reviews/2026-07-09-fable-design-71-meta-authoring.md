# Design pass — #71 meta-authoring layer (Fable)

Design for issue #71 (Roadmap A re-home: runs propose changes to their own prompts /
skills / agent rosters, human-gated apply), in the style of
`2026-07-01-fable-design-pass.md`. References are to symbols, not line numbers — the
code is moving under this doc. Assumes as-built `main` as of 2026-07-09: the cross-run
learnings KB (#72, `orchestrator/learnings_kb.py`), the approval gate
(`TaskState.BLOCKED_ON_HUMAN`, `Engine.approve`, `StatusStore.write_approval`), and the
evidence-out seam (`Engine._file_review_followups` / `_file_review_improvement` over the
adapter's duck-typed `file_followup`) are all live. Companion doc:
`2026-07-09-fable-design-73-review-workflow.md` (#73) — independent; they touch only
where noted in §5.

**The shape decision, up front.** Meta-authoring is *not* a new engine subsystem and not
a self-editing meta-agent. Every piece it needs already exists as a seam; the design is
three small additions that route through them:

1. **Capture** — the REVIEW stage's `retrospective` field (a process lesson about the
   orchestration itself, today rendered into the completion note and then lost) is
   harvested into the learnings KB as a new `process` kind, with an optional
   model-supplied `target` naming the prompt/persona/skill it complains about.
2. **Detect** — a deterministic cross-run recurrence detector runs at run finalize; when
   the same process complaint has now arrived from ≥N *distinct runs*, it files a
   `meta-authoring` issue through `file_followup`, carrying the accumulated evidence.
   This is the issue's own trigger ("retrospectives repeating the same template-level
   complaint across runs") turned into the mechanism.
3. **Apply, gated** — the filed issue is a *normal task* for the existing dogfood loop
   (selfhost adapter): scope → implement (edit the template constant / persona `.md` /
   `SKILL.md`) → review → deliver-as-PR. The human merge is the apply. A small generic
   `hold_before` mechanism parks the task `BLOCKED_ON_HUMAN` before DELIVER, so even the
   PR needs an explicit `Engine.approve` first.

> The engine never authors, evaluates, or applies a proposal. It counts
> (deterministically), files (through the adapter seam), and gates (through the
> approval artifact). Model judgment enters only inside ordinary task stages, and a
> human is the only apply.

This respects the two hard invariants: the engine never calls a model (proposal
*authoring* is model work, so it happens inside a normal implement stage, not in
`orchestrator/`), and KB-side processing stays strictly deterministic/lexical (the
detector is arithmetic over fingerprints, no clustering model).

---

## 1. Capture: `retrospective` → KB `process` entries

**Problem.** `review.json` already carries `retrospective` (`{title, detail}` — "one
lesson about the ORCHESTRATION PROCESS") but it is only rendered by
`render_completion_note`; it does not survive the run. The KB harvests only
`Task.learnings` (failure-shaped strings). The raw material for meta-authoring
evaporates at exactly the moment it exists.

**Design.**

- `review.json` schema: `retrospective` gains an optional `target` object —
  `{kind: "stage-template" | "agent" | "skill" | "stage-schema" | "kit", ref: str}`
  (e.g. `{"kind": "stage-template", "ref": "REVIEW"}`,
  `{"kind": "agent", "ref": "code-reviewer"}`). Model-supplied, advisory, absent is
  fine. The REVIEW template in `STAGE_SPECS` grows one sentence asking for it. The
  vocabulary matters: stage prompt templates are **Python constants in
  `orchestrator/stages.py`**, personas are `.claude/agents/*.md` resolved by name via
  `agent_for`, skills are `.claude/skills/*/SKILL.md`, and the scaffold seeds live in
  `templates/project-default/` — `kind` tells the eventual implementer which of these
  artifact classes the complaint is about, since there is no single "templates dir".
- `learnings_kb.VALID_KINDS` gains `"process"`. A new harvest sibling next to
  `Engine._harvest_task_learnings` (call it `_harvest_process_retrospective`) runs in
  the same task-finalize path: it reads the REVIEW record's `retrospective`, appends one
  KB entry with `kind="process"`, `text = title + ": " + detail` (bounded by the
  existing `bound_text`), and stores `target` in the entry (new optional KB field,
  defaulting absent — append-only JSONL tolerates this). Same `_fingerprint` dedupe.
- **Recall exclusion:** `relevant_learnings` must *not* surface `process` entries into
  task prompts — a complaint about the review template is advice for the harness
  maintainer, not for the next product task. Filter `kind == "process"` out of recall;
  they are detector fuel only.
- Best-effort like all evidence-out: wrapped, `harvest_failed`-style event on error,
  never breaks finalize.

**Tests.** (a) A completed task whose review carries `retrospective` produces exactly
one `process` KB entry with the target; (b) re-finalizing (idempotent finalize) does not
duplicate it (fingerprint dedupe); (c) `relevant_learnings` never returns `process`
entries; (d) a malformed/absent `retrospective` harvests nothing and does not raise.

---

## 2. Detect: deterministic recurrence → a filed proposal

**Problem.** One run's complaint is noise; the same complaint from independent runs is
signal. Nothing today aggregates across runs except the KB itself, and nothing files
from it.

**Design.**

- New module `orchestrator/meta_authoring.py` — pure functions, mirror of
  `learnings_kb.py`'s discipline (no model, no embeddings, no wall-clock beyond
  injected `now`):
  - `cluster_key(entry) -> str` — `f"{target.kind}:{target.ref}"` when a target exists,
    else the entry's text `_fingerprint`. Lexical only.
  - `recurring_proposals(entries, *, min_runs=2) -> list[dict]` — group `process`
    entries by cluster key; a cluster fires when it spans ≥ `min_runs` **distinct
    `run_id`s** (within-run repetition never fires — that's one run's mood, and it
    would let a single noisy run self-propose). Returns
    `{key, target, evidence: [{run_id, task_id, ts, text}, ...]}`.
- **Filing ledger.** `<runs-root>/meta-proposals.jsonl`, sibling of
  `learnings-kb.jsonl`: one row per filed cluster `{key, ref, filed_at, run_id}`.
  A cluster with a ledger row never re-files; new evidence on an already-filed cluster
  is dropped (the open issue is the accumulator now — a `publish_note`-style
  append-comment refinement is explicitly deferred). Same append/flock conventions as
  the KB.
- **Wiring.** At run finalize, after `_harvest_retrospective`: read the KB, compute
  `recurring_proposals`, subtract the ledger, and file each survivor via the task
  source's `file_followup` with labels `meta-authoring, enhancement` — the exact seam
  `_file_review_improvement` uses, so the engine still never shells `gh`. Emit
  `meta_proposal_filed` / `meta_proposal_failed` events. Best-effort, never breaks
  finalize; on filing failure the ledger row is *not* written (retry next run).
- **Issue body** is the proposal's evidence, not a diff: the target
  (`kind:ref` and where that artifact lives), the verbatim complaint texts with their
  `run_id`/`task_id` provenance, and a fixed instruction footer ("propose a concrete
  diff to this artifact; template constants live in `orchestrator/stages.py`
  `STAGE_SPECS`…"). The *diff* is authored later, by a model, inside the task this
  issue becomes.
- **Cross-tracker subtlety, decided simply for v1:** proposals file to the *current
  run's* task source. For selfhost runs that is this repo's tracker (correct: templates,
  skills, kit, stage schemas all live here). For product runs (heysoo), a
  `kind="agent"` complaint targets a persona `.md` living in the *product* repo — also
  correct, since that tracker is the product's. The one mismatch (a heysoo run
  complaining about an engine template) files to heysoo's tracker with the target
  clearly naming this repo; routing that issue across trackers is a human triage step,
  not v1 machinery.

**Tests.** (a) Two entries, same cluster key, two distinct runs ⇒ exactly one
`file_followup` call with both evidence rows; (b) two entries same run ⇒ nothing fires;
(c) already-ledgered cluster ⇒ nothing re-files; (d) `file_followup` raising ⇒ event
recorded, no ledger row, finalize completes; (e) targetless entries cluster by text
fingerprint; (f) determinism: same KB + ledger ⇒ byte-identical proposal list.

---

## 3. Apply: the dogfood loop is the meta-agent, `hold_before` is the gate

**Problem.** "Human-gated apply" must be mechanism, not prose (the same gap §4 of the
2026-07-01 design pass closed for live runs). And a run modifying the prompts that will
grade its successors is the one place autonomy should be *narrower* than normal.

**Design.**

- **No new execution machinery.** A `meta-authoring` issue enters through the normal
  front door (`batch-plan` / `orchestrate-task-interactive`) as an ordinary task on the
  **selfhost adapter** — scope, implement (edit `stages.py` template text, a persona
  `.md`, a `SKILL.md`, or a kit file), test (`uv run pytest` — template-text golden
  tests keep this honest), review, deliver-as-PR. The existing live-run HARD CHECKPOINT
  already means a human picks and approves the run.
- **`hold_before`: a small generic pre-stage gate.** `Task` gains
  `hold_before: Stage | None` (persisted; set at `add_task`). When
  `next_stage(task)` equals `hold_before` and no approval artifact exists
  (`StatusStore.load_approval`), `next_work` parks the task `BLOCKED_ON_HUMAN`
  (reusing `hold_for_approval`'s path) instead of dispatching; `Engine.approve` releases
  it exactly as it releases review-exhaustion holds today. This is deliberately generic
  — it also mechanizes the "deliver to a real repo needs a human" checkpoint for
  non-meta tasks, which §4 of the 2026-07-01 pass anticipated ("any deliver stage that
  would push/PR to a real repo").
- **Policy:** the front door sets `hold_before=Stage.DELIVER` on any task whose source
  issue carries the `meta-authoring` label. So a meta task can be scoped, implemented,
  and reviewed autonomously, but cannot even *open* the PR without
  `orchestrator approve`; the merge is a second, independent human gate.
- **Self-review honesty:** the meta task's own review runs under the very prompts being
  criticized. That is acceptable *because* apply is double-gated (approve + merge) — the
  review is advisory quality control here, not the gate. Convergence auto-approval may
  still end its fix loop; it cannot ship anything.

**Tests.** (a) `hold_before=DELIVER` task completes review then parks
`BLOCKED_ON_HUMAN` with no DELIVER dispatch; (b) `approve` releases it and DELIVER
dispatches once; (c) approval artifact present at `add_task`-time does not pre-satisfy a
later hold (artifact is per-`what`, checked fresh); (d) `hold_before=None` is
byte-identical to today (regression); (e) scheduler treats the held task as quiescent
(no lease, dependents not cascaded).

---

## 4. What "a concrete diff to the stage templates" means (and why not a patch artifact)

Considered and rejected: having the run itself emit a unified diff as a run artifact
(`write_run_artifact("proposal-*.md")`) that a CLI verb applies. Rejected because (a)
the diff would be authored by a model *inside a product-task stage*, against engine
source it has no worktree for — wrong cwd, wrong repo, unreviewable provenance; (b) an
apply verb that patches `orchestrator/stages.py` outside the PR flow bypasses tests,
review, and git history — precisely the accountability the moat exists for. The
issue→task→PR path gets the same concrete diff with all of that intact, and it reuses
the loop that is already live-proven (#69/#70 were auto-filed by this seam).

**Explicitly not now:** auto-apply of any kind; model-side clustering or semantic
similarity in the detector (lexical fingerprints only, same invariant as the KB);
appending late evidence as comments on an already-filed proposal; cross-tracker routing
machinery; extracting stage templates from Python constants into per-project files
(a real question, but it is #73-adjacent template surgery — do not couple it to this);
proposals targeting engine *logic* (the vocabulary is prompt/persona/skill/schema/kit
content only).

---

## 5. Interaction with #73 (review workflow)

If #73 lands, a REVIEW stage produces several finder outputs, each of which may carry a
`retrospective`; #73's synthesis picks one deterministically, so this design still sees
exactly one `retrospective` per review — no change here. In the other direction, the
richest early source of `process` complaints will be reviews themselves ("the review
prompt made me re-derive the test baseline…"), which is why capture (§1) is worth
landing even before any detector fires.

**Suggested build order:** §1 capture → §3 `hold_before` → §2 detector. Capture first
because evidence accumulates only from the day it lands (every run before it is lost
fuel); the detector is useless until the KB holds `process` entries from ≥2 runs, so it
goes last; `hold_before` is independent and small, and other work (web-dashboard write
path #95) wants it too.

---

## Open questions (recommendations inline — decide at build time)

1. **`min_runs` threshold:** recommend **2** — the issue's own trigger phrase is
   "repeating across runs", and the filing is cheap/reversible (an issue, not a change).
2. **Should `improvement` entries also feed the detector?** Recommend **no** —
   improvements are already filed as enhancement issues at N=1 by
   `_file_review_improvement`; double-filing them adds noise, and the meta layer is
   specifically for the *process* channel that today has no outlet.
3. **Does a failed-run retrospective (`Engine.retrospective`'s cross-task patterns)
   feed `process` entries too?** Recommend **yes but later** — `_harvest_retrospective`
   already distils patterns into the KB; tagging the template-shaped ones `process` is a
   one-line refinement once the detector exists and its precision is observed.
