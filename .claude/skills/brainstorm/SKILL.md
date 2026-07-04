---
name: brainstorm
description: Turn a fuzzy area or goal into a ranked shortlist of concrete, scored ideas and hand the human's picks off — small ones filed as standalone enhancement issues, larger ones fed to spec-intake. You run the divergent exploration and the idea authoring over the real codebase, backlog, and run history; deterministic `orchestrator brainstorm` code ranks and files. The front door ABOVE intake — ideas out, not issues-from-a-known-idea and not code.
---

# Brainstorm — a fuzzy area → divergent ideas → ranked shortlist → filed picks

You are the **front door above the front door**. `spec-intake` turns a *known idea* into
dependency-ordered issues; `batch-plan` turns *already-filed issues* into a scheduled batch.
Neither invents the idea. This skill does: it takes a fuzzy area/goal, **diverges** (explores
the codebase, the issue backlog, and run history to generate N candidate ideas), then
**converges** (a deterministic ranked shortlist the human picks from) and hands off. You own
the exploration and the idea authoring; the deterministic `orchestrator brainstorm` commands
own ranking and filing. **Filing is outward-facing: the human selects from the shortlist
before anything is created.**

## Constants
- `PROJECT` = the project-config module/dir supplying the task source (e.g. `adapters.project.heysoo`).
- Command shape: `uv run orchestrator [--project "$PROJECT"] brainstorm <validate|capture> <file> [flags]`.

## The flow
1. **Clarify the area.** Pin down the fuzzy goal in one or two questions — what outcome are we
   chasing, what's explicitly out of bounds. Don't interview; just enough to aim the divergence.
2. **Gather evidence (READ-ONLY).** Explore for opportunities. Good sources:
   - **The backlog:** `uv run orchestrator --project "$PROJECT" batch-plan candidates
     [--label X]` (or `gh issue list`) — what's already filed, what clusters, what's stale.
     Don't re-propose something already tracked; build on it or say why it's insufficient.
   - **The codebase:** read structure, TODOs, rough edges near the area. Cite real paths.
   - **Run history (if present):** retrospectives / learnings under `runs/<run>/` — recurring
     failure patterns and the self-improvement loop's filed ideas are rich seams. Read only.
3. **Diverge — the model work.** Generate **N candidate ideas** (aim wide, ~5–10; quantity
   before judgment). Each idea needs: a concrete **problem** (the gap/pain), a **proposal**
   sketch (how you'd address it), and honest **impact** (high/medium/low), **effort**
   (small/medium/large), **risk** (low/medium/high), plus **evidence** pointers (file paths,
   issue refs, run-log observations) that ground it. Cheap wins and moonshots both belong here.
4. **Write the session JSON** to a file (schema: `orchestrator/schemas/brainstorm.json`):
   ```json
   {
     "area": "reduce batch-run cost",
     "ideas": [
       { "title": "Cache install layers across tasks",
         "problem": "every task in a batch re-runs `uv sync` from cold — minutes of wall time",
         "proposal": "hash the lockfile set, reuse a warm venv across a run's worktrees",
         "impact": "high", "effort": "small", "risk": "low",
         "evidence": ["adapters/execution/install_cache.py", "runs/2026-07-02-*/stage-costs.jsonl"] }
     ]
   }
   ```
5. **Converge — rank and show the shortlist.** `uv run orchestrator brainstorm capture <file>`
   prints the deterministic ranked shortlist: **impact descending, then effort ascending
   (cheaper first), then risk ascending (safer first)**; ties keep authored order. The score is
   named-weight math, not a model opinion — it's reproducible from the file. Present the
   shortlist to the human with your read on the top few.
6. **STOP for the human's pick.** Filing opens real issues. Do **not** file until the human
   names which ranks to take. (`brainstorm validate <file>` schema-checks without printing.)
7. **Hand off per pick.** For each selected idea, route by size:
   - **Small / self-contained** → file it as a standalone enhancement issue:
     `uv run orchestrator --project "$PROJECT" brainstorm capture <file> --file-selected 1,3`
     (add `--dry-run` first to preview). Each files with the `brainstorm` label and a body
     carrying its problem/proposal/evidence plus a **provenance line** — the rationale travels
     with the issue instead of vaporizing in this conversation. `--file-selected` takes the
     **1-based shortlist ranks** exactly as printed.
   - **Larger / multi-task** → don't file a lone issue; carry the idea into **`spec-intake`**
     as the raw idea. It decomposes into a dependency-ordered spec and files the batch. (A
     brainstorm idea and a spec are different altitudes — one enhancement vs. a whole feature.)

## Notes
- **Three producers, one scheduler.** `brainstorm` (fuzzy area → ideas) sits above `spec-intake`
  (a known idea → a batch of issues) and `batch-plan` (existing issues → a scheduled DAG). Each
  hands the next stage its input; don't skip the altitude that fits the request.
- **Durable + replayable.** The session JSON is the record: `brainstorm capture` re-prints the
  exact same ranked shortlist from it any time, and filing is a separate, explicit step. Keep
  the file (e.g. under a scratch dir) so a brainstorm doesn't evaporate.
- **Lineage.** The old bash system surfaced innovation ideas only as a free-text
  `innovation_brainstorm` reflection stapled to a PR at task completion. This rebuilds the
  capability as a structured, standalone front door; the per-run reflection it also served now
  lives in the retrospective + the self-improvement loop's issue-filing.
- `validate` / `capture`-print are pure (no `--project`); only `capture --file-selected` (real,
  not `--dry-run`) needs the task source. You explore and author ideas; the deterministic code
  ranks and files. Don't re-implement scoring or filing here.
