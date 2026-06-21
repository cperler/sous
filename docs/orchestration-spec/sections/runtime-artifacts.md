# Runtime Artifacts (Empirical Ground Truth)

Source commit: `0dd5d09d641510ee595e0300f2e9422005194d58`
Canonical batch run: `logs/ralph-20260515-173231/`
Canonical per-task run: `logs/implement-roadmap-task/issue-505-20260515-172600/`

---

## Root-level live files

### `status-ralph.json` — batch-level live status

Written by `batch-orchestrator.sh` / `ralph-loop.sh`. Live-updated on every task state
transition and at batch close.

**Top-level (7 keys):**

| Field | Type | Notes |
|-------|------|-------|
| `state` | string | `"completed"` / `"running"` / … — batch terminal state |
| `tasks` | object (keyed `"#NNN"`) | Per-task runtime entries (see below) |
| `dependency_graph` | object (keyed `"#NNN"`) | Value = array of `"#NNN"` dep strings |
| `config` | object | CLI params snapshot at launch (`max_retries`, `max_concurrent`, `cooldown`, `branch`, `queue_mode`, `idle_timeout`, `queue_file`) |
| `progress` | object | Aggregated counters (see below) |
| `log_dir` | string | Relative path to batch log dir |
| `last_update` | string (ISO 8601 UTC) | |

**`tasks["#NNN"]` — 14 fields per task entry:**

| Field | Type | Notes |
|-------|------|-------|
| `state` | string | `"completed"` / `"running"` / `"retrying"` / `"failed"` / `"blocked"` |
| `attempt` | integer | 1-based retry count |
| `max_retries` | integer | Retry cap |
| `depends_on` | array of strings | `"#NNN"` deps from dep analysis |
| `reason` | string | Human-readable dep reason |
| `mode` | string | `"full"` / `"lite"` / `"micro"` |
| `mode_reason` | string | Rationale from dep analysis or fallback |
| `unmet_deps` | array | Deps not yet completed |
| `pid` | integer \| null | OS PID of running orchestrator subprocess; `null` when done |
| `status_file` | string | Root-relative path, e.g. `"status-ralph-issue-505.json"` |
| `last_error` | string \| null | Last stderr line(s) on failure |
| `retry_after` | string \| null | ISO timestamp when retry becomes eligible |
| `completed_at` | string \| null | Local-TZ ISO timestamp; `null` while running |
| `error_signatures` | array of strings | Deduplicated error pattern strings from failed runs |

**`progress` fields (8 counters):**
`total`, `completed`, `running`, `blocked`, `ready`, `retrying`, `permanently_failed`,
`cascade_blocked`.

---

### `status-ralph-issue-NNN.json` — per-task live status

Written by `implement-orchestrator.sh`. Live-updated at each stage boundary.

**Top-level (24 keys):**

| Field | Type | Notes |
|-------|------|-------|
| `state` | string | `"completed"` / `"running"` / `"failed"` / `"failure_signature_plateau"` |
| `issue` | string | `"#505"` |
| `base_branch` | string | |
| `branch` | string | `"issue-505"` |
| `worktree` | string | Absolute path to git worktree |
| `current_stage` | string | Stage currently executing or last completed |
| `substage` | string \| null | Fine-grained sub-step within a stage |
| `substage_detail` | string \| null | Further detail within substage |
| `current_task` | integer | 1-based index of current implement sub-task |
| `execution_mode` | string | `"full"` / `"lite"` / `"micro"` |
| `stages_skipped` | array of strings | Stage names skipped by mode or `--skip-*` flags |
| `stages_executed` | array of strings | Ordered list of completed stage names |
| `stages` | object (keyed by stage name) | Per-stage detail (see below) |
| `tasks` | array of objects | Sub-task list (see below) |
| `quality_iterations` | integer | |
| `test_iterations` | integer | |
| `pr_review_iterations` | integer | |
| `last_update` | string (ISO 8601 UTC) | |
| `log_dir` | string | Relative path |
| `stage_counter` | integer | Monotonically increasing; matches `stages/.counter` |
| `current_model` | string | Model slug of most-recent agent invocation |
| `current_provider` | string | `"codex"` or `"claude"` |
| `pr_number` | integer \| null | GitHub PR number once created |
| `pr_url` | string \| null | Full PR URL once created |

**`stages[name]` common fields:**
`status` (`"completed"` / `"running"` / `"failed"`), `started_at` (ISO 8601 UTC — see
anomaly below), `completed_at` (ISO 8601 UTC).

Stage-specific extra fields: `implement` → `task_progress` (string `"5/5"`);
`quality_loop`/`pr_review` → `iteration` (integer); `test_loop` → `iteration`,
`baseline_failures`, `last_regressions`, `last_inherited`, `last_run_type`, `last_tested_commit`,
`incremental_files`; `pr` → `pr_number`.

Observed full-mode stage sequence: `setup`, `research`, `evaluate`, `plan`, `implement`,
`quality_loop`, `test_loop`, `docs`, `pr`, `pr_review`, `complete`, `extract`, `verify`.

**`tasks[i]` sub-task fields:**
`id` (integer, 1-based), `description`, `agent`, `quality_tier` (`"full"`/`"lite"`/`"none"`),
`implementation_budget` (`"standard"`/`"short"`), `depends_on` (array of integers),
`status` (`"completed"`/`"running"`/`"pending"`), `review_attempts` (integer — added by
orchestrator as review cycles complete, not present in the planner's initial `context/tasks.json`).

---

## `stage-costs.jsonl` — per-stage cost rows

Written to `context/stage-costs.jsonl` in the per-task run directory.

**10 fields per row:**

| Field | Type | Notes |
|-------|------|-------|
| `label` | string | Stage name, e.g. `"research"` or `"evaluate fallback"` |
| `model` | string | Model slug |
| `provider` | string | `"codex"` or `"claude"` |
| `start_epoch` | integer | Unix timestamp (seconds) |
| `duration_seconds` | integer | Wall-clock duration |
| `input_tokens` | integer | 0 when telemetry unavailable |
| `output_tokens` | integer | 0 when telemetry unavailable |
| `cache_read_tokens` | integer | 0 when telemetry unavailable |
| `cache_write_tokens` | integer | 0 when telemetry unavailable |
| `exit_code` | integer | Process exit code |

---

## `cost-summary.md`

Human-readable cost report written to the per-task run directory after first full run.
Sections: scalar totals (mixed-provider invocations, total wall time, estimated cost USD),
per-model breakdown table (model, invocations, duration, estimated cost), notes on token
pricing methodology. Raw data source: `context/stage-costs.jsonl`.

---

## Log trees

**Batch run directory** — `logs/ralph-YYYYMMDD-HHMMSS/`:
`ralph.log` (structured timestamped), `summary.json`, `shared-baseline.json`,
`dependency-analysis.json` (when ≥3 tasks), `dependency-analysis.stderr`,
`dependency-analysis.stream.jsonl`, `task-issue-NNN.log` (one per task),
`learnings-issue-NNN.md` (appended per retry).

**Per-task run directory** — `logs/implement-roadmap-task/issue-NNN-YYYYMMDD-HHMMSS/`:
`orchestrator.log`, `status.json` (full copy of per-task status), `cost-summary.md`,
`retrospective.md` (on failure), `.child_pids`, `context/` (named stage outputs),
`stages/` (numbered per-stage logs with `.log`, `.stderr`, `.md`, `.codex-events.jsonl`,
`.codex-last-message.json`, `.stream.jsonl` as applicable; `index.md`; `.counter`).

---

## Orphan / in-flight fields

| Field | Location | Status |
|-------|----------|--------|
| `brainstorm_*` (`needs_brainstorm`, `brainstorm_reason`, `brainstorm_topics`) | `dependency-analysis.json[i]` | **In-flight (issue #505):** present in `ralph-dependency-analysis.json` schema and in the LLM prompt, but absent from all 8 sampled real `dependency-analysis.json` files. The LLM consistently omits them; the fallback path writes them to in-memory status only. Feature was the subject of task #505, not yet landed at this commit. |
| `progress.brainstorm_pending`, `tasks["#NNN"].needs_brainstorm` | `status-ralph.json` | **In-flight (issue #505):** referenced in #505 research as fields that "do not yet exist"; absent from observed status file. |
| `stages.verify` — missing `started_at` | `status-ralph-issue-NNN.json > stages` | **Writer omission (anomaly):** observed file has only `{completed_at, status}` for the `verify` stage, lacking `started_at` unlike all other stages. Likely a bug in the writer. |
| `stages.quality_loop` — missing `started_at` | `status-ralph-issue-NNN.json > stages` | **Writer omission (anomaly):** same pattern; `started_at` absent for `quality_loop`. |
