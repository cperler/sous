---
name: triage-followups
description: After a run completes, walk the GitHub issues that run auto-filed (review non-blocking findings + improvement ideas from the evidence-out seam, AND any issue a task filed for work it deliberately cut) ONE AT A TIME with the human — explaining each from its source finding/rationale and the code it points at — and decide keep / close / promote / edit. The human triage gate the auto-filing seams deliberately do not have. Runs after any completed run (batch or single-task); re-runnable later.
---

# Triage follow-ups — human gate on a run's auto-filed issues

You are the **triage supervisor**. When a run finishes it auto-files GitHub issues with
**no human judgment**, from two sources: (1) the evidence-out seam files the review stage's
`non_blocking` findings (UNLABELED — the engine can't tell a nit's category) and the
`improvement` idea (as `enhancement`);
(2) a task files an issue for work it deliberately cut/thinned, carrying a `Source:` line
(the AGENTS.md "nothing is silently dropped" discipline). Both are by design (the run must
never block on a human), but both grow the backlog with issues the human never chose to
track and often can't parse. This skill is that missing gate: walk each auto-filed issue
**one at a time**, explain it from its *source* (the reviewer's finding or the deferral's
rationale + the code it names, not just the terse issue body), and let the human decide
whether it is worth filing/tracking at all.

This skill only **reads** the run and **acts on GitHub** (close / relabel / comment). It
never touches the engine or re-runs any stage. It is safe to run any time after a run is
terminal, and safe to re-run — it only ever surfaces issues that are still **open**, so a
second pass skips everything already triaged.

## Constants
- `ROOT` = the shared runs-root (top-level `runs/`). `RUN` = the run id to triage.
- `REPO` = the GitHub repo (`cperler/sous` for self-host; the project
  adapter's repo otherwise). `PROJECT` = the project adapter module.
- Read-only engine call shape (for the task list): `uv run orchestrator --root "$ROOT"
  --shared-root --run "$RUN" --project "$PROJECT" status`.

## Enumerate — the issues THIS run filed
Do not guess from time windows. A run auto-files issues from **two** provenance sources,
each with its own stable footer keyed to a run task id. Gate **both** — they are both
auto-filed without a human gate and both grow the backlog:

- **Review-seam issues** — the evidence-out seam files the review stage's `non_blocking`
  findings (UNLABELED) and the `improvement` idea (as `enhancement`). Footer:
  `Filed automatically from the <task_id> review`. Assigning a real label is part of triage.
- **Cut-scope issues** — a task that deliberately cuts/thins/finds-missing work files an
  ordinary issue for it (the AGENTS.md "nothing is silently dropped" discipline). These do
  NOT carry the review marker; they carry a `Source:` line naming the task id (e.g.
  `**Source:** #216 …`, `Source: #224/#223 implementation`). Match on that line, not on a
  label — they are filed under whatever ordinary label fits (`bug`, `enhancement`, `ux`).

So:

1. Get the run's task ids: `… status` → each `task_id` (e.g. `#155`, `172`).
2. Fetch open issues once and filter locally (exact substring beats GitHub full-text
   tokenization of `#`/punctuation):
   ```
   gh issue list -R "$REPO" --state open --limit 300 \
     --json number,title,body,labels,createdAt
   ```
   Keep an issue when EITHER holds for **any** task id in the run:
   - **(a) review-seam:** its body contains `Filed automatically from the <task_id> review`
     (covers both the unlabeled findings and the `enhancement` improvement); OR
   - **(b) cut scope:** its body has a `Source:` line naming that task id (e.g. `Source:` …
     `#216`). Match the task id both with and without the leading `#`. This branch is
     label-agnostic on purpose — the `Source:` line is the marker, not a label.

   Union the two sets and dedupe by issue number. That union — and only it — is what this
   run filed and what you triage. **Open only**, so a re-run skips already-triaged issues.
3. If the set is empty, say so and stop — this run filed nothing, or it's all triaged.
4. `log`/report the count and the ordered list before starting (noting each issue's
   provenance — review-seam vs. scope-ledger — so the human knows what they're looking at),
   so the human knows the size of the queue.

## The loop — one issue at a time (never batch the presentation)
For each enumerated issue, in issue-number order, present a compact **brief** and then
STOP for the human's decision. Do not move to the next issue until this one is decided.

Build each brief from three sources — this is the "under the hood" the human is asking for:

1. **The issue** — title, labels, body (`gh issue view <n> -R "$REPO"`).
2. **The source** — depends on provenance:
   - **Review-seam issue** — open the filing task's review record
     `runs/<RUN>/stages/<task>/NN-review.json` (highest attempt) and find the matching
     entry: the `non_blocking[]` element whose `title` equals the issue title, or the
     `improvement` object. Show its full `detail` and, for a non-blocking finding, its
     `disposition` (`file`/`fix_now`/`drop`) — the reviewer's own words are far richer than
     the issue body.
   - **Scope-ledger deferral** — there is no review entry; the source IS the issue body's
     own `Source:` / why-deferred / trigger-to-revisit rationale (the implement agent
     authored it deliberately). Read it, and optionally the filing task's implement record
     `runs/<RUN>/stages/<task>/NN-implement.json` for what shipped vs. what was cut, so you
     can judge whether the deferral still holds.
3. **The code it points at** — if the finding names files/paths/lines/symbols, Read that
   code (at current `main`) and show the relevant few lines, so the human sees the *actual
   thing*, not an abstract description. If it references a PR, link it.

Then write, in plain language (assume the human did not follow the review):
- **What it is** — one sentence, no jargon.
- **Why it was filed** — what the reviewer was worried about.
- **Is it real / how much effort** — your honest read: latent bug vs. nice-to-have vs.
  cosmetic; rough size (one-liner / small / real work); any dependency or overlap with
  another open issue.
- **Recommendation** — your default disposition (see below), stated as a recommendation.

Then ask the human to pick a disposition (offer these; "keep" is the safe default):
- **keep** — worth tracking. Leave open. Give it a label if it has none (`bug`,
  `enhancement`, `docs`, `ux`, `chore`) or drop a one-line clarifying comment so a future reader (or `batch-plan`)
  understands it. Nothing else to do.
- **close** — not worth the effort to track. `gh issue close <n> -R "$REPO" --reason
  "not planned" --comment "<the human's reason, or your summarized rationale>"`. Never
  close silently — the comment is the audit trail for why it was dropped.
- **promote** — do it soon. Note it for the next `batch-plan` (or, if it's really a new
  chunk of work, `spec-intake`). Add an `enhancement` label if missing and comment that
  it's queued. Do NOT start building it here — triage decides, it doesn't implement.
- **fix-now / fold** — too small to track: a real one-liner the review should have folded
  into the PR (the #213 class). Apply the fix directly now (a trivial in-place change),
  then close the issue with the rationale and a pointer to where it landed: `gh issue
  close <n> -R "$REPO" --reason "completed" --comment "Fixed in <commit/PR>: <what
  changed>."`. Use this instead of leaving a valid-but-tiny finding on the backlog to rot.
- **edit** — the observation is worth keeping but mis-scoped/mis-titled. Retitle or
  rewrite the body (`gh issue edit <n> -R "$REPO" …`) per the human, then keep it open.

Respect the human's call even when it differs from your recommendation. If they want to
stop partway, stop — the remaining issues stay open and a later re-run picks them up
exactly where this left off (open-only enumeration makes triage naturally resumable).

## Close-out
When the queue is exhausted (or the human stops), print a short ledger: each issue and its
decision (kept / closed+reason / promoted / edited), and the count still open. That ledger
is the human-readable record of the gate — the mirror of the evidence-out seam that filed
them without a gate in the first place.

## Notes
- **Scope: actually-filed issues — plus any completion note that never reached a human.**
  Findings the reviewer marked `fix_now`/`drop`, and findings over the per-task filing cap,
  were **noted in the completion note, never filed** (#188). They are not GitHub issues, so
  they are out of the issue-by-issue queue above: triage gates what was filed, not what was
  suppressed. But that reasoning assumes the note actually reached someone, and publishing
  it is a best-effort external call — on `batch-codex-3` every note failed and three valid
  `fix_now` findings reached nobody (#357). So **before** the queue, check
  `orchestrator status --run <RUN>`'s `completion_notes` block: if `clean` is false, read
  each undelivered note's `unfiled` findings (also inline in the `completion_note_failed`
  event, and the full note is at `runs/<RUN>/stages/<TASK>/completion-note.md`) and walk
  those with the human too — they have no other channel. `persist_failed` means even the
  artifact is missing; fall back to `runs/<RUN>/stages/<TASK>/NN-review.json`.
- **Never delete `runs/<RUN>/`** while reading it (the durable audit trail). This skill
  only reads it.
- This is a human-judgment loop — you present and recommend; the human decides. Do not
  auto-close a batch of issues on your own read, even ones you think are junk. One at a
  time, with a decision each.
