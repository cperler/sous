# Stack → adapter commands (cheat-sheet)

The bootstrap interview maps a project's stack to the project-config adapter's command
methods (`install_cmd`, `test_unit_cmd`, `test_e2e_cmd`, `lint_cmd`, `typecheck_cmd`,
`infra_reset`). The generated adapter runs lint and typecheck as deterministic REVIEW gates.
The machine-readable version is the `[commands.*]` tables in `manifest.toml`; this is the
human reference. Commands are a `list[str]` (argv), never a shell string.

| Stack | install | test (unit) | lint | typecheck | e2e |
|---|---|---|---|---|---|
| **python (uv)** | `uv sync` | `uv run pytest -q` | `uv run ruff check .` | `uv run mypy .` | — |
| **python (poetry)** | `poetry install` | `poetry run pytest -q` | `poetry run ruff check .` | `poetry run mypy .` | — |
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
- Declare `fresh_install_paths()` for environment artifacts that cannot move between
  worktrees, and `worktree_origin_probes()` for the test runner and imported project source.
  Each named probe command prints its resolved absolute path as its final stdout line. If
  probes are omitted, verification is explicitly recorded as skipped rather than guessed.
