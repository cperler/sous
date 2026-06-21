# Fragment: agents + .claude config surface

Sources (Hey Soo! reference repo, read-only):
- `/Users/craigperler/Development/heysoo/.claude/agents/bulletproof-frontend-developer.md`
- `/Users/craigperler/Development/heysoo/.claude/agents/cc-orchestration-writer.md`
- `/Users/craigperler/Development/heysoo/.claude/agents/code-reviewer.md`
- `/Users/craigperler/Development/heysoo/.claude/agents/python-backend-developer.md`
- `/Users/craigperler/Development/heysoo/.claude/agents/spec-reviewer.md`
- `/Users/craigperler/Development/heysoo/.claude/settings.json`
- `/Users/craigperler/Development/heysoo/.claude/settings.local.json`
- `/Users/craigperler/Development/heysoo/.claude/hooks/session-start.sh`

Dispatch cross-reference (not assigned but read for §1 / roster):
- `/Users/craigperler/Development/heysoo/.claude/scripts/implement-orchestrator.sh`
- `/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh`
- `/Users/craigperler/Development/heysoo/.claude/scripts/batch-orchestrator.sh`

Source commit: not captured (read-only working tree).   Mapped lines: cited inline per claim.

---

## 1. Role & entry points — who invokes it, with what argv

Agents are Claude Code subagent definitions (markdown with YAML frontmatter under `.claude/agents/`). They are **not** invoked with argv; they are selected by name via the Claude CLI `--agent <name>` flag, which the orchestrator passes through `run_stage`'s agent argument. The shared `run_stage` builds `agent_args=(--agent "$agent")` (`/Users/craigperler/Development/heysoo/.claude/scripts/lib/orchestrator-common.sh:2933`).

Pipeline stages that dispatch each agent (all in `implement-orchestrator.sh` unless noted):

| Agent | Dispatched by stage(s) | Cite |
|---|---|---|
| `python-backend-developer` | default `--agent` for setup/research/evaluate/plan stages; default for implement tasks tagged backend; default fix agent for unit-test fix loop, test-validate, full-suite halts | research `:668`, evaluate `:702`, plan default `:772/:778/:795`, unit-fix `orchestrator-common.sh:3979/4238`, validate `orchestrator-common.sh:4300` |
| `bulletproof-frontend-developer` | implement tasks tagged frontend (policy reminder branch `:1066`); E2E-failure fix loop; tsc-gate fix loop | E2E fix `orchestrator-common.sh:1891/1966`, tsc-gate `orchestrator-common.sh:3728` |
| `spec-reviewer` | per-task review gate inside the implement loop ("subtask N of M" reviews) | `:1226` |
| `code-reviewer` | code-review loop (`run_stage "review-…" … code-reviewer`); also batch PR review in `batch-orchestrator.sh` | `orchestrator-common.sh:3422`; `batch-orchestrator.sh:672/704/710` |
| `cc-orchestration-writer` | **NOT dispatched by any pipeline stage.** Authoring/meta agent — writes the orchestration scripts themselves (model: opus). No `--agent cc-orchestration-writer` call exists in `.claude/scripts/`. | grep: zero hits |

The `--agent` value on the orchestrator CLI (`implement-orchestrator.sh:73`, `:116`) is the *default* agent for stages that don't hard-code one; `plan` emits per-task `agent` fields that override it (`:795`), constrained to `python-backend-developer` or `bulletproof-frontend-developer`.

## 2. Inputs — every flag, env var, file read

These are declarative config files, not executables (except the hook). Inputs:
- **Agent .md frontmatter fields read by Claude Code:** `name`, `description`, `model` (optional pin), and the markdown body = system prompt.
- **`session-start.sh` inputs:** `BASH_SOURCE`/`$0` (locate script dir, `:8`), derives `PROJECT_ROOT` two levels up (`:9`), reads file `${PROJECT_ROOT}/.claude/skills/using-skills/SKILL.md` (`:12`). No flags, no other env. `set -euo pipefail` (`:5`).
- **settings.json / settings.local.json:** consumed by the Claude Code harness, not by scripts. See §CONFIG SURFACE below.

## 3. Outputs — files written, exit codes, side effects

- **Agent .md files:** no runtime output; they shape a subagent's behavior/tool policy.
- **`session-start.sh`:** writes JSON to stdout (`hookSpecificOutput.additionalContext`) injecting the full `using-skills/SKILL.md` content wrapped in `<EXTREMELY_IMPORTANT>` (`:36-43`). `exit 0` always (`:45`). On read failure it substitutes `"Error reading using-skills skill"` (`:12`) rather than failing. Pure-bash JSON escaping via `escape_for_json` (`:15-31`).
- **settings files:** no output; they register hooks, permissions, env, plugins, statusline.

## 4. Control flow

Agents and config have no control flow of their own. The one executable, `session-start.sh`:
- `set -euo pipefail` (`/Users/craigperler/Development/heysoo/.claude/hooks/session-start.sh:5`).
- Linear: resolve `SCRIPT_DIR` (`:8`) → `PROJECT_ROOT` (`:9`) → read SKILL.md with `|| echo "Error…"` fallback (`:12`) → char-by-char escape loop `for ((i=0; i<${#input}; i++))` (`:19-30`) → emit heredoc JSON (`:36-43`) → `exit 0` (`:45`). No branches beyond the per-char `case` (`:21-29`) and the read fallback.

## 5. External invocations

None inside the agent files or `session-start.sh` (only builtin `cat` at `:12`). The agent .md docs *describe* commands (e.g. `cc-orchestration-writer` documents the `claude -p … --agent … --json-schema …` pattern as reference material) but execute nothing. Verbatim dispatch of these agents lives in the orchestrator scripts cited in §1, e.g. `run_stage … "code-reviewer"` → `claude -p … --agent code-reviewer …` assembled in `orchestrator-common.sh:2933`.

## 6. Constants & tunables

- **Model pins (frontmatter):** `cc-orchestration-writer` → `model: opus` (`/Users/craigperler/Development/heysoo/.claude/agents/cc-orchestration-writer.md:4`); `code-reviewer` → `model: inherit` (`/Users/craigperler/Development/heysoo/.claude/agents/code-reviewer.md:5`). `bulletproof-frontend-developer`, `python-backend-developer`, `spec-reviewer` have **no** model field (inherit session default).
- **`session-start.sh`:** SKILL.md path constant `${PROJECT_ROOT}/.claude/skills/using-skills/SKILL.md` (`:12`); no numeric tunables.
- **settings.json hook timeouts:** PostToolUse format hook `timeout: 30` (`:22`), e2e-selector check `timeout: 10` (`:27`), shellcheck `timeout: 15` (`:32`); PreToolUse env-guard `timeout: 5` (`:53`), deploy-guard `timeout: 5` (`:63`).

## 7. Failure handling

- **`session-start.sh`:** `set -e` would abort on any error, but the SKILL.md read is guarded `… || echo "Error reading using-skills skill"` (`:12`) so a missing skill degrades to an inline error string rather than aborting the session. Always `exit 0` (`:45`).
- **settings.json PreToolUse guards (deny-by-exit-2):** Edit/Write blocked if path contains `.env`, `.git/`, `credentials`, `package-lock.json` (`:52`); Bash blocked if any token contains `deploy_to_production` (`:62`). Both `exit(2)` to veto the tool call. These are the harness-level failure/guard rails.
- **Agent-level:** `code-reviewer` and `spec-reviewer` encode an **approval-threshold anti-thrash rule**: approve when all remaining issues are `suggestion`-level, request changes only on `critical`/`important` (`code-reviewer.md:117-122`; `spec-reviewer.md:160-176`) — prevents the review→fix loop from never terminating on nitpicks.

## 8. Coupling — generic vs Hey Soo!-specific

### Agents

| Agent | Coupling | Generic shape it should take |
|---|---|---|
| `code-reviewer` | **Mostly generic.** Plan-alignment + quality + severity-tiered `issues[]` output (`code-reviewer.md:107-122`) is reusable. Hey Soo!-specific leakage: Pydantic+DynamoDB `model_dump(mode="json")` rule (`:75-86`), Tailwind component-extraction rules (`:51-74`), frontend/backend Pydantic `extra="forbid"` contract audit (`:87-104`). | Keep severity model + comprehensive-first-pass + approval threshold as the generic core; move stack-specific rules (Pydantic/DynamoDB/Tailwind) into a project overlay/skill. |
| `spec-reviewer` | **Generic.** Goal-vs-scope-creep reviewer, no stack assumptions. Only soft coupling: example file paths use `.php` (`:144-145`) and it names the calling skill `implement-issue` Step 9 (`:222`). | Already a clean generic template; parameterize the caller name and drop `.php` examples. |
| `cc-orchestration-writer` | **Generic-but-self-referential.** Codifies *this* harness's own conventions (bash style, JSON-via-printf, stdout/stderr discipline, BATS CODECHECK, rate-limit handling). References `implement-orchestrator.sh` as the reference impl (`cc-orchestration-writer.md:1080`). Not product-coupled; it's the meta-agent that builds the orchestrator. | This is the spec author for the rebuild — its content is essentially a prose version of the engine contract. Keep as the canonical convention doc (note: Python rebuild supersedes the bash-specific rules). |
| `python-backend-developer` | **Heavily Hey Soo!-specific (lambda-coupled).** Hard-wired to `lambda/suggest/` layout (`python-backend-developer.md:11-20`), Bedrock/DynamoDB/Pydantic-v2, named ADRs 003/004/005/007/015 (`:41-47`), precision-domains, two-layer safety (`:78-91`), `uv run pytest lambda/suggest/…` commands (`:23-34`). | Generic shape = "backend implementer for stack X"; the AWS/Bedrock/ADR specifics belong in a project profile, not the agent core. |
| `bulletproof-frontend-developer` | **Product/frontend-specific.** React/TS/Tailwind craftsman; hard-codes Hey Soo! light-only theme `ADR-053` (no `dark:`, semantic-only color) (`:371-390`, `:568-573`, `:640-650`), `frontend/src/` layout, Hey Soo! skills `bulletproof-frontend`/`ui-design-fundamentals` (`:666-682`). | Generic shape = "frontend component craftsman"; theme/ADR/skill/path bindings are the product overlay. |

### Config / settings

| Item | Coupling | Generic shape |
|---|---|---|
| `settings.json` env `ENABLE_LSP_TOOL=1` + `enabledPlugins` (pyright/typescript/gopls LSP) (`:2-13`) | Stack-coupled (py/ts/go) but harness-generic mechanism | Keep LSP toggle generic; plugin list is per-project. |
| PostToolUse format hook (`:21`) | **Hey Soo!-specific paths:** `cd $CLAUDE_PROJECT_DIR/lambda/suggest && uv run ruff format` for `.py`, `cd …/frontend && npm run format` for `.[jt]sx?`. | Generic = "format file after Edit/Write"; the `lambda/suggest` + `frontend` dirs and tool choice are product config. |
| PostToolUse e2e selector check (`:26`) | Hey Soo!-specific: `tests/e2e/*.ts` → `scripts/check-e2e-no-id-selectors.sh`. | Project-specific lint hook. |
| PostToolUse shellcheck (`:31`) | Generic (any `.sh`). | Reusable as-is. |
| PreToolUse `fetch-usage.sh` (PreToolUse `:43` + Stop `:85`) | References `~/.claude/fetch-usage.sh` (user-global). Generic usage-metering. | Reusable; depends on a user-level script. |
| PreToolUse env/secret guard (`:52`) | Generic guard (`.env`/`.git/`/`credentials`/`package-lock.json`). | Reusable safety rail. |
| PreToolUse deploy guard (`:62`) | Semi-specific: blocks token `deploy_to_production`. | Generic shape = configurable denylist of dangerous command tokens. |
| Notification hook `notify-send` (`:74`) | Generic desktop notify (Linux `notify-send`; no-op fallback). | Reusable. |
| `settings.local.json` env `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` (`:3`) | Harness-generic experimental flag enabling multi-agent teams. | Reusable. |
| `settings.local.json` permission allowlist (`:5-127`) | **Heavily Hey Soo!-specific and machine-specific** — absolute `/Users/craigperler/Development/heysoo/...` paths, project script names, AWS service verbs, project playwright configs/ports. | Generic shape = a curated allowlist; nearly every entry here is project/machine-local and should NOT be templated verbatim. |

## 9. Anomalies

1. **`cc-orchestration-writer` is orphaned from the pipeline.** No stage dispatches it (grep over `.claude/scripts/` returns zero `--agent cc-orchestration-writer`). It is the meta/author agent, not a runtime worker — confirm this is intentional (it is, per its role) but note that the README/plan should classify it as "author" not "pipeline".
2. **Allowlist is polluted with one-off / debugging entries.** `settings.local.json` contains transient artifacts: `kill 34441` (`:107`), `kill 18823 18821` (`:109`), specific playwright spec lines with ports (`:79-86`), a literal `Bash(could not be validated". Use direct assignment to fall back to the:*)` (`:96`) that is clearly a mis-captured error string, and `Bash(__NEW_LINE_c6fb609e03344d98__ node --check /tmp/main_script.js)` (`:44`). These are accidental captures, not deliberate policy — do not carry into the rebuild.
3. **Mixed allow-scope.** The allowlist mixes broad globs (`Bash(git:*)` `:69`, `Bash(bash:*)` `:55`, `Bash(uv run:*)` `:38`) with hyper-narrow exact commands (full sed/python3 one-liners `:91-95`). The broad ones make the narrow ones redundant — net effect is near-unrestricted Bash. Generic rebuild should choose a deliberate, minimal allowlist.
4. **`spec-reviewer` example paths are `.php`** (`:144-145`) while the actual stack is Python/TS — stale copy from a prior project; harmless but a coupling smell (the agent itself claims to be stack-agnostic).
5. **Two different env-injection homes.** `settings.json` sets `ENABLE_LSP_TOOL`; `settings.local.json` sets `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`. Splitting harness env across global+local files is fine but worth flagging for the rebuild's config model.
6. **Hooks reference user-global scripts** (`~/.claude/fetch-usage.sh`, `~/.claude/statusline-command.sh`) not in the project tree — these are outside the assigned/reference scope and could not be verified here.

## DISPUTED

- **§1 model defaults:** I assert that agents without a `model:` frontmatter key inherit the session model. This is the documented Claude Code behavior but I did not find an explicit statement in the assigned files; treat as inference, not file-cited fact.
- **§1 `cc-orchestration-writer` = "no dispatch":** based on grep of `.claude/scripts/` only. If the agent is dispatched from a skill or a slash command outside `.claude/scripts/` (e.g. `.claude/skills/` or `.claude/commands/`), I did not search those trees — they were outside my assignment. Confidence high but not exhaustive.
- **§8 generic/coupled split** is my judgment call; the boundary between "core reviewer logic" and "stack overlay" in `code-reviewer.md` is a design opinion, not stated in the source.
