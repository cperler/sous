# Fragment: schemas/*.json (all 17)
Source commit: 0dd5d09   Mapped lines: schemas/*, implement-orchestrator.sh, lib/orchestrator-common.sh, batch-orchestrator.sh, ralph-loop.sh

---

## §1. Role & entry points

All 17 JSON Schema files live in `/Users/craigperler/Development/heysoo/.claude/scripts/schemas/`.
They are passed as `--json-schema <schema>` to the `claude` CLI to enforce structured output from LLM stage calls.

Two dispatch paths:

1. **`run_stage(stage_name, prompt, schema_file, agent?)`** (`lib/orchestrator-common.sh:2848`) — used by `implement-orchestrator.sh` for all per-stage Claude calls. Resolves `$SCHEMA_DIR/$schema_file` (`orchestrator-common.sh:2863`) and passes `--json-schema` inline (`orchestrator-common.sh:2870,280`).

2. **`run_provider_oneshot(prompt, schema_path, out_var, timeout?)`** (`ralph-loop.sh:1172,1520`) — used directly in `ralph-loop.sh` with full absolute paths like `$SCHEMA_DIR/ralph-dependency-analysis.json`.

3. **`batch-orchestrator.sh`** — loads `$SCHEMA_DIR/process-pr.json` at startup into `$PROCESS_SCHEMA` (`batch-orchestrator.sh:137`) and passes it directly to `claude --json-schema` (`batch-orchestrator.sh:674,706,712`).

---

## §2. Schema↔Stage Matrix

| # | Filename | Top-level fields (type, R=required/O=optional) | Consuming stage | Consuming script:line |
|---|----------|------------------------------------------------|-----------------|----------------------|
| 1 | `implement-issue-research.json` | `status` (enum success/error, R), `context` (object: issue_title str, issue_body str, related_files arr[str], dependencies arr[str]; O), `error` (str\|null, O) | **research** stage | `implement-orchestrator.sh:668` |
| 2 | `implement-issue-evaluate.json` | `status` (enum success/error, R), `approach` (str, O), `rationale` (str, O), `risks` (arr[str], O), `summary` (str, R), `error` (str\|null, O) | **evaluate** stage | `implement-orchestrator.sh:702` |
| 3 | `implement-issue-plan.json` | `status` (enum success/error, R), `plan_path` (str, O), `tasks` (arr[obj: id int R, description str R, agent str R, quality_tier enum full/light/none O, implementation_budget enum short/standard O, depends_on arr[int] O], O), `summary` (str, R), `task_list_markdown` (str, R), `error` (str\|null, O) | **plan** stage | `implement-orchestrator.sh:832` |
| 4 | `implement-issue-implement.json` | `status` (enum success/error, R), `commit` (str, O), `files_changed` (arr[str], O), `summary` (str, R), `error` (str\|null, O) | **implement-task-N** stage AND **docs** stage | `implement-orchestrator.sh:1010,1091,1517` |
| 5 | `implement-issue-simplify.json` | `status` (enum success/error, R), `commit` (str, O), `files_simplified` (arr[str], O), `changes_made` (arr[str], O), `summary` (str, R), `error` (str\|null, O) | **simplify** loop sub-stage | `orchestrator-common.sh:3385` |
| 6 | `implement-issue-review.json` | `result` (enum approved/changes_requested, R), `comments` (str, O), `issues` (arr[obj: severity enum critical/important/suggestion R, category str R, description str R, file str R, line int O, suggested_fix str O], R), `summary` (str, R) | **review** loop sub-stage, **task-review**, **spec-review**, **code-review**, **test-validate** | `orchestrator-common.sh:3422,4300`; `implement-orchestrator.sh:1226,1712,1867,1904` |
| 7 | `implement-issue-fix.json` | `status` (enum success/error, R), `commit` (str, O), `files_changed` (arr[str], O), `fixes_applied` (arr[str], O), `summary` (str, R), `error` (str\|null, O) | **fix** loop sub-stage, multiple fix sub-stages (e2e-fix, tsc-gate-fix, unit-fix, fix-test-quality, fix-pr-review, fix-review, build-fix) | `orchestrator-common.sh:1891,1966,3510,3728,3979,4238,4336`; `implement-orchestrator.sh:1265,1303,1424,1742,1987` |
| 8 | `implement-issue-task-review.json` | `result` (enum passed/failed, R), `comments` (str, R) | **task-review-N** stage (subtask goal verification) | `implement-orchestrator.sh:1226` |
| 9 | `implement-issue-test.json` | `result` (enum passed/failed, R), `total_tests` (int, O), `passed_tests` (int, O), `failed_tests` (int, O), `failures` (arr[obj: test str, message str], O), `summary` (str, R) | **test** stage | Not found as a direct `run_stage` call with this schema — see §9 Anomaly A |
| 10 | `implement-issue-pr.json` | `status` (enum success/error/skipped, R), `pr_number` (int, O), `pr_url` (str, O), `error` (str\|null, O) | **pr** stage | `implement-orchestrator.sh:1601` |
| 11 | `implement-issue-complete.json` | `status` (enum success/error/max_iterations, R), `pr_number` (int, O), `branch` (str, O), `decisions` (arr[str], O), `reviews_passed` (arr[str], O), `follow_up_issues` (arr[int], O), `innovation_brainstorm` (str, O), `orchestration_retrospective` (str, O), `pipeline_notes` (arr[str], O), `summary` (str, R), `error` (str\|null, O) | **complete** stage | `implement-orchestrator.sh:2086` |
| 12 | `implement-issue.json` | `status` (enum success/error/rate_limit, R), `pr_number` (int, O), `branch` (str, O), `error` (str, O), `stage` (enum fetch/research/plan/implement/test/review/pr, O) | Batch-level output schema for the full implement-issue pipeline | **NOT consumed via run_stage** — see §9 Anomaly B |
| 13 | `process-pr.json` | `status` (enum merged/changes_requested/error/rate_limit, R), `follow_up_issues` (arr[int], O), `innovation_idea` (str, O), `process_learnings` (str, O), `pipeline_notes` (arr[str], O), `error` (str, O) | **process-pr** skill invocation in batch-orchestrator | `batch-orchestrator.sh:674,706,712` |
| 14 | `ralph-dependency-analysis.json` | `tasks` (arr[obj: id str R, depends_on arr[str] R, reason str R, mode enum micro/lite/full R, mode_reason str R, needs_deploy arr[str] R], R); `additionalProperties: false` | **ralph dependency analysis** pre-run step | `ralph-loop.sh:1172` |
| 15 | `ralph-learnings-summary.json` | `summary` (str, R) | **ralph post-failure learnings** generation | `ralph-loop.sh:1520` |
| 16 | `implement-roadmap-task-extract.json` | `status` (enum success/error, R), `task` (obj: task_id str R, title str R, description arr[str] R, status enum pending/completed/deferred O, full_title str O, milestone obj\|null O, phase obj\|null O; R), `error` (str, O) | **DEAD SCHEMA** — not consumed anywhere in scripts | None found |
| 17 | `implement-roadmap-task-update.json` | `status` (enum success/error, R), `commit` (str\|null, O), `summary` (str, O), `error` (str, O) | **DEAD SCHEMA** — not consumed anywhere in scripts | None found |

---

## §3. Outputs

Each schema constrains the JSON blob returned from a `claude --json-schema` call. The structured output is then parsed by the calling script via `jq`. No schemas write files directly; they validate LLM responses only.

---

## §4. Control flow

The `run_stage` function (`orchestrator-common.sh:2848`) is the sole stage dispatcher. It:
1. Validates `$SCHEMA_DIR/$schema_file` exists (`orchestrator-common.sh:2863`).
2. Passes schema inline via `--json-schema` to the `claude` CLI (`orchestrator-common.sh:280`).
3. Returns raw JSON output which callers parse with `jq`.

`run_provider_oneshot` (`ralph-loop.sh`) takes a full path rather than a bare filename — passes `$SCHEMA_DIR/ralph-dependency-analysis.json` directly (`ralph-loop.sh:1172`).

---

## §5. External invocations

- `claude -p "$prompt" --model claude-sonnet-4-6 --dangerously-skip-permissions --output-format json --json-schema "$schema_path"` (`orchestrator-common.sh:273-282`)
- `claude ... --json-schema "$PROCESS_SCHEMA"` (`batch-orchestrator.sh:674,706,712`) — schema pre-loaded as compact JSON string via `jq -c`
- `run_provider_oneshot "$prompt" "$SCHEMA_DIR/ralph-dependency-analysis.json" output` (`ralph-loop.sh:1172`)
- `run_provider_oneshot "..." "$SCHEMA_DIR/ralph-learnings-summary.json" raw_summary 180` (`ralph-loop.sh:1520`)

---

## §6. Constants & tunables

- `SCHEMA_DIR="$SCRIPT_DIR/schemas"` (set independently in each entry-point script: `implement-orchestrator.sh:25`, `batch-orchestrator.sh:33`, `ralph-loop.sh` via `SCRIPT_DIR`)
- `ralph-learnings-summary.json` call has an explicit 180s timeout (`ralph-loop.sh:1520`); all other schema calls inherit the surrounding stage timeout

---

## §7. Failure handling

- `run_stage` returns `{"status":"error","error":"schema not found"}` and exits the function with rc=1 if the schema file does not exist (`orchestrator-common.sh:2864-2866`).
- `run_provider_oneshot` failure at `ralph-loop.sh:1172` triggers fallback to `run_dependency_analysis_fallback` (`ralph-loop.sh:1174-1177`).
- `ralph-learnings-summary.json` failure is silently tolerated (best-effort; `summary_rc` is checked but output is omitted, not propagated as pipeline error) (`ralph-loop.sh:1522-1526`).

---

## §8. Coupling — generic vs Hey Soo!-specific

**Claim under test:** design doc §2 states schemas are "generic; no Hey Soo! coupling."

**Verified generic (no project-specific strings/enums in the schema files themselves):**
- `implement-issue-research.json`, `implement-issue-evaluate.json`, `implement-issue-fix.json`, `implement-issue-implement.json`, `implement-issue-pr.json`, `implement-issue-review.json`, `implement-issue-simplify.json`, `implement-issue-task-review.json`, `implement-issue-test.json`, `implement-issue.json`, `implement-roadmap-task-extract.json`, `implement-roadmap-task-update.json`, `ralph-learnings-summary.json` — all field names and enums are domain-agnostic.

**DISPUTED — `needs_deploy` field in `ralph-dependency-analysis.json`:**
- `ralph-dependency-analysis.json:50-55` — field `needs_deploy` with description *"Task IDs from depends_on whose code must be DEPLOYED (not just merged) before this task's E2E tests can pass."* This implies a deployed-service model (e.g. a live backend that must be deployed before E2E tests pass). This is an architectural assumption baked into the schema that reflects Hey Soo!'s deploy-gated E2E test pattern. A project without a deployable backend service would always produce empty `needs_deploy` arrays and would find this field meaningless. **Flag: weakly Hey Soo!-coupled.** The field itself is generic JSON (array of strings), but its semantic meaning and the `mode` enum description (`"cross-boundary work (e.g., not both lambda/ and frontend/)"` — in the *prompt* fed to ralph, not in the schema itself at `ralph-loop.sh:1152-1161`) reveals project topology assumptions.

**`implement-issue-plan.json` — `quality_tier` and `implementation_budget` fields:**
- `implement-issue-plan.json:14-27` — These fields (`quality_tier: full/light/none`, `implementation_budget: short/standard`) are pipeline-internal conventions, not Hey Soo!-specific. They are generic pipeline controls. **No flag.**

**`implement-issue-complete.json` — `innovation_brainstorm` and `orchestration_retrospective` fields:**
- `implement-issue-complete.json:19-20` — These are cultural/process fields specific to the team's retrospective practice. While not containing Hey Soo!-specific strings, `orchestration_retrospective` references the orchestration process improvement loop which is this project's meta-level concern. **Weakly project-specific** but generic enough to carry forward. The *prompt* text feeding these fields is more coupled (`implement-orchestrator.sh:2067-2143`).

**`process-pr.json` — `process_learnings` field:**
- `process-pr.json:24` — *"What was learned about the orchestration process (scripts, skills, hooks, agents)"* — process-internal but not Hey Soo!-specific. **No flag.**

**Verdict:** The claim "generic; no Hey Soo! coupling" is **mostly true** for schema structure. The one exception worth flagging is `needs_deploy` in `ralph-dependency-analysis.json:50-55`, which encodes a deploy-gated E2E test assumption specific to projects with deployable services. No Hey Soo!-specific names, enums, or data values appear in any schema file.

---

## §9. Anomalies

**Anomaly A — `implement-issue-test.json` has no direct `run_stage` call:**
- A search across all scripts finds no `run_stage` call passing `"implement-issue-test.json"`. The test stage in `implement-orchestrator.sh` uses an `orchestrator-common.sh` function (`run_test_loop`) which invokes `run_stage` with `implement-issue-fix.json` and `implement-issue-review.json` for fix/validate sub-stages, but the top-level test result is not captured via a structured schema output. The schema exists and is well-formed but appears **unused by any current run_stage call**. Possible explanation: the test stage was formerly a single Claude call with this schema, then refactored into a loop using fix/review schemas; the test schema was not cleaned up.

**Anomaly B — `implement-issue.json` is orphaned at the script level:**
- `implement-issue.json` (`schemas/implement-issue.json`) has `$schema: http://json-schema.org/draft-07/schema#` and describes the final output of the entire implement-issue pipeline (status, pr_number, branch, stage-of-failure). No script passes it via `--json-schema`. The batch-orchestrator reads `issue_status_file` produced by the orchestrator process (`batch-orchestrator.sh:613-614`) and parses it with `jq` directly — it does not validate against this schema. This schema may have been intended for a future structured-output mode of the batch pipeline or was used in an earlier architecture where `implement-issue-orchestrator.sh` was invoked as a single Claude skill call.

**Anomaly C — `implement-roadmap-task-extract.json` and `implement-roadmap-task-update.json` are dead schemas:**
- Neither schema is referenced anywhere outside the schemas directory. The extract stage uses `extract-roadmap-task.sh` (a pure bash script) and writes JSON that structurally matches `implement-roadmap-task-extract.json` (`extract-roadmap-task.sh:60-80`), but the schema is never passed as `--json-schema`. Similarly, `update-roadmap-status.sh` is marked DEPRECATED (`update-roadmap-status.sh:4`) and does not call Claude at all — no schema needed. **Both schemas are dead.**

**Anomaly D — `implement-roadmap-task-extract.json` mismatches the actual `extract-roadmap-task.sh` output:**
- `implement-roadmap-task-extract.json` does not include a `source` field, but `extract-roadmap-task.sh` always emits `source: "github"` or (implicitly) `"roadmap"` in its output (`extract-roadmap-task.sh:73`). The schema would fail validation on actual output if it were ever applied.

**Anomaly E — `docs` stage reuses `implement-issue-implement.json`:**
- The docs-writing stage (`implement-orchestrator.sh:1517`) uses the implement schema (`implement-issue-implement.json`) rather than a dedicated docs-output schema. This means the LLM is expected to return `commit`/`files_changed` for a documentation update, which is technically correct but the semantic mismatch may cause confusion.

---

## DISPUTED

1. **`needs_deploy` coupling** (`ralph-dependency-analysis.json:50-55`): The design doc claim of "no Hey Soo! coupling" is **disputed** for this field. It encodes a deploy-gated E2E architecture assumption. The field is schema-level generic (array of strings) but semantically implies a deployable-service project topology.

2. **`implement-issue-test.json` active status**: Marked as potentially dead/orphaned above (Anomaly A). If there is a code path that passes this schema not found by grep, that would change the classification. Confidence: high that it is unused, but not 100% without runtime tracing.
