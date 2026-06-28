# Hooks (examples)

These are **example** Claude Code hooks, stack-tagged in `../manifest.toml`. They are
optional — off unless the bootstrap selects them for the project's stack.

Each file is a fragment to merge into the project's `.claude/settings.json` under the
`"hooks"` key. They demonstrate the common pattern (format-on-edit per language); treat
them as starting points and verify the event name, matcher syntax, and file-path
environment variable against your Claude Code version before depending on them.

The orchestration engine does **not** manage hooks — they're execution-environment
behavior that fires in the worktree regardless of what triggered the edit.
