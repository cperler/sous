# Do design-rationale docstrings speed up review? (#382)

**Date:** 2026-10-10 · **Task:** #382 · **Source:** REVIEW retrospective, `ff-v1-b8` task `#10`
(family-finance)

**Result: no effect. No IMPLEMENT prompt clause was added.** Across 146 reviews from runs
after the #381 fix, the share of a diff's docstrings that argue design choices does not
predict how long or how much the review took, once diff size, project, and reviewer model
are held fixed. The review that prompted the claim was fast for a different reason: it ran
during an earlier period when every family-finance review was roughly 7× faster per line
than reviews are now, and its own docstrings were ordinary for that project. IMPLEMENT
already writes almost all design-rationale docstrings without being told to, so a clause
would add tokens to every implement prompt and change nothing.

## The claim

The `ff-v1-b8` `#10` reviewer approved a 3,346-line, three-feature diff in 72 s for $0.67
and proposed: "front-load the 'why' in docstrings for design-heavy tasks, it pays for itself
at review time." That is a self-report from the stage whose speed is the evidence, with
n = 1. A fast zero-finding review is also what #381's false green looked like.

## Method

Everything below is computed from data on disk plus `gh pr diff` for each task's PR.

- **Corpus.** Every task with a recorded REVIEW in `stage-costs.jsonl` across
  `~/Development/{family-finance,prediction,sous}/runs/*` and `~/Development/runs/*/*/*`:
  203 reviewed tasks, each with its own merged PR.
- **#381 exclusion.** #381 (a review worktree executing a sibling worktree's code) was fixed
  by PR #387, merged 2026-08-13T02:55Z. Every run created before then is excluded from the
  primary analysis (22 tasks, `ff-v1-b2` through `ff-v1-b14`). Those runs include `ff-v1-b8`
  itself and all four of the named control-group tasks.
- **Other exclusions.** One interactive-lane review (its cost reads $0, so it is not
  metered), and diffs touching fewer than 50 lines of non-test Python source. Primary set:
  **n = 146** (100 family-finance, 39 prediction, 7 sous).
- **Review effort.** The FIRST review attempt's `duration_s` and `cost_usd`, which is the pass
  that read the diff fresh. Fix-cycle re-reviews are excluded because they re-read a diff the
  reviewer has already seen.
- **Diff size.** Added plus deleted lines in the PR, excluding lockfiles and snapshots.
- **Rationale density.** Docstring blocks added in non-test `.py` files that are at least 3
  lines long and contain a reasoning marker (`why`, `because`, `rather than`, `instead`,
  `so that`, `deliberate(ly)`, `intentional(ly)`, `on purpose`, `trade-off`, `otherwise`,
  `the point`, `chose/chosen`, `reason`, `would`, `cannot`, `never`). Density is the number of
  lines in those blocks per 100 changed non-test source lines. This is a keyword heuristic.
  On `#10` it finds the blocks the retrospective cites, such as the `superseded_by`
  migration docstring.
- **Did the review find something?** Any blocking issue, any non-blocking finding, or any
  rejection across the task's review attempts.
- **Post-merge misses (control group).** Later `bug` issues in the same repo that cite the
  task's issue number or PR, read by hand to keep only defects in that task's own code.
  Excluded: auto-filed review follow-ups (the review found those), integration-gate filings,
  and mentions that only name the task. Confirmed misses after the #381 fix: `#43`→#56 (not
  metered, so outside the primary set), `#146`→#152, `#212`→#317, `#317`→#326, and
  prediction `#115`→#236. Before the fix: the four named in the issue, `#3`→#30, `#5`→#31,
  `#17`→#32, and `#6`→#35. This is a lower bound, because a defect nobody filed against its
  source task is not counted.

## Findings

### 1. Rationale density does not predict review time or cost

Regression of log(first-review seconds or $) on log(changed lines) plus density:

| set | n | effect of +10 density points on time | effect on cost |
| --- | --- | --- | --- |
| post-#381, project + reviewer-model fixed effects | 146 | ×1.05 (95% CI ×0.97–×1.14) | ×1.00 (×0.94–×1.06) |
| post-#381, family-finance only (one project, 99/100 Sonnet 5) | 100 | ×0.99 (×0.92–×1.07) | ×0.97 |

Both confidence intervals include "no effect". Going from the 10th to the 90th percentile
of density (about 2 to 30 points), the largest review speed-up the data allow is about 8%
pooled, or 21% for family-finance alone. The best point estimate is no effect at all. Family-finance by density tertile:

| density tertile | n | median changed lines | median review s | median review $ | s / 100 lines | $ / 100 lines | found something | post-merge miss |
|---|---|---|---|---|---|---|---|---|
| low (< 15.2) | 33 | 574 | 360 | 2.80 | 54.5 | 0.415 | 28/33 | 2 |
| mid | 33 | 986 | 356 | 2.74 | 34.5 | 0.264 | 27/33 | 1 |
| high (≥ 23.2) | 34 | 895 | 322 | 2.67 | 39.8 | 0.314 | 25/34 | 0 |

The per-line gap between low and mid is a diff-size effect: review time has a large fixed
cost, so smaller diffs look slower per line. It disappears in the regression.

The pooled table WITHOUT fixed effects shows the opposite sign (×1.44 slower per +10
points). That is a project confound: prediction reviews run on Opus over code with low
density (median 6.9) and are fast per line (5.8 s/100), while family-finance runs on Sonnet
over code with high density (median 19.6) and is slower (43.6 s/100). Any analysis of this
question has to control for the project.

### 2. The `#10` review was fast because of when it ran, not because of its docstrings

| | rationale density | review s / 100 changed lines |
| --- | --- | --- |
| `ff-v1-b8` `#10` | 12.6 | 2.2 |
| family-finance, before #381 fix (n = 22) | median 9.3 | median 6.1 |
| family-finance, after #381 fix (n = 100) | median 19.6 | median 43.6 |

`#10`'s density is below the post-fix family-finance median. Under today's runs it would sit
in the LOW tertile. Its 2.2 s/100 lines fits the early period, when reviews were shallow
across the board: three of the four named control-group misses come from one run
(`ff-v1-b3b`) whose reviews took 58–83 s on diffs of 222–2,397 lines.

### 3. The confound: a fast review that found nothing is rare now

After the #381 fix, the fastest quartile of reviews per line (≤ 11 s/100 lines) found
something in 36 of 37 cases, with no post-merge misses. Reviews that found nothing are the
SLOW ones (median 63.5 s/100 lines against 24.3 for reviews that found something). That fits
a reviewer that works to establish there is nothing to find. "Fast and empty", the signature
the issue worried about, is the pre-fix pattern: there, the fastest quartile included 2 of
the 4 misses.

### 4. No sign that rationale docstrings make review more credulous

The issue raised the possibility that a confident narrative in the code discourages
independent checking. The four post-fix misses in the primary set have densities of 6.9,
10.3, 14.9 and 22.3 (median 12.6, against 15.6 for every other task). Three of the four
reviews found something else in the same diff. The pre-fix misses all sit at low density
(0.0–6.8). With n = 4 and 4 this cannot show an effect in either direction, but nothing
here suggests the failure mode is real.

## Ownership of design rationale: IMPLEMENT, already, in practice

Each stage's checkpoint commits were attributed to that stage (`git diff <prev checkpoint>
<stage checkpoint>` per stage, from `stages/<task>/NN-<stage>.json`). Across the 156
post-fix tasks with recorded checkpoints, IMPLEMENT and SIMPLIFY wrote **11,285** of the
**11,315** rationale-docstring lines (99.7%). DELIVER's docstring pass added any rationale in
**6 of 156** tasks, 30 lines in total.

The decision, recorded beside the stage specs in `orchestrator/stages.py` and in
`ARCHITECTURE.md`:

- **IMPLEMENT owns design rationale.** It is the only stage that made the design choices,
  and it already writes the "why" unprompted. No prompt clause is needed to get behaviour
  that already happens 99.7% of the time.
- **DELIVER's docstring pass owns API description.** That means contracts, params,
  raises, and refreshing docs the diff made stale. It runs at low effort after the fact, so
  it can describe what the code does but cannot recover why a choice was made. The
  `docstring-writer` agent's "explain the why" line is left as is: it covers the obvious
  why of a contract, and the data shows it does not compete with IMPLEMENT's rationale.
  Removing it would be a separate, unmeasured prompt change.

## What would reopen this

A new claim of this kind should come with a measurement, not another retrospective. The
cheapest test that could still overturn the result is the same regression rerun on a larger
post-fix corpus, ideally with a second project on the same reviewer model. Until then, an
IMPLEMENT clause would cost tokens in every implement prompt for an effect the data puts at
roughly zero.
