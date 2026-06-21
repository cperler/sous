# Fragment: Non-Wired Skill Disposition Catalogue (Phase 1)
Source: /Users/craigperler/Development/heysoo/.claude/skills/{name}/SKILL.md  
Pipeline refs confirmed: 0 for each skill (per skill-wiring.txt lines 24–33)

---

## Catalogue Table

| Skill | Intent (1–2 sentences) | Disposition Tag | Rationale |
|-------|------------------------|-----------------|-----------|
| **brainstorming** | Guides a collaborative dialogue to turn a raw idea into a fully-formed design: asks one question at a time, proposes 2–3 approaches with trade-offs, presents the design in 200–300-word sections for incremental validation, then writes a doc and hands off to writing-plans. (SKILL.md:3–11) | methodology-port | Pure collaborative process with no product-specific paths, model names, or stack assumptions; applies to any project. |
| **executing-plans** | Loads a written implementation plan, critiques it before starting, then executes tasks in batches of ~3 with a review checkpoint after each batch, enforcing a gate that requires all tasks complete before handing off to finishing-a-development-branch. (SKILL.md:1–10) | methodology-port | The batch-with-checkpoint loop and finishing gate are engineering process; the only concrete coupling (test-unit.sh) is a path reference, not structural. |
| **investigating-codebase-for-user-stories** | Runs a seven-phase investigation (identify user types → map features → trace journeys → write stories → validate coverage → map dependencies → save files) to reverse-engineer well-formed user stories with acceptance criteria and code-reference traceability from an existing codebase. (SKILL.md:9–12) | superfluous/stale | Contains hardcoded heysoo paths (`lambda/*/handler.py`, `frontend/src/pages/`) at SKILL.md:65–70; the technique itself is generic but the document is too product-contaminated to port cleanly, and no active pipeline invokes it. |
| **review-ui** | Reviews React/TypeScript components and CSS by dispatching parallel bulletproof-frontend-developer agents—one per UI design fundamentals section—then compiles findings into a prioritised executive summary with severity ratings. (SKILL.md:9–11) | product-specific | Structurally depends on bulletproof-frontend-developer agent and the ui-design-fundamentals skill tree (both product-wired); the review domains and reference file paths are heysoo-specific. |
| **systematic-debugging** | Enforces a four-phase discipline (root-cause investigation → pattern analysis → hypothesis & test → implementation) with an iron law against proposing fixes before completing root-cause analysis, including explicit stop conditions after three failed attempts and an architectural escalation path. (SKILL.md:9–12) | methodology-port | Entirely language/stack-agnostic; the embedded examples (shell, CI) are illustrative, not structural. High-value carry-over. |
| **using-git-worktrees** | Creates isolated git worktrees under `.worktrees/`, verifies gitignore safety first, installs language-appropriate dependencies, runs the baseline test suite to confirm a clean starting state, and reports the worktree location before any feature work begins. (SKILL.md:9–11) | methodology-port | The core isolation pattern is generic; the React/TypeScript + Lambda setup block (SKILL.md:40–57) is a heysoo-specific example, not a structural constraint—the skill's own auto-detect fallback covers other stacks. |
| **write-docblocks** | Batch-processes Python docstring gaps by finding undocumented functions, dispatching python-docstring-writer subagents in parallel batches of five, and verifying coverage with grep or interrogate. (SKILL.md:9–12) | superfluous/stale | Hardcoded to `lambda/` path convention and the python-docstring-writer subagent (SKILL.md:44–49, 63); the technique is trivially replaceable by a one-liner prompt and adds no methodology value to the new engine. |
| **writing-agents** | Teaches the full agent-authoring cycle: research domain best practices via WebSearch, gather project-specific codebase context, write a file with clear persona/scope/anti-patterns/coordination protocols/model selection, then apply a RED-GREEN-REFACTOR test loop before deploying. (SKILL.md:9–13) | methodology-port | The framework (persona, scope, anti-patterns, TDD cycle) is entirely generic; the only product references are illustrative examples (Laravel, heysoo agent names) that would be swapped for new-project examples. |
| **writing-skills** | Applies TDD to process documentation: run a baseline scenario without the skill (RED), write the minimal skill that fixes the failures (GREEN), close loopholes found in testing (REFACTOR), with detailed guidance on CSO (Claude Search Optimisation), token budgets, flowchart usage, and rationalization-resistant phrasing. (SKILL.md:9–16) | methodology-port | Entirely meta/tooling-level guidance with no product dependencies; immediately applicable to skill authoring in the new Python engine's `.claude/skills/` tree. |

---

## DEFERRED.md Seed Rows

| Item | Why deferred | Earliest phase | Trigger to revisit |
|------|-------------|----------------|-------------------|
| brainstorming | No engine wiring needed; carry-over decision (keep/refine/drop) is Phase 2 scope | Phase 2 | When the new harness's skill directory is initialised and the writing-skills skill is ported. |
| executing-plans | References `finishing-a-development-branch` (not in scope map); test-unit.sh path needs parameterisation; keep/refine decision is Phase 2 | Phase 2 | When the new engine's plan-execution flow is designed; reconcile with implement-roadmap-task (wired). |
| investigating-codebase-for-user-stories | Product path contamination (lambda/, frontend/) must be purged or abstracted; low priority given 0 invocations | Phase 3 | If the new project ever needs user-story reverse-engineering; otherwise drop. |
| review-ui | Hard dependency on bulletproof-frontend-developer + ui-design-fundamentals (both wired/product); cannot port until adapter strategy for those two is settled (see skill-wiring.txt:20–21) | Phase 3 | After the adapter-drop-in decision for bulletproof-frontend and ui-design-fundamentals is resolved in Phase 2. |
| systematic-debugging | High-value methodology; no blockers except Phase 2 keep/refine decision | Phase 2 | When the new skill tree is initialised; strong candidate for first-batch carry-over. |
| using-git-worktrees | The heysoo React+Lambda setup block (SKILL.md:40–57) must be removed or generalised; worktree path convention (`.worktrees/`) needs to match new project CLAUDE.md | Phase 2 | When the new engine's CLAUDE.md is drafted and development workflow is established. |
| write-docblocks | Hardcoded lambda/ paths and python-docstring-writer agent; likely drop unless new project has same structure | Phase 3 | If the new Python engine project grows enough to warrant batch docstring work; otherwise drop. |
| writing-agents | No blockers; generic methodology; Phase 2 keep/refine decision | Phase 2 | When the new engine's agent directory is initialised. |
| writing-skills | No blockers; foundational meta-skill; Phase 2 keep/refine decision; high priority | Phase 2 | Immediately when the new skill tree is initialised — this skill governs all subsequent skill authoring. |

---

## Anomalies / Flags for Re-check

### More wired than evidence suggests?

**executing-plans** — Warrants a second look. The skill explicitly references `bash .claude/scripts/test-unit.sh` (SKILL.md:52) and cross-references `finishing-a-development-branch` as a REQUIRED SUB-SKILL (SKILL.md:63). Neither of those surfaces in skill-wiring.txt's pipeline grep (which scanned scripts/, agents/, hooks/, prompts/). However, `finishing-a-development-branch` is not in the 20-skill inventory at all, suggesting it may live outside the scanned skill directory or was dropped. The test-unit.sh reference is a concrete script path that implies coupling to heysoo's test harness. Confidence: low enough to flag, not high enough to reclassify without a targeted grep.

**review-ui** — Already tagged product-specific. But its dependency on `bulletproof-frontend-developer` (SKILL.md:10, 107) means it is effectively agent-referenced via cross-skill coupling, even though no script directly invokes it. The evidence map's 0-refs count is accurate for pipeline wiring but understates the indirect coupling. No reclassification warranted; noted for Phase 2 adapter planning.

### DISPUTED

None. All 9 skills confirmed at 0 pipeline references per skill-wiring.txt lines 24–33. Disposition tags confirmed against SKILL.md content. No conflicting evidence found.
