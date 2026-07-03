# Hooks

Claude Code hook fragments, selected via `../manifest.toml`: **untagged hooks (the
safety guards) are seeded into every project**; language-tagged hooks (format-on-edit)
follow the project's stack.

Each file is a fragment of the form `{"hooks": {<event>: [entries]}}`. The bootstrap
does two things with a selected hook:

1. copies the fragment to the project's `.claude/hooks/<name>.json` (the reviewable
   source of record), and
2. **merges it into the project's `.claude/settings.json`** (additive + idempotent —
   existing entries and other settings keys are preserved), so the hook is LIVE, not
   an inert example.

All hook commands read the hook's **stdin JSON** (`tool_input.file_path` /
`tool_input.command`) — the interface the reference system's hooks used in production.
The safety guards exit `2` to deny the tool call:

- `guard-sensitive-files` — blocks model edits to `.env` / credentials / `.git/`
  internals / key files.
- `guard-deploy` — blocks deploy-shaped Bash commands (`deploy_to_production`,
  `terraform apply`, `cdk deploy`); add your project's own deploy entry points.

The orchestration engine does **not** manage hooks — they're execution-environment
behavior that fires in the worktree regardless of what triggered the edit.
