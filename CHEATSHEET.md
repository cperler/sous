# Cheat sheet

One page. `USING.md` is the walkthrough; this is the lookup.

Throughout: `RUN` = run id, `PROJECT` = `<repo>/.orchestration` (or a dotted module for an
in-repo adapter). Most commands take the same prefix:

```bash
O="uv run orchestrator --root runs --shared-root --run $RUN --project $PROJECT"
```

`--shared-root` is a no-op once run-nesting exists, so it is safe on every call — and
omitting it on a fresh `runs/` is a real footgun. Include it always.

---

## Skills

| Skill | Use it for | Produces |
|---|---|---|
| `/brainstorm` | A fuzzy area, no specific idea yet | Ranked scored ideas → issues or a spec |
| `/spec-intake` | A known idea → decompose and file | Dependency-linked issues + a spec archive |
| `/batch-plan` | Issues that already exist, edges unknown | A validated plan applied to a run |
| `/orchestrate-batch-headless` | **Ordinary batches — the default** | Real cost, session-free |
| `/orchestrate-batch-interactive` | Only when watching stages live | Records `$0.00`, burns session context |
| `/orchestrate-task-interactive` | One task, watched live | Same caveat |
| `/triage-followups` | After a run, walk auto-filed issues | Keep / close / promote / edit |

Producer rule: `/spec-intake` for a **new idea** (it authors the edges as it files);
`/batch-plan` when the issues **already exist** and only need analysis + lane wiring.

---

## Phase 0 — repo skeleton (once, by hand)

Done when these three exit clean in the new repo:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Needs: a GitHub remote, a detectable stack, one passing test.

## Phase 1 — adapter bootstrap (once, re-tuned later)

```bash
uv run orchestrator-scaffold --detect "$REPO" --name "$NAME"          # prints a draft profile, writes nothing
uv run orchestrator-scaffold --name "$NAME" --profile p.toml --into "$REPO"
uv run orchestrator --project "$REPO/.orchestration" validate
```

Then finish by hand: `task_source.py` (swap in GitHub Issues — copy
`adapters/project/selfhost/task_source.py`) and `classifier.py` (map your test output to
unit/e2e/shell failures).

Never hand-edit `config.py` — it is a generated view of `profile.toml`.

## Phase 2 — idea → issues

```bash
uv run orchestrator spec validate spec.json            # pure, no --project
uv run orchestrator spec plan spec.json                # pure, no --project
uv run orchestrator --project "$PROJECT" spec file spec.json --dry-run
uv run orchestrator --project "$PROJECT" spec file spec.json
```

Recover a filed batch later: `gh issue list --label spec:<slug>`.

## Phase 3 — issues → a run

```bash
$O init-run --lane full --budget-usd 25
$O add-task --task "#7"                                 # depends_on comes from the task source
$O dispatchable --util auto --max-concurrent 3          # check the DAG and that limit > 0
```

With lane pins or inferred edges, use `/batch-plan` instead of `add-task`:

```bash
$O batch-plan candidates --label spec:<slug>
$O batch-plan validate plan.json
$O batch-plan apply plan.json --dry-run
$O batch-plan apply plan.json
```

## Phase 4 — run it

```bash
$O run-headless --wait
```

Monitor from a **second terminal**:

```bash
$O watch --activity
$O dashboard --watch
$O tail <task> --follow
```

Result gate:

```bash
$O status
```

- `run_state` → `completed` / `failed`
- `lane_audit.clean == true`
- `events_audit` → `dispatched == recorded` (non-zero `outstanding` = orphaned leases)
- `driver` → `alive`, `heartbeat_age_s`, `last_state`, `exit_reason`

## Phase 5 — after the batch

```bash
$O trunk-gate -C /path/to/merged-trunk        # exits non-zero on red; files one remediation task
uv run orchestrator --project "$PROJECT" spec conformance ./specs/<slug>.json
$O retrospective
$O cost-report                                 # --by-effort splits by stage/effort/model
$O gc --repo "$REPO"                           # dry-run; add --prune to delete checkpoint tags
uv run orchestrator kb add
```

Then `/triage-followups`.

---

## Lane selection (`--pipeline`)

| Lane | For | Note |
|---|---|---|
| `micro` | Docs-only, pure config | Add `--deterministic-stages test,deliver` |
| `lite` | Small, mechanical, localized | |
| `full` | Risky, cross-cutting, ambiguous | Default |

Optional per-task: `--model fable` (architecture-heavy design work), `--effort high`.
Via `/batch-plan` these are **plan fields** — hand-writing `add-task --model` costs you
`apply`'s topological ordering.

---

## Recovery

| Situation | Command |
|---|---|
| Driver was killed mid-run | Re-invoke the **same** `run-headless` — resumes the same attempt, spends no retry budget |
| Run PAUSED (budget or circuit breaker) | `$O unpause [--raise-budget N]` |
| Task parked at the approval gate | `$O approve --task "#7" --by craig --note "..."` |
| Park a task for sign-off | `$O hold --task "#7" --reason "..."` |
| Dispatch died, lease stuck | `$O abandon --task "#7" --reason "..."` |
| Issue body changed mid-run | `$O refresh-spec --task "#7"` |
| Run superseded by the human | `$O retire` |
| Interactive run out of context | `$O park-supervisor` → `resume-supervisor` in a fresh session |

`blocked_on_orphaned_dispatches` on exit means leases it may not reclaim (another live
driver, or a foreign host) — not a finished run. Terminal escape hatch is `abandon`.

---

## Gotchas

**Run-level settings must be set at `init-run`.** Every subcommand rebuilds the engine from
defaults, so anything not stored on the Run doc is gone by the next command: `--lane`,
`--budget-usd`, `--review-workflow`, `--max-filed-followups`, `--progress-comments`,
`--route-by-cost`, `--route-by-capacity`, `--cross-provider-fallback`, `--warm-retry`.

**Backgrounded drivers die on a ~30-minute boundary.** Tracked background tasks are reaped on
a wall-clock-aligned schedule, so a driver's life is uniformly 0–30 minutes. Anything longer
runs detached or in a foreground terminal.

**A multi-issue `Closes #7, #8` only closes the first.** Verify issue closures by hand after
merging.

**Never `rm -rf runs/<run>/`.** Worktrees, branches, and checkpoint tags are cleanup targets;
the run log dir is the durable audit trail and is pruned only by a human, deliberately.

**Merge PRs in dependency order.** They were built on a DAG.

**An all-codex batch needs the global `--provider codex` flag.** A per-task `provider_tag`
routes only IMPLEMENT/TEST/SIMPLIFY, and `lane_audit.clean` will not catch the miss. DELIVER
is vetoed onto the deterministic engine lane regardless — the codex sandbox turns its
`git push` into a keychain prompt no unattended batch can answer.

**Interactive lanes record `$0.00`.** The Workflow shim cannot report usage. Use headless
unless you specifically need to watch.

**Don't add a DAG edge for work that merely could run in sequence** — it serializes what
would otherwise parallelize. And if two tasks' fixes touch the same code region, fold them
into one task rather than edging them.

**`trunk-gate` verifies only the checkout you point it at.** You must ensure the merged-trunk
checkout at `-C` exists; a missing path reports red and files nothing rather than silently
gating the wrong tree.

**Commits carry no model attribution trailer.** Per-stage provenance lives in
`runs/<run>/events.jsonl` and `stage-costs.jsonl`.
