# Project starter kit (the default orchestration template)

The customizable source of "stuff" a new project starts from when you bootstrap it for
orchestration. The bootstrap skill (`run_targets/project_bootstrap_skill.md`) and the
profile-driven scaffold (`orchestrator-scaffold`) read `manifest.toml` to roll a
**stack-appropriate subset** of these assets into a new project — plus the generated
project-config adapter.

## What's here

| Dir | Seeds into | Purpose |
|---|---|---|
| `agents/` | `<project>/.claude/agents/` | Persona definitions the stage roster references (generic + per-stack). |
| `skills/` | `<project>/.claude/skills/` | The run-target supervisor/scheduler skills (always). |
| `hooks/` | `<project>/.claude/` settings | Optional per-stack Claude Code hooks (format-on-edit, etc.). |
| `schemas/` | `<project>/.claude/schemas/` | Overridable copies of the engine's per-stage output schemas (codex full-validation). |
| `commands.reference.md` | — | Stack → build/test/lint command cheat-sheet the interview maps answers to. |
| `manifest.toml` | — | The selection menu: which assets are generic vs stack-tagged. |

## Customizing it

This kit is **version-controlled in the orchestration-template repo**, so editing it
changes what every future scaffolded project starts from. To tailor a single project,
the bootstrap is **Keep / Modify / Replace / Delete**:

- **Keep** the generic agents/skills/schemas that fit.
- **Modify** an agent's persona or a schema's contract for the project.
- **Replace** the task source, commands, or an implement agent with a project-specific one.
- **Delete** layers the project lacks (no e2e? drop the e2e agent/commands).

The engine is never edited — only the adapter and this seeded `.claude/` content. Stage
**schemas are universal contracts**: a seeded copy lets a project override one stage's
shape, but if you delete it the engine's canonical schema is still used.

## Re-running

The bootstrap skill is re-callable. On a second run it reads the project's persisted
`profile.toml`, shows the current selection, and applies **additive deltas** (add a new
language's agents, swap a noisy reviewer, route a stage to codex) — it does not clobber
hand-edits. After real runs it can read `events.jsonl` / `retrospective.md` /
`cost-report.md` to *propose* data-informed tuning.
