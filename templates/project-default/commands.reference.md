# Stack → adapter commands (cheat-sheet)

The bootstrap interview maps a project's stack to the project-config adapter's command
methods (`install_cmd`, `test_unit_cmd`, `test_e2e_cmd`, `lint_cmd`, `typecheck_cmd`,
`infra_reset`). The generated adapter runs lint and typecheck as deterministic REVIEW gates.
The machine-readable version is the `[commands.*]` tables in `manifest.toml`; this is the
human reference. Commands are a `list[str]` (argv), never a shell string.

| Stack | install | test (unit) | lint | typecheck | e2e |
|---|---|---|---|---|---|
| **python (uv)** | `uv sync` | `uv run python -m pytest -q` | `uv run python -m ruff check .` | `uv run python -m mypy .` | — |
| **python (poetry)** | `poetry install` | `poetry run python -m pytest -q` | `poetry run python -m ruff check .` | `poetry run python -m mypy .` | — |
| **typescript (pnpm)** | `pnpm install` | `pnpm test` | — | `pnpm exec tsc --noEmit` | `pnpm exec playwright test` |
| **node (npm)** | `npm ci` | `npm test` | — | `npm run typecheck` | `npm run e2e` |
| **go** | `go mod download` | `go test ./...` | — | `go vet ./...` | — |
| **rust** | `cargo fetch` | `cargo test` | — | `cargo clippy` | — |

Notes:
- A stage's command method should **fail closed** (exit non-zero) until set, so an
  unconfigured run doesn't vacuously pass — see the generated adapter's `test_unit_cmd`.
- Delete the command methods for layers a project lacks (no e2e → leave the no-op).
- These are defaults; the interview confirms/overrides them against what's actually in the
  repo (lockfiles, `package.json` scripts, `pyproject.toml`, Makefile targets).
- Python commands use **module invocation** (`python -m pytest`), not the bare console
  script (#396). A console script bakes its interpreter into a shebang, so a `.venv` copied
  from another worktree runs THAT worktree's python and a review can pass on the wrong
  source; `python -m` resolves the interpreter through the runner and cannot inherit a stale
  path. Note it also puts the project root on `sys.path`, which bare `pytest` does not.
- A **python** profile gets `fresh_install_paths()` and `worktree_origin_probes()` generated
  for it from `profile.toml`'s `[worktree]` table (#391) — `.venv`, a module probe for the
  detected package, and one origin probe per declared `test_unit`/`typecheck` gate whose
  KIND follows the command form: `python -m ...` proves the runner's interpreter
  (`interpreter_probe`), a bare console script proves its shebang (`launcher_probes`). A
  runner whose environment lives outside the tree (poetry, pipenv) gets neither, since a
  healthy worktree would fail both. Any other stack declares them by hand:
  `fresh_install_paths()` for environment artifacts that cannot move between worktrees,
  `worktree_origin_probes()` for the test runner and imported project source.
  Each named probe is `(name, argv, kind)`, prints its absolute path as its final stdout line,
  and uses `kind="launcher"` only when the final executable symlink may target a shared
  interpreter; imported modules use `kind="source"` so their real path must remain inside the
  worktree. If probes are omitted, verification is explicitly recorded as skipped.
