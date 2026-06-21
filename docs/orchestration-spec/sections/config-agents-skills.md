# Section: Agents + Config + Skills

Sources (Hey Soo! reference repo, read-only):
- `/Users/craigperler/Development/heysoo/.claude/agents/` (5 agent files)
- `/Users/craigperler/Development/heysoo/.claude/settings.json`
- `/Users/craigperler/Development/heysoo/.claude/settings.local.json`
- `/Users/craigperler/Development/heysoo/.claude/hooks/session-start.sh`
- `/Users/craigperler/Development/heysoo/.claude/skills/` (20 skills total; 11 wired, 9 non-wired)
- Dispatch cross-reference: `implement-orchestrator.sh`, `batch-orchestrator.sh`, `orchestrator-common.sh`
- `inventory/skill-wiring.txt:11-33`

---

## 1. Agent Roster

Five agent definitions under `.claude/agents/`. No agent declares tools in its frontmatter; all inherit the full session tool set.

### Tool policy
No agent uses a `tools:` frontmatter key. Per Claude Code behavior, all inherit the active session's tool set. This means every subagent has access to the same tools as the orchestrating session — an intentional design choice (agents need Bash, Read, Write, etc. to implement tasks). The generic rebuild should note this explicitly rather than relying on undocumented defaults.

### Roster, model pins, and pipeline dispatch

| Agent | Model pin | Dispatched by stage(s) | Cite |
|-------|-----------|------------------------|------|
| `python-backend-developer` | none (inherits session default) | Default `--agent` for setup/research/evaluate/plan; default for backend implement tasks; default fix agent in unit-test fix loop, test-validate, full-suite halts | `implement-orchestrator.sh:668` (research), `:702` (evaluate), `:772/:778/:795` (plan), `orchestrator-common.sh:3979/4238` (unit-fix), `orchestrator-common.sh:4300` (validate) |
| `bulletproof-frontend-developer` | none (inherits session default) | Frontend implement tasks; E2E-failure fix loop; tsc-gate fix loop | `implement-orchestrator.sh:1066` (policy reminder branch); `orchestrator-common.sh:1891/1966` (E2E fix), `orchestrator-common.sh:3728` (tsc-gate) |
| `spec-reviewer` | none (inherits session default) | Per-task review gate inside the implement loop ("subtask N of M" reviews) | `implement-orchestrator.sh:1226` |
| `code-reviewer` | `model: inherit` (explicit) | Code-review loop (`run_stage "review-…"`); batch PR review | `orchestrator-common.sh:3422`; `batch-orchestrator.sh:672/704/710` |
| `cc-orchestration-writer` | `model: opus` (explicit pin) | **ORPHAN author-agent — 0 pipeline dispatch.** No `--agent cc-orchestration-writer` call exists anywhere in `.claude/scripts/`. Writes the orchestration scripts themselves; also the target of `improvement-loop`'s repair routing. | grep: zero hits in `.claude/scripts/` |

**Note on `cc-orchestration-writer`:** The README/plan should classify it as "author" not "pipeline". Its dispatching happens via human interaction and the `improvement-loop` skill, not via any stage in `implement-orchestrator.sh` or `batch-orchestrator.sh`.

### Default-agent override mechanism
The `--agent` value on the orchestrator CLI (`implement-orchestrator.sh:73`, `:116`) is the *default* agent for stages that don't hard-code one. The `plan` stage emits per-task `agent` fields that override it (`:795`), constrained to `python-backend-developer` or `bulletproof-frontend-developer`. Stage dispatch is assembled via `run_stage`'s agent argument, which builds `agent_args=(--agent "$agent")` (`orchestrator-common.sh:2933`).

### Anti-thrash approval threshold
Both `code-reviewer` and `spec-reviewer` encode an approval-threshold rule: approve when all remaining issues are `suggestion`-level; request changes only on `critical`/`important` (`code-reviewer.md:117-122`; `spec-reviewer.md:160-176`). This prevents review→fix loops from never terminating on nitpicks.

### Coupling: generic vs. Hey Soo!-specific

| Agent | Coupling | Generic shape |
|-------|----------|---------------|
| `code-reviewer` | Mostly generic core (severity-tiered `issues[]` output, approval threshold). Leakage: Pydantic+DynamoDB `model_dump(mode="json")` rule (`:75-86`), Tailwind component-extraction (`:51-74`), `extra="forbid"` contract audit (`:87-104`). | Keep severity model + comprehensive-first-pass + approval threshold; move stack-specific rules into a project overlay/skill. |
| `spec-reviewer` | Generic. Goal-vs-scope-creep reviewer; no stack assumptions. Soft coupling: `.php` example paths (`:144-145`), names caller skill `implement-issue` Step 9 (`:222`). | Already a clean generic template; parameterize the caller name and drop `.php` examples. |
| `cc-orchestration-writer` | Generic-but-self-referential. Codifies this harness's own conventions (bash style, JSON-via-printf, stdout/stderr discipline, BATS CODECHECK, rate-limit handling). References `implement-orchestrator.sh` as the reference impl (`cc-orchestration-writer.md:1080`). Not product-coupled. | Content is essentially a prose version of the engine contract. Keep as the canonical convention doc. Note: Python rebuild supersedes the bash-specific rules. |
| `python-backend-developer` | Heavily Hey Soo!-specific (lambda-coupled). Hard-wired to `lambda/suggest/` layout (`:11-20`), Bedrock/DynamoDB/Pydantic-v2, ADRs 003/004/005/007/015 (`:41-47`), precision-domains, two-layer safety (`:78-91`). | Generic shape = "backend implementer for stack X"; AWS/Bedrock/ADR specifics belong in a project profile, not the agent core. |
| `bulletproof-frontend-developer` | Product/frontend-specific. React/TS/Tailwind; hard-codes Hey Soo! light-only theme ADR-053 (`:371-390`, `:568-573`, `:640-650`), `frontend/src/` layout, skills `bulletproof-frontend`/`ui-design-fundamentals` (`:666-682`). | Generic shape = "frontend component craftsman"; theme/ADR/skill/path bindings are the product overlay — adapter drop-in. |

---

## 2. Config Surface

### settings.json hooks

All hook timeouts cited from `settings.json` line numbers in the reference system.

**PostToolUse hooks (after Edit/Write/Bash):**

| Hook | Trigger | Behavior | Timeout | Coupling |
|------|---------|----------|---------|----------|
| Format hook | Edit/Write on `.py`, `.[jt]sx?` | `cd $CLAUDE_PROJECT_DIR/lambda/suggest && uv run ruff format` for `.py`; `cd …/frontend && npm run format` for JS/TS/JSX/TSX (`:21`) | 30s (`:22`) | Hey Soo!-specific paths (`lambda/suggest`, `frontend`); mechanism is generic |
| E2E selector check | Edit/Write on `tests/e2e/*.ts` | `scripts/check-e2e-no-id-selectors.sh` (`:26`) | 10s (`:27`) | Project-specific lint hook |
| Shellcheck | Edit/Write on `*.sh` | `shellcheck` (`:31`) | 15s (`:32`) | Generic; reusable as-is |

**PreToolUse hooks (before Bash/Edit/Write):**

| Hook | Trigger | Behavior | Timeout | Coupling |
|------|---------|----------|---------|----------|
| `fetch-usage.sh` | Every tool call (`PreToolUse :43`) and on Stop (`:85`) | `~/.claude/fetch-usage.sh` — user-global usage metering | — | Generic; depends on a user-level script outside the project tree |
| Env/secret guard | Edit/Write where path contains `.env`, `.git/`, `credentials`, `package-lock.json` | `exit(2)` — vetoes the tool call (`:52`) | 5s (`:53`) | Generic safety rail |
| Deploy guard | Bash where any token contains `deploy_to_production` | `exit(2)` — vetoes the tool call (`:62`) | 5s (`:63`) | Semi-specific; configurable denylist of dangerous command tokens |

**Notification hook:**
- `notify-send` on session events (`:74`) — generic desktop notify (Linux); no-op fallback. Reusable.

**Other settings.json config:**
- `ENABLE_LSP_TOOL=1` env var (`:2-13`) — stack-coupled (py/ts/go); harness-generic mechanism.
- `enabledPlugins`: pyright, typescript-language-server, gopls — per-project plugin list.
- `statusline-command.sh` reference (user-global, outside project tree; unverified here).

### settings.local.json

**Env vars:**
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (`:3`) — harness-generic experimental flag enabling multi-agent teams. Reusable.

**Permission allowlist:**
- **Heavily Hey Soo!-specific and machine-specific.** Contains absolute `/Users/craigperler/Development/heysoo/...` paths, project script names, AWS service verbs, playwright configs/ports. Nearly every entry is project/machine-local. Do NOT template verbatim.
- **Polluted with accidental captures** (anomaly; see §Anomalies below): `kill 34441` (`:107`), `kill 18823 18821` (`:109`), playwright spec lines with ports (`:79-86`), literal error string `Bash(could not be validated". Use direct assignment to fall back to the:*)` (`:96`), `Bash(__NEW_LINE_c6fb609e03344d98__ node --check /tmp/main_script.js)` (`:44`). These are transient artifacts — do not carry into the rebuild.
- **Mixed allow-scope:** broad globs (`Bash(git:*)` `:69`, `Bash(bash:*)` `:55`, `Bash(uv run:*)` `:38`) alongside hyper-narrow exact one-liners (`:91-95`). The broad ones make the narrow ones redundant — net effect is near-unrestricted Bash. Generic rebuild should choose a deliberate, minimal allowlist.

### Config anomalies

1. **Two different env-injection homes:** `settings.json` sets `ENABLE_LSP_TOOL`; `settings.local.json` sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`. Splitting harness env across global+local files is fine but worth flagging for the rebuild's config model.
2. **Hooks reference user-global scripts** (`~/.claude/fetch-usage.sh`, `~/.claude/statusline-command.sh`) not in the project tree — outside the reference scope and could not be fully verified.

---

## 3. Session-Start Hook

**File:** `/Users/craigperler/Development/heysoo/.claude/hooks/session-start.sh`

**Mechanism:** At every session start (SessionStart event), the hook reads `${PROJECT_ROOT}/.claude/skills/using-skills/SKILL.md` (`:12`) and emits its full content as a `hookSpecificOutput.additionalContext` JSON payload wrapped in `<EXTREMELY_IMPORTANT>` (`:36-43`). This is the injection point for the `using-skills` skill bootstrap.

**Control flow:**
- `set -euo pipefail` (`:5`)
- Linear: resolve `SCRIPT_DIR` (`:8`) → `PROJECT_ROOT` two levels up (`:9`) → read SKILL.md with `|| echo "Error reading using-skills skill"` fallback (`:12`) → char-by-char JSON escape loop `for ((i=0; i<${#input}; i++))` (`:19-30`) → emit heredoc JSON (`:36-43`) → `exit 0` (`:45`)
- Always exits 0; missing SKILL.md degrades gracefully to an inline error string rather than aborting the session.

**Why this matters:** Without this hook, the `using-skills` skill (which establishes the "invoke a skill before ANY response, even 1% chance" rule) would not fire, and no other skill would self-invoke. This hook is the load-bearing bootstrap for the entire skill system.

---

## 4. Wired Skills (11)

The 11 wired skills fall into four wiring classes. All wiring claims cite `absolute-path:line` as verified in fragment 11.

### 4a. Hook-Injected

**`using-skills`** — injected into every session by `session-start.sh:12`; not loaded via the Skill tool. The full SKILL.md bytes are pushed into context at session start, wrapped in `<EXTREMELY_IMPORTANT>`. Establishes the "invoke a skill before ANY response, even 1% chance" rule (`H/skills/using-skills/SKILL.md:6-12,23-24`) that makes all other skill wiring fire. Generic (load-bearing); only the hook path is project-relative.

### 4b. Agent-Referenced

**`ui-design-fundamentals`** — **PRODUCT-SPECIFIC (web/CSS) — adapter drop-in candidate.** Referenced by `bulletproof-frontend-developer.md:23`: "consult the `ui-design-fundamentals` skill. It provides concrete values for the 8pt grid, type scales, WCAG contrast...". Pure design reference; no agents dispatched by the skill itself. `adapting-claude-pipeline/SKILL.md:112,221` lists it under "Delete if not web." For a web target, the design-values doc is a drop-in; delete for non-web targets.

**`test-driven-development`** — Generic. Referenced by `python-backend-developer.md` "TESTING APPROACH" section (~`:219`): "Follow **test-driven-development** skill patterns." Also referenced in `subagent-driven-development/SKILL.md:248`. Any subtask routed to `python-backend-developer` inherits red-green-refactor discipline (Iron Law: "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST", SKILL.md:33). The agent is the project-specific piece; the skill is generic.

### 4c. Script-Referenced

**`writing-plans`** — Named in the orchestrator's plan-stage prompt: `implement-orchestrator.sh:793` — `"1. Write a detailed implementation plan using writing-plans skill"`. The only `writing-plans` ref in `scripts/`. Governs plan shape: bite-sized TDD steps, required header with **Feature Branch** (SKILL.md:42), save path `docs/plans/YYYY-MM-DD-<feature>.md` (SKILL.md:16). Also required as a human entry-point by `adapting-claude-pipeline/SKILL.md:167`. The skill requires `subagent-driven-development` to execute the plan it writes (SKILL.md:106). Generic with minor coupling (the agent enum in the orchestrator is project-specific).

**`ralph-loop`** — Drives `ralph-loop.sh` directly. `H/skills/ralph-loop/SKILL.md:19` launches `.claude/scripts/ralph-loop.sh --tasks "$TASK_IDS" --branch $BASE_BRANCH`. Ralph is a *scheduler around* the single-task orchestrator: `ralph-loop.sh:1408` invokes `"$SCRIPT_DIR/implement-roadmap-task-orchestrator.sh"` per task (DAG + concurrency + retry-with-learnings). Status surface: `status-ralph.json`, `ralph-queue.json`, `$LOG_BASE/stages/index.md`, `$LOG_BASE/context/stage-costs.jsonl` (SKILL.md:95, 170-177, 289). Mode flags (`--micro/--lite/--full`) and provider levers (`ORCHESTRATOR_PROVIDER`, per-task `:codex`) change which stages each child orchestrator runs (SKILL.md:35-44, 54-91). Generic scheduler; GitHub-issue task source and `roadmap.md` are project-specific.

**`implement-roadmap-task`** — Drives `implement-orchestrator.sh` directly (verified correction: the SKILL.md invokes `implement-orchestrator.sh` at `H/skills/implement-roadmap-task/SKILL.md:54,59,67,83,177,204,207`). `implement-roadmap-task-orchestrator.sh` is a 632-byte backward-compatible wrapper that `exec`s the unified script (`H/scripts/implement-roadmap-task-orchestrator.sh:3-7,19`) — both names resolve to the same engine. Ralph still calls the wrapper name (`ralph-loop.sh:1408`), so both aliases are live. Stage map (SKILL.md:136-148): extract→setup→research→evaluate→plan→implement→test→docs→pr→review→complete, with per-stage agents (`test`→`python-backend-developer`, `docs`→`phpdoc-writer`, `review`→`spec-reviewer`+`code-reviewer`). Pre-flight (SKILL.md:17-44) can refuse to launch if the issue/task is already done. Generic orchestrator; per-stage agent map is project-specific → adapter.

**`improvement-loop`** — **Inverted wiring direction:** the *script emits a recommendation to run this skill* after pipeline failure, rather than the human invoking it to drive a script. Refs: `orchestrator-common.sh:800` (comment), `:812` (recommendation comment), `:845` (`printf -- 'Run \`/improvement-loop\` to analyze this failure...'`), `:851` (log message). The skill then edits skills/agents/hooks/scripts (`H/skills/improvement-loop/SKILL.md:111-118` classification table; routing at :160-168 dispatches `cc-orchestration-writer` / `bash-script-craftsman`). Gate (SKILL.md:51-77) mandates the original issue be resolved first. This is the pipeline's closed self-repair feedback loop: script failure → human runs improvement-loop → edits pipeline → next run. Generic core; routing targets are project-specific.

### 4d. Entry-Point / Methodology

**`adapting-claude-pipeline`** — THE BOOTSTRAP. See §5 below for in-depth treatment. 0 script refs; human-invoked at clone time. The skill that produces the entire adapted `.claude/` that the other stages then run inside.

**`dispatching-parallel-agents`** — Fan-out methodology; 0 script refs. `H/skills/dispatching-parallel-agents/SKILL.md:66-72` shows the `Task("...")` dispatch pattern. Invoked by a human (or controlling skill) to fan out independent `Task(...)` calls. Generic; no named agent definitions. Referenced by `adapting-claude-pipeline/SKILL.md` (Phase 5, parallel-safe workstreams).

**`subagent-driven-development`** — In-session plan execution; 0 script refs, 3 cross-skill refs. No pipeline-script reference. Referenced by `writing-plans/SKILL.md:106` ("REQUIRED: Use subagent-driven-development") and `adapting-claude-pipeline/SKILL.md:189` (Phase 5). Routes by task type → `python-backend-developer` (backend) and `bulletproof-frontend-developer` (frontend) (`H/skills/subagent-driven-development/SKILL.md:104-108`); plus reviewer subagents via `./implementer-prompt.md`, `./spec-reviewer-prompt.md`, `./code-quality-reviewer-prompt.md` (SKILL.md:96-98). This is the *human/Claude-driven* analogue of what `implement-orchestrator.sh` does in shell (fresh subagent per task + 2-stage review). The orchestrator is the as-built implementation; this skill is the manual playbook. Generic core; the agent-selection table is project-specific → adapter.

**`bulletproof-frontend`** — **PRODUCT-SPECIFIC (web/CSS) — adapter drop-in candidate.** Wiring clarification: of the "30 refs" cited in `inventory/skill-wiring.txt:21`, only **2 reference the skill directly**: `H/prompts/frontend/refactor-blade-thorough.md:57` and `H/agents/bulletproof-frontend-developer.md:669`. The remaining ~28 refs are the *agent* `bulletproof-frontend-developer` (dispatched by `select-incremental-test-agent.sh:31`, `orchestrator-common.sh` E2E/tsc-gate fix loops, `implement-orchestrator.sh`, `batch-orchestrator.sh`). The skill governs CSS implementation patterns the frontend agent applies; the *agent* (carrying both this skill and `ui-design-fundamentals`) is what the scripts actually invoke. `adapting-claude-pipeline/SKILL.md:92,221` lists it as the canonical "Delete if not web" example. Delete for non-web targets; drop in for web targets.

---

## 5. adapting-claude-pipeline — Bootstrap Mechanism (In Depth)

This skill is the §5 bootstrap: it is invoked by a human at clone time to produce the entire adapted `.claude/` that pipeline stages then run inside. It is load-bearing for the template. 0 pipeline script refs; it sits at the top of the skill dependency graph, composing `writing-plans`, `subagent-driven-development`, `dispatching-parallel-agents` (and `brainstorming`) into the bootstrap workflow.

**Core principle (SKILL.md:12):** "every file must earn its place" — Delete aggressively (SKILL.md:255).

### 6-Phase workflow (`H/skills/adapting-claude-pipeline/SKILL.md:22-39`)

**Phase 1 — Brainstorm** (SKILL.md:43-59):
REQUIRED `brainstorming` skill. Elicits the target's tech stack, project type, workflows, pain points, scope. This is where adapter values originate — test commands, formatter, language. The output feeds all subsequent phases.

**Phase 2 — Research domain patterns** (SKILL.md:61-81):
WebSearch for `[lang] best practices/anti-patterns/testing/security`. Findings feed agent anti-pattern sections, skill content, hook logic, and orchestration stage design for the target project.

**Phase 3 — Audit existing inventory** (SKILL.md:83-165):
Bucket every `.claude/` file into **Keep / Modify / Replace / Delete**. Tables enumerate all 19 skills (note: stale — reference system now has 20), 10 agents, hooks, settings, scripts, prompts with a default decision each. This is the inventory the adapter overrides. The audit tables and Common Adaptation Patterns (SKILL.md:217-243) — especially "Web to CLI/Library" at `:219` — are the exact mapping the template's project-config adapter formalizes. The audit table is stale relative to the current skill set (predates the ralph/unified-orchestrator era; missing `ralph-loop`, `implement-roadmap-task`, etc.); will need regeneration when the template's adapter is built.

**Phase 4 — Write the plan** (SKILL.md:166-185):
REQUIRED `writing-plans` skill. Organizes work into parallel workstreams A–F: delete / modify / new skills / new agents / scripts / hooks+settings.

**Phase 5 — Execute with subagents** (SKILL.md:187-204):
REQUIRED `subagent-driven-development`. Routing table (SKILL.md:193-203): script edits → `cc-orchestration-writer`; hook creation → `bash-script-craftsman`; new skills/agents → `writing-skills`/`writing-agents`; parallel-safe workstreams use `dispatching-parallel-agents`.

**Phase 6 — Verify** (SKILL.md:206-216):
Glob for orphaned files; grep for dangling refs to deleted skills/agents; validate `settings.json` hooks; audit skill descriptions + agent deferral graph; mental dry-run.

**Why load-bearing:** Phase 3's audit tables + Common Adaptation Patterns are the exact mapping the template's project-config adapter must formalize — what to keep generic vs. what to drop in per target. The skill's "Delete aggressively" design rule is what the adapter encodes. It composes the other wired entry-point skills into the bootstrap and is therefore the root of the skill dependency graph.

---

## 6. Non-Wired Skills (9) — Disposition Catalogue

All 9 confirmed at 0 pipeline references per `inventory/skill-wiring.txt:24-33`.

| Skill | Intent | Disposition Tag | Notes |
|-------|--------|-----------------|-------|
| `brainstorming` | Collaborative dialogue to turn a raw idea into a fully-formed design: one question at a time, 2–3 approaches with trade-offs, 200–300-word sections for incremental validation, then writes a doc and hands off to writing-plans. (SKILL.md:3-11) | **methodology-port** | Pure process; no product-specific paths, model names, or stack assumptions. |
| `executing-plans` | Loads a written plan, critiques it, executes tasks in batches of ~3 with a review checkpoint per batch; gate requires all tasks complete before handing off to `finishing-a-development-branch`. (SKILL.md:1-10) | **methodology-port** | Batch-with-checkpoint loop is generic. Concrete coupling: references `bash .claude/scripts/test-unit.sh` (SKILL.md:52) and REQUIRED SUB-SKILL `finishing-a-development-branch` (SKILL.md:63) — neither in `skill-wiring.txt` pipeline scan. `finishing-a-development-branch` is not in the 20-skill inventory (may be dropped or outside scanned dir). **Flag for re-check.** |
| `investigating-codebase-for-user-stories` | Seven-phase investigation to reverse-engineer user stories with acceptance criteria and code-reference traceability. (SKILL.md:9-12) | **superfluous/stale** | Hardcoded heysoo paths (`lambda/*/handler.py`, `frontend/src/pages/`) at SKILL.md:65-70; 0 invocations; too product-contaminated to port cleanly. |
| `review-ui` | Reviews React/TS components by dispatching parallel `bulletproof-frontend-developer` agents (one per UI design fundamentals section), then compiles a prioritised executive summary. (SKILL.md:9-11) | **product-specific** | Structurally depends on `bulletproof-frontend-developer` agent + `ui-design-fundamentals` (both product-wired). 0 direct pipeline refs but effectively agent-referenced via cross-skill coupling. Cannot port until adapter strategy for those two is settled. |
| `systematic-debugging` | Four-phase discipline: root-cause investigation → pattern analysis → hypothesis & test → implementation. Iron law against proposing fixes before completing root-cause analysis; stop conditions after three failed attempts; architectural escalation path. (SKILL.md:9-12) | **methodology-port** | Entirely language/stack-agnostic; embedded examples are illustrative. High-value carry-over, no blockers. |
| `using-git-worktrees` | Creates isolated git worktrees under `.worktrees/`, verifies gitignore safety, installs language-appropriate dependencies, runs baseline test suite, reports worktree location before feature work. (SKILL.md:9-11) | **methodology-port** | Core isolation pattern is generic; React/TypeScript + Lambda setup block (SKILL.md:40-57) is a heysoo-specific example (auto-detect fallback covers other stacks). Path convention `.worktrees/` must match new project CLAUDE.md. |
| `write-docblocks` | Batch-processes Python docstring gaps: finds undocumented functions, dispatches `python-docstring-writer` subagents in parallel batches of five, verifies coverage with grep or interrogate. (SKILL.md:9-12) | **superfluous/stale** | Hardcoded to `lambda/` path and `python-docstring-writer` agent (SKILL.md:44-49, 63); trivially replaceable by a one-liner prompt. |
| `writing-agents` | Full agent-authoring cycle: research domain best practices via WebSearch, gather codebase context, write file with persona/scope/anti-patterns/coordination protocols/model selection, apply RED-GREEN-REFACTOR test loop, then deploy. (SKILL.md:9-13) | **methodology-port** | Entirely generic; product references (Laravel, heysoo agent names) are illustrative examples only. No blockers. |
| `writing-skills` | TDD applied to process documentation: RED (run baseline scenario without the skill) → GREEN (write minimal skill) → REFACTOR (close loopholes). Guidance on CSO, token budgets, flowcharts, rationalization-resistant phrasing. (SKILL.md:9-16) | **methodology-port** | Entirely meta/tooling-level; no product dependencies. Foundational; governs all subsequent skill authoring. High priority. |

### DEFERRED.md seed rows

| Item | Why deferred | Earliest phase | Trigger to revisit |
|------|-------------|----------------|-------------------|
| `brainstorming` | No engine wiring needed; keep/refine/drop is Phase 2 scope | Phase 2 | When the new harness's skill directory is initialised and `writing-skills` is ported. |
| `executing-plans` | References `finishing-a-development-branch` (not in scope map); `test-unit.sh` path needs parameterisation; reconcile with `implement-roadmap-task` (wired) | Phase 2 | When the new engine's plan-execution flow is designed. |
| `investigating-codebase-for-user-stories` | Product path contamination (`lambda/`, `frontend/`) must be purged; 0 invocations | Phase 3 | If the new project ever needs user-story reverse-engineering; otherwise drop. |
| `review-ui` | Hard dependency on `bulletproof-frontend-developer` + `ui-design-fundamentals`; cannot port until adapter strategy settled | Phase 3 | After the adapter-drop-in decision for those two skills is resolved in Phase 2. |
| `systematic-debugging` | High-value methodology; no blockers | Phase 2 | When the new skill tree is initialised; strong candidate for first-batch carry-over. |
| `using-git-worktrees` | heysoo React+Lambda setup block must be removed or generalised; `.worktrees/` path must match new CLAUDE.md | Phase 2 | When the new engine's CLAUDE.md is drafted and development workflow is established. |
| `write-docblocks` | Hardcoded `lambda/` paths and `python-docstring-writer` agent; likely drop unless new project has same structure | Phase 3 | If the new Python engine grows to warrant batch docstring work; otherwise drop. |
| `writing-agents` | No blockers; generic methodology | Phase 2 | When the new engine's agent directory is initialised. |
| `writing-skills` | No blockers; foundational meta-skill; governs all subsequent skill authoring | Phase 2 | Immediately when the new skill tree is initialised — highest priority. |

---

## 7. Cross-Cutting Anomalies and Flags

1. **`cc-orchestration-writer` orphan confirmed.** Zero `--agent cc-orchestration-writer` calls in `.claude/scripts/`. Intentional (meta/author agent) but classify explicitly as "author" not "pipeline" in the rebuild's README.

2. **`bulletproof-frontend` "30 refs" = 2 skill + 28 agent refs.** The inventory's ref count overstates direct skill wiring. The skill is wired only transitively (the `bulletproof-frontend-developer` agent carries it). See §4d for the corrected breakdown.

3. **`implement-roadmap-task` SKILL drives `implement-orchestrator.sh` directly.** `implement-roadmap-task-orchestrator.sh` is a 632-byte live wrapper alias (`exec`s the unified script, `H/scripts/implement-roadmap-task-orchestrator.sh:19`). Ralph calls the wrapper (`ralph-loop.sh:1408`); the SKILL.md calls `implement-orchestrator.sh` directly. Both are live; both reach the same engine. The wiring map's script name (`implement-roadmap-task-orchestrator.sh`) is the deprecated alias; the as-built skill drives `implement-orchestrator.sh`.

4. **`adapting-claude-pipeline` audit table is stale.** SKILL.md:96 says "19 skills"; reference system has 20. The audit table omits `ralph-loop`, `implement-roadmap-task`, `improvement-loop` (partially), and others added in the ralph/unified-orchestrator era. Flag for regeneration when the template's adapter is built.

5. **`executing-plans` more wired than evidence suggests.** Explicitly references `bash .claude/scripts/test-unit.sh` (SKILL.md:52) and REQUIRED SUB-SKILL `finishing-a-development-branch` (SKILL.md:63). Neither surfaces in pipeline scans. `finishing-a-development-branch` is absent from the 20-skill inventory — may be dropped or live outside the scanned directory. Confidence: low; flagged for targeted grep before Phase 2 disposition is finalized.

6. **`improvement-loop` inverts the normal wiring direction.** Unlike every other script-referenced skill (human → skill → script), here the script emits a recommendation to run the skill (`orchestrator-common.sh:845,851`). The as-built model: script failure → human runs improvement-loop → edits pipeline → next run. Structurally unusual; document explicitly in the rebuild's architecture overview.

7. **Settings env split across global+local.** `ENABLE_LSP_TOOL` in `settings.json`; `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` in `settings.local.json`. Fine mechanically but worth a deliberate config model in the rebuild.

8. **`spec-reviewer` `.php` example paths** (`:144-145`) — stale copy from a prior project; harmless but a coupling smell (the agent claims to be stack-agnostic).

9. **`session-start.sh` model inference.** Agents without a `model:` frontmatter key inherit the session model — documented Claude Code behavior, but not explicitly stated in the assigned files. Treat as inference, not file-cited fact.
