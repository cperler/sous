---
name: adapt-orchestration-pipeline
description: Stand up the orchestration template for a NEW repo by generating and filling in a project-config adapter. The engine is never edited — only the adapter.
---

# Adapt the pipeline to a new project (the §5 bootstrap)

Goal: make the orchestration engine drive a new repo by writing ONE adapter. Ports
the `adapting-claude-pipeline` workflow. The engine, scheduler, runners, and CLI are
untouched; all project-specific knowledge lives in `adapters/project/<name>/`.

## 1. Generate the skeleton
```
uv run orchestrator-scaffold --name <project> --dest adapters/project
```
This writes a WORKING skeleton (`config.py`, `classifier.py`, `task_source.py`,
`__init__.py`) that already satisfies the `ProjectConfig` protocol with no-op defaults
and a local-JSON task source — so it imports and runs immediately. You harden it next.

## 2. Audit the repo — Keep / Modify / Replace / Delete
Inspect the target repo and fill the TODOs:
- **Commands** (`install_cmd`, `test_unit_cmd`, `test_e2e_cmd`, `test_shell_cmd`,
  `typecheck_cmd`, `infra_reset`): the real build/test/lint commands. Delete layers
  the project lacks (leave the no-op `["true"]`).
- **`classifier`** (taxonomy): how this project's test output maps to unit/e2e/shell
  failures, and how a changed file maps to its impacted tests.
- **`task_source`**: keep the local-JSON source, or replace with the project's real
  source (GitHub Issues — see `adapters/project/heysoo/task_source.py` — a tracker, etc.).
- **`agent_for`** (roster): the agent personas for implement/review/docstring stages;
  delete unused roles.

## 3. Verify
- `python -c "import adapters.project.<project> as a; from adapters.project.base import ProjectConfig; assert isinstance(a.get_config(), ProjectConfig)"`
- Dry-run a task: `… --project adapters.project.<project> --mode headless run-headless`
  (or the interactive supervisor loop). Confirm `status.lane_audit.clean == true`.

## Invariant
If you find yourself editing anything under `orchestrator/`, stop — that means a
project concern leaked into the engine. The correct fix is almost always in the adapter.
