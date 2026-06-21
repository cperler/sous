# Schemas & Entry Points

Source commit: `0dd5d09d641510ee595e0300f2e9422005194d58`

---

## Schemas

### Role

All 17 JSON Schema files live in `.claude/scripts/schemas/`. They are passed as
`--json-schema <schema>` to the `claude` CLI to enforce structured output from LLM stage
calls. The sole stage dispatcher `run_stage()` (`orchestrator-common.sh:2848`) resolves
`$SCHEMA_DIR/$schema_file` (`orchestrator-common.sh:2863`) and passes it inline
(`orchestrator-common.sh:280`). `run_provider_oneshot()` (`ralph-loop.sh:1172,1520`) takes
a full absolute path instead of a bare filename.

### Schema ↔ Stage Matrix

| # | Filename | Consuming stage | Notes |
|---|----------|-----------------|-------|
| 1 | `implement-issue-research.json` | **research** | `implement-orchestrator.sh:668` |
| 2 | `implement-issue-evaluate.json` | **evaluate** | `implement-orchestrator.sh:702` |
| 3 | `implement-issue-plan.json` | **plan** | `implement-orchestrator.sh:832` |
| 4 | `implement-issue-implement.json` | **implement-task-N**, **docs** | `implement-orchestrator.sh:1010,1091,1517` |
| 5 | `implement-issue-simplify.json` | **simplify** sub-stage | `orchestrator-common.sh:3385` |
| 6 | `implement-issue-review.json` | **review**, **task-review**, **spec-review**, **code-review**, **test-validate** | `orchestrator-common.sh:3422,4300`; `implement-orchestrator.sh:1226,1712,1867,1904` |
| 7 | `implement-issue-fix.json` | **fix** and all fix sub-stages (e2e-fix, tsc-gate-fix, unit-fix, fix-pr-review, build-fix, …) | `orchestrator-common.sh:1891,1966,3510,3728,3979,4238,4336`; `implement-orchestrator.sh:1265,1303,1424,1742,1987` |
| 8 | `implement-issue-task-review.json` | **task-review-N** (subtask goal verification) | `implement-orchestrator.sh:1226` |
| 9 | `implement-issue-test.json` | **DEAD** — no `run_stage` call found | Anomaly A: likely orphaned after test stage was refactored into a fix/review loop |
| 10 | `implement-issue-pr.json` | **pr** | `implement-orchestrator.sh:1601` |
| 11 | `implement-issue-complete.json` | **complete** | `implement-orchestrator.sh:2086` |
| 12 | `implement-issue.json` | **ORPHAN** — batch-level output shape, never passed via `--json-schema` | Anomaly B: `batch-orchestrator.sh` parses issue status with `jq` directly, skipping validation |
| 13 | `process-pr.json` | **process-pr** skill invocation in `batch-orchestrator.sh` | `batch-orchestrator.sh:674,706,712`; pre-loaded as compact JSON via `jq -c` |
| 14 | `ralph-dependency-analysis.json` | ralph dependency analysis pre-run | `ralph-loop.sh:1172`; failure triggers `run_dependency_analysis_fallback` |
| 15 | `ralph-learnings-summary.json` | ralph post-failure learnings | `ralph-loop.sh:1520`; explicit 180s timeout; failure silently tolerated |
| 16 | `implement-roadmap-task-extract.json` | **DEAD** | Anomaly C: `extract-roadmap-task.sh` output is parsed by `jq`, never `--json-schema`-validated |
| 17 | `implement-roadmap-task-update.json` | **DEAD** | Anomaly C: `update-roadmap-status.sh` is DEPRECATED and makes no Claude call |

**Three DEAD schemas** (`implement-issue-test.json`, `implement-roadmap-task-extract.json`,
`implement-roadmap-task-update.json`) and **one ORPHAN** (`implement-issue.json`) — four
schemas with no active `--json-schema` consumer.

### Coupling verdict

Schemas are generic: no Hey Soo!-specific names or enums appear in any schema file.
**One weak exception:** `needs_deploy` in `ralph-dependency-analysis.json:50-55` encodes a
deploy-gated E2E architecture assumption (projects with no deployable service would always
produce empty arrays). The field itself is schema-generic (array of strings), but its
semantic meaning reflects Hey Soo!'s topology. Flag: **weakly coupled**.

---

## Entry Points

### Wrapper aliases

Two thin wrappers delegate to `implement-orchestrator.sh` with no engine logic of their own:

| Script | Role | Delegation |
|--------|------|------------|
| `implement-roadmap-task-orchestrator.sh` | Backward-compat pass-through for legacy callers using the roadmap-task entry name | `exec implement-orchestrator.sh "$@"` (`:15`); verbatim argv forwarding |
| `implement-issue-orchestrator.sh` | Backward-compat argv-translating wrapper for `--issue N` callers | Rewrites `--issue N` → `--task "#N"` (`:20`), then `exec implement-orchestrator.sh "${args[@]}"` (`:30`); `exit 3` if `--issue` value missing (`:19`) |

Neither wrapper handles failures beyond the `--issue` guard; exit code is the engine's.

### `extract-roadmap-task.sh` — task-source parser

Single entry point `extract-roadmap-task.sh <task-id>`. The engine calls it as a plain
bash subprocess (`implement-orchestrator.sh:492`) and **parses stdout with `jq`** — there
is no `--json-schema` validation; `implement-roadmap-task-extract.json` documents the
intended contract but is never enforced at runtime.

Task-ID dispatch:
- `^#[0-9]+$` → **GitHub Issues branch** (live): `gh issue view <N> --json title,body,labels,state` (`:40`). This is the active source at commit `0dd5d09`.
- `^[0-9]+(\.[0-9]+)+$` → **roadmap-markdown branch** (DEAD): `grep`/`awk`/`sed` over `docs/roadmap.md`. At this commit the roadmap contains no `- [ ] **Task X.Y.Z:` lines; any dotted-ID call returns `exit 4`. The roadmap header states all tasks are tracked as GitHub Issues.

Output (stdout): `{status:"success", task:{task_id, title, status, full_title, description[], milestone, phase, source}}`.
Exit codes: `0` success, `4` not found, `1` bad args/format/missing file.

**`implement-roadmap-task-extract.json` mismatch:** the schema omits the `source` field that
`extract-roadmap-task.sh` always emits (`:73`); schema-validation would reject real output
if ever applied.

### `update-roadmap-status.sh` — DEPRECATED

Self-described as DEPRECATED (`:4-6`). Makes no Claude call; `implement-roadmap-task-update.json`
is likewise dead. For `#N` IDs the script is a guaranteed no-op (`:23-34`). For dotted task
IDs it rewrites `docs/roadmap.md` in place via `sed -i.tmp` and commits — but the roadmap
markdown task source is dead (see above), so this path is never reached in the live system.
