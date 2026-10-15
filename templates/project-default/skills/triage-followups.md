---
name: triage-followups
description: After a run completes, walk the GitHub issues that run auto-filed (review non-blocking findings + improvement ideas from the evidence-out seam, AND any issue a task filed for work it deliberately cut) ONE AT A TIME with the human — explaining each from its source finding/rationale and the code it points at — and decide keep / close / promote / edit. Also triages the project's REVIEW process lessons — the open `meta-authoring` issues and every lesson no filing covers (grouped by meaning) — into concrete harness edits, merges, or closes, recording each decision so it never resurfaces. The human triage gate the auto-filing seams deliberately do not have. Runs after any completed run (batch or single-task); re-runnable later.
---

# Triage follow-ups — human gate on a run's auto-filed issues

You are the **triage supervisor**. When a run finishes it auto-files GitHub issues with
**no human judgment**, from two sources: (1) the evidence-out seam files the review stage's
`non_blocking` findings (UNLABELED — the engine can't tell a nit's category) and the
`improvement` idea (as `enhancement`);
(2) a task files an issue for work it deliberately cut/thinned, carrying a `Source:` line
(the CLAUDE.md "nothing is silently dropped" discipline). Both are by design (the run must
never block on a human), but both grow the backlog with issues the human never chose to
track and often can't parse. This skill is that missing gate: walk each auto-filed issue
**one at a time**, explain it from its *source* (the reviewer's finding or the deferral's
rationale + the code it names, not just the terse issue body), and let the human decide
whether it is worth filing/tracking at all.

This skill only **reads** the run and **acts on GitHub** (close / relabel / comment), plus
— for process lessons — appends the human's decisions to the disposition ledger through
`orchestrator kb triage-dispose`. It never re-runs any stage. It is safe to run any time
after a run is terminal, and safe to re-run — it only ever surfaces issues that are still
**open** and process lessons with no recorded disposition, so a second pass skips
everything already triaged.

## Constants
- `RUN` = the run id to triage. Its logs are at
  `~/Development/runs/<project name>/<YYYY-MM-DD>/<RUN>/` (#523; `status` prints the exact
  dir on stderr) — the `<run dir>` the paths below refer to.
- `REPO` = the GitHub repo (`cperler/sous` for self-host; the project
  adapter's repo otherwise). `PROJECT` = the project adapter module.
- `HARNESS_REPO` = the harness's own tracker, where process-lesson work goes
  (`cperler/sous`; see "Process lessons" below).
- Read-only engine call shape (for the task list): `uv run orchestrator --run "$RUN"
  --project "$PROJECT" status`.

## Enumerate — the issues THIS run filed
Do not guess from time windows. A run auto-files issues from **two** provenance sources,
each with its own stable footer keyed to a run task id. Gate **both** — they are both
auto-filed without a human gate and both grow the backlog:

- **Review-seam issues** — the evidence-out seam files the review stage's `non_blocking`
  findings (UNLABELED) and the `improvement` idea (as `enhancement`). Footer:
  `Filed automatically from the <task_id> review`. Assigning a real label is part of triage.
- **Cut-scope issues** — a task that deliberately cuts/thins/finds-missing work files an
  ordinary issue for it (the CLAUDE.md "nothing is silently dropped" discipline). These do
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
     `<run dir>/stages/<task>/NN-review.json` (highest attempt) and find the matching
     entry: the `non_blocking[]` element whose `title` equals the issue title, or the
     `improvement` object. Show its full `detail` and, for a non-blocking finding, its
     `disposition` (`file`/`fixup`/`fix_now`/`drop`) — the reviewer's own words are far richer than
     the issue body.
   - **Scope-ledger deferral** — there is no review entry; the source IS the issue body's
     own `Source:` / why-deferred / trigger-to-revisit rationale (the implement agent
     authored it deliberately). Read it, and optionally the filing task's implement record
     `<run dir>/stages/<task>/NN-implement.json` for what shipped vs. what was cut, so you
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

## Process lessons — the harness's own retrospectives
Every REVIEW may emit a `retrospective` process lesson about the harness itself. The engine
keeps them in the project's learnings KB and files a `meta-authoring` issue only once one
lexical cluster spans two runs — so a lesson with no named target, worded uniquely, never
files and is otherwise read by nobody. This part of triage is where those lessons get a
human decision. Grouping them by meaning is your job here; the engine deliberately does not
(it never calls a model). Run it after the issue queue above, or on its own.

**Which tracker.** Process lessons are about the harness itself, so anything you file,
edit, or close for them goes to `HARNESS_REPO` — the engine's own tracker, where the
`meta-authoring` issues already live (`cperler/sous` unless `ORCHESTRATOR_ENGINE_PROJECT`
selects another engine adapter) — not the product repo. The KB is per-project, so run the listing for
the project whose runs produced the lessons.

### Enumerate
1. The deterministic queue (read-only):
   ```
   uv run orchestrator --project "$PROJECT" kb process-lessons
   ```
   (`--root <runs-root>/<project>` works too.) It returns:
   - `filed_issues[]` — each filed meta-authoring cluster's current issue `ref`, its
     `cluster_key`, `target`, and every KB `evidence` row behind it (run, task, ts, text).
   - `lessons[]` — every process lesson NO filing covers (held under the two-run floor),
     untargeted (`untargeted: true`, `cluster_key` `text:…`) first, each with `key`
     (`lesson:<kb id>`), run, task, ts, text, target.
   - `excluded` — how many were left out because triage already decided them
     (`handled_*`) or because a filed issue carries them (`covered_by_filed_issue`).
     Add `--include-handled` to see decided items again, each with its `disposition`.
2. The open issues: `gh issue list -R "$HARNESS_REPO" --label meta-authoring --state open
   --json number,title,body,url,comments`. Join on the issue number. A `filed_issues` entry
   whose issue is already closed needs no triage (the engine re-files if it recurs). An
   open `meta-authoring` issue absent from `filed_issues` came from another project's
   ledger or was filed by hand — triage it on GitHub as normal; there is nothing to record.
3. Report the queue size before starting: N open meta-authoring issues, M uncovered
   lessons (K untargeted).

### Meta-authoring issues — one at a time
For each open one: read its evidence (the KB rows plus any engine comments carrying later
evidence), then READ THE TARGET ARTIFACT at current `main` — stage templates in
`orchestrator/stages.py` (`STAGE_SPECS`), stage schemas in `orchestrator/schemas/stages/`,
personas in `.claude/agents/`, skills in `.claude/skills/*/SKILL.md`, scaffold assets in
`templates/project-default/`. Decide whether the complaint still holds (it may already be
fixed). Then recommend one of:
- **propose an edit** — name the file and the exact change (a diff or a precise
  before/after), not "tighten the wording". Apply CLAUDE.md's rule: *when the agent keeps
  violating a rule, the rule is not the fix*. Recurring evidence that a prompt clause is
  ignored argues for a structural fix (a check the engine runs, a schema field it cannot
  skip, a deterministic gate) or for DELETING the clause — never for restating it louder.
  Prefer subtractive edits when the evidence says a clause does nothing. On approval,
  either edit the issue body to carry the concrete proposal (so the ordinary pipeline can
  pick it up) or apply a trivial change directly and close it pointing at the commit.
- **merge** — it is the same problem as another open issue: comment the cross-reference and
  close the duplicate (`--reason "not planned"`, label `duplicate`).
- **close** — stale, already fixed, or not worth it; close with the reason as the comment.
- **keep** — leave it open as-is. It drops out of the queue until new evidence arrives.

### Uncovered lessons — group by meaning, then one group at a time
Read every `lessons[]` row (targeted and untargeted) and group them by what they are
ACTUALLY about — the same underlying problem worded differently in different runs is one
group; a lesson that fits an open `meta-authoring` issue joins that issue instead. Show the
human the proposed grouping (group → member `lesson:` keys + one-line gist) before working
through it, and let them regroup. Then, per group, the same brief as above (evidence, the
artifact it implicates, read at `main`, your honest read) and a recommendation:
- **file** — a group worth acting on becomes an ORDINARY issue on `HARNESS_REPO` with a
  standard label (`enhancement`, `bug`, `docs`, `chore`); add `meta-authoring` only when
  the human wants the change held before DELIVER like engine-filed ones. Title names the
  artifact and the change; the body states the concrete proposed edit (file + change) and
  cites every evidence row as `- kb:<id> — run <run>, task <task> (<ts>): <text>`. Those
  `kb:<id>` markers are also the crash-safety check: before filing, `gh issue list -R
  "$HARNESS_REPO" --state all --search "kb:<id>"` for the group's ids — if an earlier
  interrupted pass already filed it, record that issue instead of filing a second one.
- **merge** — fold into an existing issue: comment the evidence rows onto it.
- **apply** — a trivial, clearly-right edit: make it now and commit (no attribution
  trailer), then record the commit.
- **close** — not worth acting on, already fixed, or noise. Nothing on GitHub; the
  disposition's reason is the record.

### Record every decision — after the human approves, never before
Each decision is written to `triage-dispositions.jsonl` beside the KB (append-only; the KB
itself is never rewritten), so it does not come back on the next pass:
```
uv run orchestrator --project "$PROJECT" kb triage-dispose --lesson <kb id> [--lesson …] \
  --disposition filed|merged --ref <issue>
uv run orchestrator --project "$PROJECT" kb triage-dispose --lesson <kb id> \
  --disposition closed --reason "<why>"
uv run orchestrator --project "$PROJECT" kb triage-dispose --lesson <kb id> \
  --disposition applied --commit <sha>
uv run orchestrator --project "$PROJECT" kb triage-dispose --issue <#N|url> \
  --disposition kept|closed|merged|applied …
```
Do the GitHub action first, then record it (record the ref it produced). One call can
cover a whole group (repeat `--lesson`). An unknown id or ref fails the call and records
nothing; re-recording the same decision is a no-op. A `kept` (or any) issue decision
remembers the cluster's evidence count, so the issue reappears when the engine adds new
evidence to it.

## Close-out
When the queue is exhausted (or the human stops), print a short ledger: each issue and its
decision (kept / closed+reason / promoted / edited), each process-lesson group or
meta-authoring issue and its recorded disposition, and the count still open / still
undecided. That ledger
is the human-readable record of the gate — the mirror of the evidence-out seam that filed
them without a gate in the first place.

## Notes
- **Scope: actually-filed issues — plus any completion note that never reached a human.**
  Findings the reviewer marked `fix_now`/`drop`, and findings over the per-task filing cap,
  were **noted in the completion note, never filed** (#188). A `fixup` finding is also not
  filed, but for the opposite reason — the engine re-opened implement→…→review and APPLIED
  it (#414); the note reports whether it actually landed, so read that line rather than
  assuming either way. None of them are GitHub issues, so they are out of the
  issue-by-issue queue above: triage gates what was filed, not what was suppressed. But
  that reasoning assumes the note actually reached someone, and publishing it is a
  best-effort external call — on `batch-codex-3` every note failed and three valid
  `fix_now` findings reached nobody (#357) — findings that, at the time, the note also
  claimed had been "fixed in place" when nothing had applied them (#414). So **before**
  the queue, check
  `orchestrator status --run <RUN>`'s `completion_notes` block: if `clean` is false, read
  each undelivered note's `unfiled` findings (also inline in the `completion_note_failed`
  event, and the full note is at `<run dir>/stages/<TASK>/completion-note.md`) and walk
  those with the human too — they have no other channel. `persist_failed` means even the
  artifact is missing; fall back to `<run dir>/stages/<TASK>/NN-review.json`.
- **Never delete the run dir** while reading it (the durable audit trail). This skill
  only reads it.
- This is a human-judgment loop — you present and recommend; the human decides. Do not
  auto-close a batch of issues on your own read, even ones you think are junk. One at a
  time, with a decision each.
