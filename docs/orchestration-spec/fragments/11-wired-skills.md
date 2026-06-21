# Fragment: 11 WIRED skills
Source: `/Users/craigperler/Development/heysoo/.claude/skills/{...}/SKILL.md` (read-only reference system)
Mapped against: `scripts/`, `agents/`, `hooks/`, evidence in `inventory/skill-wiring.txt`

> Scope note: This fragment adapts the standard template — it focuses on §1 (role/entry-point),
> §5 (invocations/wiring), §8 (coupling), §9 (anomalies). The §2/§3/§4/§6/§7 mechanics of the
> *scripts* these skills drive belong to the script fragments; here we map skill→pipeline wiring only.
> **Hard rule honored:** every wiring claim cites `absolute-path:line` and was verified by reading that line.

Base paths (abbreviated below):
- `H = /Users/craigperler/Development/heysoo/.claude`

---

## §1. Role & entry points (per skill)

The 11 wired skills fall into four wiring classes (from `inventory/skill-wiring.txt:11-21`):

| # | Skill | Wiring class | Role |
|---|-------|--------------|------|
| 1 | adapting-claude-pipeline | entry-point/methodology (BOOTSTRAP) | One-time: adapts generic `.claude/` to a target repo |
| 2 | dispatching-parallel-agents | entry-point/methodology | Fan-out of independent agent tasks |
| 3 | subagent-driven-development | entry-point/methodology | In-session plan execution: fresh subagent + 2-stage review per task |
| 4 | ralph-loop | script-referenced | Drives `ralph-loop.sh` (multi-task scheduler) |
| 5 | implement-roadmap-task | script-referenced | Drives `implement-orchestrator.sh` (single task/issue end-to-end) |
| 6 | improvement-loop | script-referenced (script *emits* it) | Post-failure pipeline self-repair |
| 7 | writing-plans | script-referenced (prompt names it) | Plan-stage methodology |
| 8 | using-skills | hook-injected | Skill-system bootstrap; injected every session |
| 9 | test-driven-development | agent-referenced | TDD discipline for implementer agents |
| 10 | ui-design-fundamentals | agent-referenced | Design values reference — **PRODUCT-SPECIFIC** |
| 11 | bulletproof-frontend | script+agent-referenced | CSS impl patterns — **PRODUCT-SPECIFIC** |

---

## §5. External invocations / WIRING (how each skill alters stage behavior — verified)

### 1. adapting-claude-pipeline — entry-point, BOOTSTRAP (0 script refs)
- **Wiring:** `inventory/skill-wiring.txt:11` classifies it `entry-point/methodology — §5 bootstrap (role); 0 script refs`. **Verified:** `grep` for the skill name across `scripts/ agents/ hooks/ prompts/` returns nothing — it is invoked by a human at clone time, never by the running pipeline.
- **Alters behavior:** It does not alter a *stage*; it produces the entire adapted `.claude/` that the stages then run inside. See §SPECIAL (bootstrap mechanism) below.

### 2. dispatching-parallel-agents — entry-point (0 script refs)
- **Wiring:** `inventory/skill-wiring.txt:12` `orchestration methodology; 0 script refs`. **Verified:** no script/agent/hook references it; it is a methodology a human (or a controlling skill) follows to fan out `Task(...)` calls. `H/skills/dispatching-parallel-agents/SKILL.md:66-72` shows the `Task("...")` dispatch pattern.
- **Dispatches:** generic — "one agent per independent problem domain"; no named agent definitions.

### 3. subagent-driven-development — entry-point (0 script refs, 3 cross-skill)
- **Wiring:** `inventory/skill-wiring.txt:13` `orchestration methodology; 0 script refs (3 cross-skill)`. **Verified:** no pipeline-script reference. It is referenced *by other skills*: `writing-plans/SKILL.md:106` ("REQUIRED: Use subagent-driven-development") and `adapting-claude-pipeline/SKILL.md:189` ("Phase 5: Execute with Subagents... Use the `subagent-driven-development` skill").
- **Dispatches (named agent definitions):** `H/skills/subagent-driven-development/SKILL.md:104-108` routes by task type → `python-backend-developer` (backend) and `bulletproof-frontend-developer` (frontend); plus reviewer subagents via `./implementer-prompt.md`, `./spec-reviewer-prompt.md`, `./code-quality-reviewer-prompt.md` (SKILL.md:96-98).
- **Note:** This is the *human/Claude-driven* analogue of what `implement-orchestrator.sh` does in shell (fresh subagent per task + 2-stage review). The orchestrator is the as-built; this skill is the manual playbook.

### 4. ralph-loop → `ralph-loop.sh`
- **Wiring:** `inventory/skill-wiring.txt:14` `ralph-loop.sh + status files (18 refs)`. **Verified:** `H/skills/ralph-loop/SKILL.md:19` launches `.claude/scripts/ralph-loop.sh --tasks "$TASK_IDS" --branch $BASE_BRANCH`; the script exists (`H/scripts/ralph-loop.sh`, 87 KB).
- **Per-task dispatch:** `H/scripts/ralph-loop.sh:1408` invokes `"$SCRIPT_DIR/implement-roadmap-task-orchestrator.sh"` per task — i.e. ralph is a *scheduler around* the single-task orchestrator (DAG + concurrency + retry-with-learnings).
- **Status surface:** writes `status-ralph.json`, `ralph-queue.json`, `$LOG_BASE/stages/index.md`, `$LOG_BASE/context/stage-costs.jsonl` (SKILL.md:95, 170-177, 289).
- **Alters behavior:** mode flags (`--micro/--lite/--full`, per-task `82:lite` syntax) and provider levers (`ORCHESTRATOR_PROVIDER`, per-task `:codex`) change which stages each child orchestrator runs (SKILL.md:35-44, 54-91).

### 5. implement-roadmap-task → `implement-orchestrator.sh`
- **Wiring:** `inventory/skill-wiring.txt:15` cites `implement-roadmap-task-orchestrator.sh + logs/ (18 refs)`. **Verified with a correction (see §9 DISPUTED):** the SKILL.md actually invokes `.claude/scripts/implement-orchestrator.sh` directly (`H/skills/implement-roadmap-task/SKILL.md:54,59,67,83,177,204,207`). `implement-roadmap-task-orchestrator.sh` is a 632-byte **backward-compatible wrapper** that `exec`s the unified script (`H/scripts/implement-roadmap-task-orchestrator.sh:3-7,19`). Both names resolve to the same engine.
- **Stage map (verified from SKILL):** `H/skills/implement-roadmap-task/SKILL.md:136-148` — extract→setup→research→evaluate→plan→implement→test→docs→pr→review→complete, with per-stage agents (`test`→`python-backend-developer`, `docs`→`phpdoc-writer`, `review`→`spec-reviewer`+`code-reviewer`).
- **Alters behavior:** pre-flight (SKILL.md:17-44) can *refuse* to launch if the issue/task is already done; provider levers identical to ralph.

### 6. improvement-loop — script *emits* the skill invocation
- **Wiring:** `inventory/skill-wiring.txt:16` `orchestrator-common.sh:812/845/851 emits /improvement-loop (4 refs)`. **Verified:**
  - `H/scripts/lib/orchestrator-common.sh:800` — comment: "Generate post-run retrospective + improvement-loop recommendation on failure states"
  - `:812` — "...recommendation to run /improvement-loop. Called automatically from..."
  - `:845` — `printf -- 'Run \`/improvement-loop\` to analyze this failure and suggest pipeline fixes.\n'`
  - `:851` — `log "║  Next step: run /improvement-loop to analyze and improve"`
- **Alters behavior:** Inverse of the others — the *script tells the human* to run this skill after a failed orchestrator run. It is the pipeline's self-repair feedback loop; it edits skills/agents/hooks/scripts (`H/skills/improvement-loop/SKILL.md:111-118` classification table; routing at :160-168 dispatches `cc-orchestration-writer` / `bash-script-craftsman`). Its gate (SKILL.md:51-77) mandates the original issue be resolved first.

### 7. writing-plans — named in the orchestrator's plan-stage prompt
- **Wiring:** `inventory/skill-wiring.txt:17` `implement-orchestrator.sh:793 (plan stage)`. **Verified:** `H/scripts/implement-orchestrator.sh:793` — the plan_prompt literal: `1. Write a detailed implementation plan using writing-plans skill`. (This is the only `writing-plans` ref in `scripts/`.)
- **Alters behavior:** the orchestrator's plan stage hands this prompt to Claude; the skill governs plan *shape* — bite-sized TDD steps, the required header with **Feature Branch** (SKILL.md:42), save path `docs/plans/YYYY-MM-DD-<feature>.md` (SKILL.md:16). Also human entry-point: `adapting-claude-pipeline/SKILL.md:167` requires it for the adaptation plan.
- **Handoff:** SKILL.md:106 requires `subagent-driven-development` to execute the plan it writes.

### 8. using-skills — hook-injected every session
- **Wiring:** `inventory/skill-wiring.txt:18` `hooks/session-start.sh:12 (injected into every session)`. **Verified:** `H/hooks/session-start.sh:12` `cat`s the full `using-skills/SKILL.md` and `:36-42` emits it as a `SessionStart` `additionalContext` injection wrapped in `<EXTREMELY_IMPORTANT>`. It is *not* loaded via the Skill tool — its bytes are pushed into context at the start of **every** session.
- **Alters behavior:** It is the skill-system bootstrap: it establishes the "invoke a skill before ANY response, even 1% chance" rule (`H/skills/using-skills/SKILL.md:6-12,23-24`) that makes all other skill wiring fire. Without this hook, no skill would be self-invoked.

### 9. test-driven-development — referenced by implementer agent definition
- **Wiring:** `inventory/skill-wiring.txt:19` `agents/python-backend-developer.md:219`. **Verified:** `H/agents/python-backend-developer.md` "TESTING APPROACH" section: `Follow **test-driven-development** skill patterns:` (the heading sits at ~:219; the directive immediately follows). Also referenced as a subagent skill in `subagent-driven-development/SKILL.md:248`.
- **Alters behavior:** Any subtask routed to `python-backend-developer` inherits red-green-refactor discipline (Iron Law: "NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST", SKILL.md:33). It tightens implement/test stages.

### 10. ui-design-fundamentals — referenced by frontend agent  [PRODUCT-SPECIFIC]
- **Wiring:** `inventory/skill-wiring.txt:20` `agents/bulletproof-frontend-developer.md:23 [PRODUCT-SPECIFIC → adapter drop-in]`. **Verified:** `H/agents/bulletproof-frontend-developer.md:23` — "**UI Design Reference:** ...consult the `ui-design-fundamentals` skill. It provides concrete values for the 8pt grid, type scales, WCAG contrast..."
- **Alters behavior:** Frontend subtasks pull 8pt-grid / WCAG / component-anatomy values from this skill. Pure design reference, no agents dispatched.

### 11. bulletproof-frontend — script+agent referenced  [PRODUCT-SPECIFIC]
- **Wiring:** `inventory/skill-wiring.txt:21` `30 refs (batch/implement-orchestrator, select-incremental-test-agent, agents)`. **Verified — with an important distinction (see §9):** of the 30 hits, only **two reference the *skill* directly**:
  - `H/prompts/frontend/refactor-blade-thorough.md:57` — "`.claude/skills/bulletproof-frontend/` — CSS architecture patterns"
  - `H/agents/bulletproof-frontend-developer.md:669` — "`.claude/skills/bulletproof-frontend/SKILL.md` - Quick reference for styling patterns"
  - The other ~28 hits are the *agent* `bulletproof-frontend-developer`, dispatched by: `H/scripts/lib/select-incremental-test-agent.sh:31` (returns it for `*.spec.ts`-only diffs), `H/scripts/lib/orchestrator-common.sh:1874,1891,1893,1966,1968,3713,3728,3745,4205,4244` (E2E/tsc-gate fix dispatch), `H/scripts/implement-orchestrator.sh:795,1066,1253,1265,1424`, and `H/scripts/batch-orchestrator.sh:9,12,59,68` (CLI `--agent`).
- **Alters behavior:** the skill governs CSS impl patterns the frontend agent applies; the *agent* (carrying both this skill and ui-design-fundamentals) is what the scripts actually invoke for frontend/E2E stages.

---

## §8. Coupling (generic vs Hey Soo!-specific; generic shape it should take)

| Skill | Coupling | Generic shape for the template |
|-------|----------|--------------------------------|
| using-skills | **Generic** (load-bearing) | Keep verbatim — it's the skill bootstrap. Only the hook path is project-relative. |
| writing-plans | **Generic** w/ minor coupling | Plan-stage methodology is universal; the `python-backend-developer`/`bulletproof-frontend-developer` agent enum (SKILL via orchestrator:795) is project-specific and belongs in the project-config adapter. |
| test-driven-development | **Generic** | Universal; the agent that references it (`python-backend-developer`) is the project-specific piece. Template keeps TDD, swaps the agent. |
| dispatching-parallel-agents | **Generic** | Universal methodology. Keep as-is. |
| subagent-driven-development | **Generic core, project agent table** | Core 2-stage-review loop is generic; the agent-selection table (SKILL.md:104-108) is project-specific → adapter. |
| improvement-loop | **Generic core, project routing** | Five-step cycle generic; routing targets (`cc-orchestration-writer`, `bash-script-craftsman`, settings.json) are project-specific. |
| adapting-claude-pipeline | **Generic (it IS the adapter mechanism)** | This skill is the bootstrap engine; its inventory tables (Laravel/PHP/web examples) are illustrative and must be regenerated per target. |
| ralph-loop | **Generic scheduler, project task-source** | Scheduler/DAG/queue generic; GitHub-issue task source + roadmap.md are project-specific. |
| implement-roadmap-task | **Generic orchestrator, project stages** | End-to-end driver generic; per-stage agent map (test→python, docs→phpdoc) is project-specific → adapter. |
| **ui-design-fundamentals** | **PRODUCT-SPECIFIC (web/CSS)** → **project-config adapter drop-in candidate** | Delete for non-web targets. In a web target, the design-values doc is a drop-in. `adapting-claude-pipeline/SKILL.md:112,221` already lists it under "Delete if not web." |
| **bulletproof-frontend** | **PRODUCT-SPECIFIC (web/CSS)** → **project-config adapter drop-in candidate** | Same: delete for non-web; drop in for web. `adapting-claude-pipeline/SKILL.md:92,221` lists it as the canonical "Delete if not web" example. |

---

## SPECIAL: adapting-claude-pipeline — BOOTSTRAP mechanism (load-bearing for the template)

This is the §5 mechanism that *fills a new repo's project-config adapter*. It is a **6-phase human-driven workflow** (`H/skills/adapting-claude-pipeline/SKILL.md:22-39`):

1. **Brainstorm** (SKILL.md:43-59) — REQUIRED `brainstorming` skill. Elicits the target's tech stack, project type, workflows, pain points, scope. **This is where adapter values originate** (test commands, formatter, language).
2. **Research domain patterns** (SKILL.md:61-81) — WebSearch for `[lang] best practices/anti-patterns/testing/security`; findings feed agent anti-pattern sections, skill content, hook logic, orchestration stages.
3. **Audit existing inventory** (SKILL.md:83-165) — bucket every `.claude/` file into Keep / Modify / Replace / Delete. Tables enumerate all 19 skills, 10 agents, hooks, settings, scripts, prompts with a default decision each. **This is the inventory the adapter overrides.**
4. **Write the plan** (SKILL.md:166-185) — REQUIRED `writing-plans`; organizes into parallel workstreams A–F (delete / modify / new skills / new agents / scripts / hooks+settings).
5. **Execute with subagents** (SKILL.md:187-204) — REQUIRED `subagent-driven-development`; routing table (SKILL.md:193-203) sends script edits to `cc-orchestration-writer`, hook creation to `bash-script-craftsman`, new skills/agents to `writing-skills`/`writing-agents`; parallel-safe workstreams use `dispatching-parallel-agents`.
6. **Verify** (SKILL.md:206-216) — glob for orphaned files, grep for dangling refs to deleted skills/agents, validate settings.json hooks, audit skill descriptions + agent deferral graph, mental dry-run.

**Why load-bearing for the template:** Phase 3's audit tables + the Common Adaptation Patterns (SKILL.md:217-243, esp. "Web to CLI/Library" at :219) are the exact mapping the template's **project-config adapter** formalizes — what to keep generic vs. drop in per target. The skill's "Core principle: every file must earn its place" (SKILL.md:12) and "Delete aggressively" (SKILL.md:255) are the design rule the adapter encodes. **It composes the other 10 wired skills** (brainstorming-not-in-our-11, writing-plans, subagent-driven-development, dispatching-parallel-agents) into the bootstrap — so it sits at the top of the skill dependency graph.

---

## §9. Anomalies, contradictions, stale content

1. **DISPUTED — implement-roadmap-task script name.** `inventory/skill-wiring.txt:15` cites the wiring as `implement-roadmap-task-orchestrator.sh`. The SKILL.md actually invokes `implement-orchestrator.sh` (`H/skills/implement-roadmap-task/SKILL.md:54` and 10 other lines). `implement-roadmap-task-orchestrator.sh` is a 632-byte backward-compat wrapper that `exec`s the unified script (`H/scripts/implement-roadmap-task-orchestrator.sh:19`). **Not a contradiction in behavior** (same engine), but the wiring map's script name is the deprecated alias; the *as-built* skill drives `implement-orchestrator.sh`. Ralph still calls the wrapper name (`ralph-loop.sh:1408`), so both aliases are live.

2. **DISPUTED — "bulletproof-frontend (30 refs)" conflates skill and agent.** `inventory/skill-wiring.txt:21` attributes 30 pipeline refs to the *skill*. Verified: only **2** of those 30 reference the skill file (`prompts/frontend/refactor-blade-thorough.md:57`, `agents/bulletproof-frontend-developer.md:669`). The remaining ~28 reference the **agent** `bulletproof-frontend-developer`. The skill is wired only transitively (the agent carries it). Classification "script+agent-referenced" is defensible but the ref count overstates direct skill wiring.

3. **Stale inventory count in adapting-claude-pipeline.** SKILL.md:96 says "Skills (19 in template)" and SKILL.md:120 "Agents (10 in template)"; the audit table at :98-118 lists 19 skills. The reference system now has **20 skills** (per `inventory/skill-wiring.txt:1`, "20 skills total"). The audit table also omits the script-driver skills present today (`ralph-loop`, `implement-roadmap-task`, `improvement-loop`, `using-skills` appears but `handle-issues`/`implement-issue`/`process-pr` listed at :104-107 may not all still exist). The adaptation skill's inventory tables are **stale** relative to the current skill set — expected, since it predates the ralph/unified-orchestrator era. Flag for regeneration when the template's adapter is built.

4. **Frontmatter formatting glitch.** `H/skills/ui-design-fundamentals/SKILL.md:1` has a leading-tab/indented `---` opening fence (line 1 reads "\t---"). Cosmetic; YAML frontmatter parsers may still accept it, but it's malformed vs. every other SKILL.md. Minor.

5. **Skill↔as-built duplication (not a contradiction, a design overlap).** `subagent-driven-development` (manual fresh-subagent + 2-stage review) and `implement-orchestrator.sh` (shell fresh-subagent + spec/code review stages) implement the *same methodology* in two media. The skill is the human playbook; the script is the automated as-built. They agree; the template should note the script is the authoritative implementation and the skill the documentation/manual-fallback.

6. **improvement-loop inverts the normal wiring direction.** Unlike every other script-referenced skill (human → skill → script), here the *script emits a recommendation to run the skill* (`orchestrator-common.sh:845,851`). Worth modeling explicitly in the template: it's a closed feedback loop (script failure → human runs improvement-loop → edits pipeline → next run). No bug; noting because it's structurally unusual.
