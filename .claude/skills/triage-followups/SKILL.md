---
name: triage-followups
description: After a run completes, walk the GitHub issues that run auto-filed (review non-blocking findings + improvement ideas) ONE AT A TIME with the human — explaining each from its source finding and the code it points at — and decide keep / close / promote / edit. The human triage gate the evidence-out seam deliberately does not have. Runs after any completed run (batch or single-task); re-runnable later.
---

# Triage follow-ups — human gate on a run's auto-filed issues

You are the **triage supervisor**. When a run finishes, its evidence-out seam files the
review stage's `non_blocking` findings (as `deferred-scope` issues) and the `improvement`
idea (as an `enhancement` issue) — automatically, with **no human judgment**. That is by
design (the run must never block on a human), but it means the backlog grows with issues
the human never chose to track and often can't parse. This skill is that missing gate:
walk each auto-filed issue **one at a time**, explain it from its *source* (the reviewer's
finding + the code it names, not just the terse issue body), and let the human decide
whether it is worth filing/tracking at all.

This skill only **reads** the run and **acts on GitHub** (close / relabel / comment). It
never touches the engine or re-runs any stage. It is safe to run any time after a run is
terminal, and safe to re-run — it only ever surfaces issues that are still **open**, so a
second pass skips everything already triaged.

## Constants
- `ROOT` = the shared runs-root (top-level `runs/`). `RUN` = the run id to triage.
- `REPO` = the GitHub repo (`cperler/orchestration-template` for self-host; the project
  adapter's repo otherwise). `PROJECT` = the project adapter module.
- Read-only engine call shape (for the task list): `uv run orchestrator --root "$ROOT"
  --shared-root --run "$RUN" --project "$PROJECT" status`.

## Enumerate — the issues THIS run filed
Do not guess from time windows. Every auto-filed issue carries a stable provenance footer
keyed to the filing task: `Filed automatically from the <task_id> review`. So:

1. Get the run's task ids: `… status` → each `task_id` (e.g. `#155`, `172`).
2. Fetch open issues once and filter locally on the marker (exact substring beats GitHub
   full-text tokenization of `#`/punctuation):
   ```
   gh issue list -R "$REPO" --state open --limit 300 \
     --json number,title,body,labels,createdAt
   ```
   Keep an issue when its body contains `Filed automatically from the <task_id> review`
   for **any** task id in the run. That set — and only that set — is what this run filed
   and what you triage. (Both the `deferred-scope` non-blocking findings and the
   `enhancement` improvement idea carry the same marker.) **Open only**, so a re-run skips
   already-triaged issues.
3. If the set is empty, say so and stop — this run filed nothing, or it's all triaged.
4. `log`/report the count and the ordered list before starting, so the human knows the
   size of the queue.

## The loop — one issue at a time (never batch the presentation)
For each enumerated issue, in issue-number order, present a compact **brief** and then
STOP for the human's decision. Do not move to the next issue until this one is decided.

Build each brief from three sources — this is the "under the hood" the human is asking for:

1. **The issue** — title, labels, body (`gh issue view <n> -R "$REPO"`).
2. **The source finding** — open the filing task's review record
   `runs/<RUN>/stages/<task>/NN-review.json` (the `NN-review` with the highest attempt),
   and find the matching entry: the `non_blocking[]` element whose `title` equals the
   issue title, or the `improvement` object. Show its full `detail` and, for a
   non-blocking finding, its `disposition` (`file`/`fix_now`/`drop`) — the reviewer's own
   words are far richer than the issue body.
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
- **keep** — worth tracking. Leave open. Optionally relabel (e.g. `deferred-scope` →
  `enhancement`) or drop a one-line clarifying comment so a future reader (or `batch-plan`)
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
- **Scope: only actually-filed issues.** Findings the reviewer marked `fix_now`/`drop`, and
  findings over the per-task filing cap, were **noted in the completion note, never filed**
  (#188) — they are not GitHub issues, so they are out of scope here. Triage gates what was
  filed, not what was suppressed.
- **Never delete `runs/<RUN>/`** while reading it (the durable audit trail). This skill
  only reads it.
- This is a human-judgment loop — you present and recommend; the human decides. Do not
  auto-close a batch of issues on your own read, even ones you think are junk. One at a
  time, with a decision each.
