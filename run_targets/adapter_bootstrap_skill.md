---
name: adapt-orchestration-pipeline
description: Stand up (or re-tune) the orchestration template for a project. Detect the stack, confirm a profile with the user, generate the adapter + seed the starter kit, and verify. Re-callable to tune an existing project from its run artifacts. The engine is never edited — only the adapter + the project's .claude/.
---

# Bootstrap a project for orchestration (the §5 interview)

> **Does the repo exist yet?** This skill assumes it does — it detects a stack, which means
> reading files that are already there. For a project that does not exist at all, use the
> **`new-project`** skill instead: it runs `orchestrator init-project` to write and verify a
> phase-0 skeleton (and create the GitHub repo, which the `task_source` guess below depends
> on), then walks this same detect → confirm → generate → verify flow.

You are the **bootstrap supervisor**. Standing up a project means composing a *profile*
(stack + commands + roster + task source) and letting the deterministic scaffold turn it
into a project-config adapter + a seeded `.claude/` starter kit. You detect and interview;
`orchestrator-scaffold` does the file generation. **Never edit `orchestrator/`** — if a
project concern seems to need an engine change, it belongs in the adapter.

Constants: `PROJECT` = the adapter/package name; `REPO` = the target project's root dir;
`ADAPTER` = where the adapter package lives — **default `$REPO/.orchestration`** (the
adapter is owned by the project's repo and loaded by path; pass `--dest` only to place it
in this repo under `adapters/project/` instead, e.g. for a reference adapter).

---

## A. First run — stand up a project

### 1. Detect (don't ask cold)
```
uv run orchestrator-scaffold --detect "$REPO" --name "$PROJECT"
```
This prints a **draft `profile.toml`**: detected `languages` (from `pyproject.toml` /
`package.json` / `tsconfig.json` / `go.mod` / `Cargo.toml`), `commands` (refined by the
detected package manager — uv/poetry/pip, pnpm/yarn/npm — and an `e2e` command only if a
`playwright.config.*` exists), a `roster` (stack implement agents + the generic
reviewers/test-validator), and a `task_source` guess (`github-issues` if a GitHub remote,
else `local-file`).

### 2. Confirm (detect-then-confirm)
Show the user the draft and use **AskUserQuestion** to correct it, not to answer from
scratch. Confirm/adjust:
- **Languages** — did detection miss or over-call a stack?
- **Commands** — are `test_unit` / `install` / `typecheck` / `test_e2e` actually how this
  repo builds and tests? (Check `package.json` scripts, Makefile targets, CI.)
- **Task source** — GitHub Issues, or a local `tasks.json`?
- **Roster** — keep the detected implement agent(s), or swap one.
Write the confirmed profile to a file, e.g. `/tmp/<PROJECT>-profile.toml`.

### 3. Generate
```
uv run orchestrator-scaffold --name "$PROJECT" --profile /tmp/<PROJECT>-profile.toml \
    --into "$REPO"
```
This writes the adapter into the **project's own repo** (`$REPO/.orchestration/`:
`profile.toml`, generated `config.py`, write-once `classifier.py` / `task_source.py`)
and **seeds the stack's kit** into `$REPO/.claude/` (agents, skills, example hooks).
`config.py` is a regenerated VIEW of `profile.toml` — hand-edits go in the profile (or
re-run), never in `config.py`. The generated `__init__.py` carries `CONTRACT_VERSION`;
the engine checks it at load and refuses a stale adapter loudly.

### 4. Finish what the profile can't infer (Keep / Modify / Replace / Delete)
- **`classifier.py`** (Replace) — map THIS project's test output to unit/e2e/shell failures
  and changed-file → impacted-tests. The generated default matches `^FAILED <name>`.
- **`task_source.py`** (Replace, only if `task_source = github-issues`) — swap the generated
  `LocalTaskSource` for a GitHub-Issues source; copy the shape from
  `adapters/project/selfhost/task_source.py`.
- **Hooks** (Modify) — the seeded `$REPO/.claude/hooks/*.json` are examples; merge them into
  the project's `.claude/settings.json` and adjust to its Claude Code version.

### 5. Verify
- Satisfies the contract (version + full ProjectConfig surface, no run needed):
  `uv run orchestrator --project "$REPO/.orchestration" validate`
- A stage resolves (and the roster/agent names resolve in `$REPO/.claude/agents/`): run the
  interactive supervisor loop (`/orchestrate-task-interactive`) or
  `--project "$REPO/.orchestration" run-headless` on a throwaway task, and confirm
  `status` shows `lane_audit.clean == true`.

---

## B. Re-run — tune an existing project

The scaffold is idempotent and additive, so re-running is safe. Use this after some real
runs, or when the stack grows.

### 1. Read the current state + the evidence
- The persisted `$REPO/.orchestration/profile.toml` (what's configured now).
- Run artifacts from a run's root dir, if present:
  - `retrospective.md` — recurring failure patterns (which stage/signature keeps failing).
  - `cost-report.md` — per-stage cost + the session-reuse win (which stages are cheap).
  - `events.jsonl` — the timeline.

### 2. Propose data-informed deltas (judgment — surface them, get approval)
- A stage that keeps failing the same way in `retrospective.md` → propose a stronger or
  different agent for that role (swap the roster entry), or note a missing classifier rule.
- A cheap, file-patching stage (`implement`/`test`) in `cost-report.md` → propose routing it
  to codex (a per-task `:codex` provider tag) to cut cost.
- A newly-added language → add it (its agents/commands roll in automatically).
- **Do not auto-apply.** Present the proposals; the human decides.

### 3. Apply additively
Write only the *delta* into a profile file (e.g. just the new language, or the swapped
roster entry) and re-run:
```
uv run orchestrator-scaffold --name "$PROJECT" --profile /tmp/<PROJECT>-delta.toml --into "$REPO"
```
The scaffold unions languages, re-derives defaults for new ones, and re-applies hand-
overrides — `classifier.py` / `task_source.py` are never clobbered, and only newly-selected
kit assets are seeded.

---

## Invariant
If you find yourself editing anything under `orchestrator/`, stop — a project concern leaked
into the engine. The fix is almost always in the adapter (`$REPO/.orchestration/`) or the
profile.
