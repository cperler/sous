---
name: batch-finished
description: After a batch run ends, do the whole merge-out in one pass — review each task's outcome and the integration gate, diagnose failures, compose every task branch over trunk and verify the composite, merge the PRs in dependency order, check issue closures, remove the run's worktrees and branches, sync local and remote main, run the post-merge trunk gate, then hand off to triage-followups. Invoked by the human as `/batch-finished [run-id]`; the invocation is their go to merge.
---

# Batch finished — review, merge, clean up, sync, triage

The human ran a batch and it has stopped. This skill is the routine that follows every batch,
so they can say `/batch-finished` instead of listing the steps. **Invoking it is the human's go
to merge this run's PRs** (CLAUDE.md: merges stay human; this is the human deciding). It is not
a go to merge anything outside the run, to force through red verification, or to delete run
logs. Stop and ask at the points marked **STOP**.

## Constants
- `RUN`: the argument, else the newest run dir under `~/Development/runs/<project>/<date>/`
  whose `driver.jsonl` has an exit record. Say which run you picked.
- `PROJECT`: the adapter path recorded on the run (`project_ref` in `init-run`'s output, or the
  `--project` in `driver.jsonl`'s start record `argv`).
- `REPO`: the project checkout (the adapter's parent directory for a `.orchestration/` adapter).
- `RUN_DIR`: the run's log dir. **Never delete or rewrite anything in it.**
- `CLI`: `uv run orchestrator --run "$RUN" --project "$PROJECT"`, run from the sous checkout.

## 1. Read the outcome
1. Confirm the driver is gone: `CLI status` → `driver.alive` false and an `exit_reason`. If it is
   alive, **STOP**: the batch is not finished.
2. From `status`: each task's `state` and `pr_url`, `progress`, `cost.total_cost_usd`, and
   `completion_notes`. **`completion_notes.clean` must be true.** On `delivery_invalid` or
   `undelivered`, a task's last commit never reached its PR; recover it (push the branch tip or
   cherry-pick onto a recovery PR) before any cleanup, because cleanup makes it unreachable.
3. `RUN_DIR/batch-integration-gate.json`: `green`, `failing` (which merge or command was red,
   with its output tail), `skipped_tasks` (failed tasks, decomposition umbrellas), and `filed`
   (the bug it opened).
4. `RUN_DIR/events.jsonl`: `task_decomposed` (umbrella → child issues), `followup_filed`,
   `improvement_not_filed`, `review_fixup_*`, `test_failed_fix_cycle`, and any warning-grade
   event. A decomposed umbrella has no branch or PR; its children carry the code.
5. For each **failed** task, read why: its `status-<RUN>-#N.json` `context.summary` and the
   failing stage's `stages/<N>/*.md` / `*.stream.jsonl`. Classify it:
   - **nothing to do**: the work was already on trunk, or the issue is moot. Close the issue
     with a comment naming the commit or PR that already did it.
   - **work on the branch**: a timeout or a breaker trip with the change complete. It needs a
     follow-on run or hand-finishing; leave its branch alone and list it for the human.
   - **real failure**: report it and leave the issue open.
6. Report a short outcome table to the human: task, result, PR, review fix cycles, and anything
   notable. Then continue. **STOP** only if a failed task has unpushed work you cannot recover,
   or a finding needs the human's decision before merging.

## 2. Check the PRs
For every PR: `gh pr view N --json baseRefName,state,mergeable,headRefName`.
- A base other than trunk (a `task/*` branch, sous#667): retarget it with `gh pr edit N --base main`
  before anything else, or "merging" it only updates the other branch and `Closes` never fires.
- Note PRs that must leave their issue open (`keep_issue_open` on the task, or the project's
  keep-open labels such as `experiment`), so you don't close those issues by hand later.

## 3. Compose and verify the whole batch, once
The pre-merge gate stops at the first conflict, so the whole composite may never have been tested.
1. In `REPO`: `git fetch`; check `git status` is clean on trunk, then fast-forward local trunk.
2. `git worktree add -b compose/<RUN> <scratchpad>/compose origin/main`.
3. Merge each task branch (`origin/task/<N>`) in the gate's `branches` order, which is
   dependency order. After each step record the compose commit (`COMPOSE[N]`): it is the
   resolution you will reuse in step 4.
4. On a conflict, read both sides and resolve. Do not just take one side: understand what each
   branch meant (see the conflict shapes below). Commit the merge and continue.
5. Run the project's verification argv from the gate's `commands` list (or the adapter's
   install and verification commands): install, then lint, types, and the full suite. Unset
   `VIRTUAL_ENV` first (sous's venv leaks in). Run this in the background; it takes minutes.
6. Red: fix it in the composite if the cause is the composition. A test that checks real-clock
   timing and fails once under `-n auto` is a flake only if it passes run alone AND ~12 parallel
   copies at once, on both the composite and trunk; say so and carry it to triage. If a single
   task branch is wrong, **STOP** and report; don't merge a red batch.

## 4. Merge in order
For each PR in the same order:
1. Ask whether it merges cleanly into current `origin/main` (`gh pr view N --json mergeable`).
   The flag goes stale after any push, reading `UNKNOWN` or even `CONFLICTING` for up to a
   minute. If the branch already contains `origin/main` (`git merge origin/main` says "Already up
   to date"), re-query every 10 s rather than resolving again.
2. If it conflicts: in the task's own worktree (`REPO/.worktrees/<N>`, else a fresh one on
   `task/<N>`), `git merge origin/main`, then take the resolved files from the composite:
   `git diff --name-only --diff-filter=U -z | xargs -0 git checkout COMPOSE[N] --` (zsh does
   not split a `$FILES` variable). Then `git diff --stat COMPOSE[N] HEAD`: anything other than
   commits people landed on trunk meanwhile is a file the composite's resolution also changed
   that git auto-merged differently (e.g. a registry file the resolution added a line to), so
   take it from `COMPOSE[N]` as well. Run that area's tests, commit, and push.
3. `gh pr merge N --merge`, then `git fetch`, then do the next PR.
4. After the last merge, verify that `origin/main`'s tree matches the composite's
   (`git diff COMPOSE[last] origin/main --stat` is empty, or differs only by commits other
   people landed meanwhile).

## 5. Close the loop on issues
- For every issue a PR claimed: `gh issue view N --json state`. Close any left open by hand
  (a `Closes #a, #b` list closes only the first), except keep-open ones.
- A decomposition umbrella: comment with the child issues and PRs. Close it only when merging
  the children met its done-when; if a step the code can't do remains (turn a setting on in a
  release, check against an outside schedule, read data after a release), leave it open and say
  what is left.
- The gate's `filed` bug: if step 3 resolved and verified the composition, close it with a
  comment saying how it was resolved (which conflict, which commit).
- Failed tasks classified "nothing to do" in step 1: closed with a comment.

## 6. Clean up
Only after every merge has landed and `completion_notes.clean` holds:
1. For each task: confirm `git merge-base --is-ancestor origin/task/<N> origin/main`. Then
   `git worktree remove REPO/.worktrees/<N>`, `git branch -D task/<N>`, and
   `git push origin --delete task/<N>`. Skip and list any branch that is not merged (a failed
   task's leftover work).
2. Remove the compose worktree and its branch, and delete the run's local checkpoint tags:
   `git tag -l 'task/<RUN>/*' | xargs git tag -d` (check `git ls-remote --tags origin` too;
   they are normally local only).
3. Check for stray processes the run left (`pgrep -fl "orchestrator-review|pytest"` under the
   repo's worktrees) and report them; kill one only if it belongs to this run.
4. Leave `RUN_DIR` alone. Leave worktrees from other runs and other sessions alone too; list
   them for the human if they look stale.
5. In zsh, `for n in $LIST` does not split a variable: use an explicit list.

## 7. Sync trunk
1. In `REPO`: `git checkout main && git pull --ff-only`. If local main has commits that are not
   on origin, push them (`git push origin main`) only if they are the human's own committed work
   and the push is a fast-forward; otherwise **STOP** and ask.
2. Run the post-merge gate on the merged checkout: `CLI trunk-gate -C "$REPO" --no-file-fix`.
   Report red; don't file from here unless the human asks.

## 8. Triage
Hand off to the `triage-followups` skill for `RUN` (invoke it with the run id). It walks the
issues the run filed and the review lessons one at a time with the human.

## Final report
A few lines: what was merged (PR numbers), what is still open and why (failed tasks, keep-open
issues), what was cleaned, the trunk-gate result, and the run's cost. Use plain words.

## Conflict shapes seen before
- **A ledger or log file every task appends to** (prediction's `docs/STATE.md` header chain):
  keep both sides' entries, dedupe, and keep the newest on top.
- **Per-change pin tests** (prediction's money-path pins in `tests/taker/test_release10_money_path.py`):
  two tasks pinning the same module each wrote "base plus my patch". Re-base the later pin on the
  earlier branch's tip and regenerate its patch and sha with the test's own helper.
- **Pin-chain registries** (prediction's `tests/taker/feed_pins.py` `UNDO`, #1805): when one task
  converted hand-nested `before_NNNN(...)` chains to the registry and another appended its own
  step to every chain, keep the registry version of each file and add the new step to the top
  of `UNDO`.
- **Two tasks adding the same mechanism differently**: unify them rather than keeping both, and
  expect the later task's tests to assume the earlier one never happened.
