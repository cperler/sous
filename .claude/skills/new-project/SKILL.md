---
name: new-project
description: Stand up a brand-new project for the harness end to end — interview, create the repo skeleton, create the GitHub repo, generate the project adapter, finish what the profile can't infer, verify, and hand off to spec-intake. The phase 0 + phase 1 front door for a project that does not exist yet; use adapter_bootstrap_skill.md instead when the repo already exists.
---

# New project — idea → a repo the harness can drive

You are the **phase 0 + phase 1 supervisor**. Nothing upstream of this turns *no repo at
all* into a project the harness can be pointed at. You own the conversation and the
oversight; the deterministic `orchestrator init-project` and `orchestrator-scaffold`
commands own the file generation. **Never edit `orchestrator/`** — a project concern
belongs in the project's adapter.

Use `run_targets/adapter_bootstrap_skill.md` instead when the repo already exists and only
needs an adapter. This skill is for the greenfield case, and it ends by handing off to
`/spec-intake` — it does **not** build the product.

## Constants
- `NAME` = kebab-case project name. `PARENT` = the dir the project is created inside
  (e.g. `~/Development`). `REPO` = `$PARENT/$NAME`. `ADAPTER` = `$REPO/.orchestration`.

---

## 1. Interview — short, and only what changes the outcome

Ask the few questions whose answers change what gets written. Don't interrogate.

- **Name and location.** Confirm `$NAME` and `$PARENT`. `init-project` normalizes what a
  human types (`"Prediction Markets"` → `prediction-markets`) and refuses what cannot be a
  package or repo name, so offer the normalized form back rather than demanding a format.
- **One-line description.** It lands in `pyproject.toml` and the README. Worth getting
  real: the `scope` stage reads the repo for context on every future run.
- **Stack.** Python is the only skeleton today (`orchestrator init-project --stack`
  lists what exists). If the human wants another, say so plainly rather than improvising a
  skeleton by hand — a hand-rolled tree is exactly the unverified boilerplate this replaces.
- **GitHub repo + visibility.** Creating it is outward-facing, so it is **opt-in and
  confirmed**, never assumed. Default to `private`.

## 2. Create the skeleton (deterministic — run it via Bash)

```
uv run orchestrator init-project "$NAME" --into "$PARENT" \
    --description "..." --create-repo --visibility private
```

Preview first with `--dry-run` (writes nothing, prints the resolved plan) whenever the
human wants to see the tree before it exists.

What it does, in order: writes the skeleton, `git init` + an initial commit, runs the
skeleton's own verification commands, and **only on green** creates the GitHub repo and
pushes. Exit is non-zero if any step it was asked to do failed.

**Report what came back rather than assuming success.** In particular:

- `verified: true` means `uv sync` / `pytest` / `ruff` / `mypy` actually ran and passed in
  the new repo — not merely that a config file mentioning them was written.
- On red, `verify[]` names the failing command and carries its output. Fix the repo before
  going near phase 1: those exact commands become the adapter's contract, so a skeleton
  that cannot pass them would give the harness gates it can never satisfy. **Do not
  work around a red verify by dropping the command from the profile in step 3.**
- On red, no GitHub repo was created. That is deliberate, not a partial failure to clean up.

## 3. Generate the adapter

**Order matters: the repo must exist on GitHub before you detect.** `--detect` guesses
`task_source` from the presence of a GitHub remote — run it on a remote-less repo and it
writes `task_source = "local-file"`, which is the wrong answer for an issue-driven project
and easy to miss in the draft.

```
uv run orchestrator-scaffold --detect "$REPO" --name "$NAME"
```

Writes nothing; prints a draft `profile.toml`. Show it to the human and use
**AskUserQuestion** to correct it — detect-then-confirm, not answer-from-scratch. Confirm
languages, the commands, the task source, and the roster.

Then generate:

```
uv run orchestrator-scaffold --name "$NAME" --profile /tmp/$NAME-profile.toml --into "$REPO"
```

This writes `$ADAPTER` (`profile.toml`, generated `config.py`, write-once `classifier.py` /
`task_source.py`) and seeds `$REPO/.claude/`. `config.py` is a regenerated VIEW of
`profile.toml` — hand-edits go in the profile, never in `config.py`.

## 4. Finish what the profile cannot infer

Two files need real work. Do them WITH the human, explaining what each is for — these are
the parts a generated default gets wrong silently.

- **`task_source.py`** — the generated default reads a local JSON file. For
  `task_source = "github-issues"`, swap in a real GitHub-Issues source; copy the shape from
  the engine repo's `adapters/project/selfhost/task_source.py`.
- **`classifier.py`** — maps this project's test output to unit/e2e/shell failures (the
  engine retries those differently) and changed-file → impacted tests. The generated default
  matches `^FAILED <name>`, roughly right for pytest. It cannot be tuned properly until real
  failures exist, so say that: it is a first pass, revisited after the first red run.

Seeded `$REPO/.claude/hooks/*.json` are examples — merge them into the project's
`.claude/settings.json`.

## 5. Verify the adapter

```
uv run orchestrator --project "$ADAPTER" validate
```

Duck-checks the full `ProjectConfig` surface and the contract version with no run needed.
Do not proceed past a failure here.

## 6. Hand off — do NOT start building

Phase 0 and 1 are done; the product gets built through the harness, task by task.

- **The human has a shape in mind** → `/spec-intake`. This is the normal path, and a
  one-line idea ("ingest market data, flag mispricings") is already enough to decompose.
- **The human genuinely has no shape yet** → `/brainstorm`, with a caveat you must state:
  its evidence sources are the codebase, the backlog, and run history, and on a brand-new
  project all three are empty. It will diverge on the idea space alone and its output is
  correspondingly weaker. Prefer spec-intake whenever there is any shape at all.

Then point at `USING.md` phases 3–5 for the run and post-run loop.

## Notes
- **Everything outward-facing is confirmed first**: creating the GitHub repo (step 2) and
  filing issues (step 6, owned by the downstream skill). Same rule as every other front door.
- The CLI half works standalone from a bare terminal — a human who wants no session at all
  can run `orchestrator init-project` directly. This skill adds the interview and the
  phase-1 hand-off, not capability.
- `init-project` refuses to write over a non-empty dir, and never overwrites a file it owns
  even under `--force`. If the human already made a folder with notes in it, `--force`
  writes alongside them.
