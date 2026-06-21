# Fragment 13 — Runtime I/O Artifacts (Empirical Ground Truth)

Source repo commit: `0dd5d09d641510ee595e0300f2e9422005194d58`
Canonical sample run (batch):   `logs/ralph-20260515-173231/`
Canonical sample run (per-task): `logs/implement-roadmap-task/issue-505-20260515-172600/`

---

## Scope

This fragment documents the **exact shapes** of every runtime-written artifact in the
heysoo reference system as observed from real files. It is the empirical anchor that the
synthesis agent uses to diff against the code-inferred schema.

**Excluded** (noted, not documented):
- `logs/baseline-*/` — non-canonical; contains only raw test runner outputs (`e2e.exit`,
  `e2e.out`, `unit.exit`, `unit.out`).
- `.worktrees/**` — checked-out branch copies; duplicate of main tree at a point in time,
  contain no orchestrator-written metadata of their own.

---

## §1 Root-level live files

Two JSON files are written to the repo root by the orchestrators and live-updated
throughout a run.

### 1.1 `status-ralph.json` — batch-level live status

Written by `batch-orchestrator.sh` / `ralph-loop.sh`. Live-updated on every task
state transition and at batch close.

Source: `/Users/craigperler/Development/heysoo/status-ralph.json`

**Top-level fields:**

| Field | Type | Example / Notes |
|---|---|---|
| `state` | string | `"completed"` — batch terminal state |
| `tasks` | object (keyed by `"#NNN"`) | Per-task runtime entries (see §1.1.1) |
| `dependency_graph` | object (keyed by `"#NNN"`) | Value is array of `"#NNN"` strings (deps) |
| `config` | object | Snapshot of CLI params at launch (see §1.1.2) |
| `progress` | object | Aggregated counters (see §1.1.3) |
| `log_dir` | string | `"logs/ralph-20260515-173231"` — relative path |
| `last_update` | string (ISO 8601 UTC) | `"2026-05-15T23:39:53Z"` |

**§1.1.1 `tasks["#NNN"]` fields:**

| Field | Type | Example / Notes |
|---|---|---|
| `state` | string | `"completed"` / `"running"` / `"retrying"` / `"failed"` / `"blocked"` |
| `attempt` | integer | `2` — 1-based retry count |
| `max_retries` | integer | `3` |
| `depends_on` | array of strings | `[]` or `["#NNN"]` — populated from dep analysis |
| `reason` | string | Human-readable dep reason from analysis |
| `mode` | string | `"full"` / `"lite"` / `"micro"` |
| `mode_reason` | string | Explanation from dep analysis or fallback |
| `unmet_deps` | array | `[]` — deps not yet completed |
| `pid` | integer \| null | OS PID of running orchestrator subprocess; `null` when done |
| `status_file` | string | `"status-ralph-issue-505.json"` — root-relative path |
| `last_error` | string \| null | Last stderr line(s) on failure; `null` when none |
| `retry_after` | string \| null | ISO timestamp when retry becomes eligible; `null` when not retrying |
| `completed_at` | string \| null | `"2026-05-15T19:34:52-04:00"` (local TZ); `null` while running |
| `error_signatures` | array of strings | Deduplicated error pattern strings from failed runs |

**§1.1.2 `config` fields:**

| Field | Type | Example / Notes |
|---|---|---|
| `max_retries` | integer | `3` |
| `max_concurrent` | integer | `3` |
| `cooldown` | integer | `120` — seconds between retries |
| `branch` | string | `"main"` |
| `queue_mode` | boolean | `true` |
| `idle_timeout` | integer | `300` — seconds to wait for new queue items |
| `queue_file` | string | `"ralph-queue.json"` |

**§1.1.3 `progress` fields:**

| Field | Type | Notes |
|---|---|---|
| `total` | integer | Total tasks in batch |
| `completed` | integer | Terminal-success count |
| `running` | integer | Currently executing |
| `blocked` | integer | Blocked on unmet deps |
| `ready` | integer | Queued, not yet started |
| `retrying` | integer | Awaiting cooldown retry |
| `permanently_failed` | integer | Exhausted retries |
| `cascade_blocked` | integer | Blocked because a dep permanently failed |

---

### 1.2 `status-ralph-issue-NNN.json` — per-task live status

Written by `implement-orchestrator.sh` / `implement-issue-orchestrator.sh`. Live-updated
at each stage boundary.

Source: `/Users/craigperler/Development/heysoo/status-ralph-issue-505.json`

**Top-level fields:**

| Field | Type | Example / Notes |
|---|---|---|
| `state` | string | `"completed"` / `"running"` / `"failed"` / `"failure_signature_plateau"` |
| `issue` | string | `"#505"` |
| `base_branch` | string | `"main"` |
| `branch` | string | `"issue-505"` |
| `worktree` | string | Absolute path to git worktree |
| `current_stage` | string | Name of stage currently executing or last completed |
| `substage` | string \| null | Fine-grained sub-step within a stage (e.g. iteration label) |
| `substage_detail` | string \| null | Further detail within substage |
| `current_task` | integer | 1-based index of current implement sub-task |
| `execution_mode` | string | `"full"` / `"lite"` / `"micro"` |
| `stages_skipped` | array of strings | Stage names skipped by mode or `--skip-*` flags |
| `stages_executed` | array of strings | Ordered list of completed stage names |
| `stages` | object (keyed by stage name) | Per-stage detail (see §1.2.1) |
| `tasks` | array of objects | Sub-task list (see §1.2.2) |
| `quality_iterations` | integer | Total quality-loop iterations run |
| `test_iterations` | integer | Total test-loop iterations run |
| `pr_review_iterations` | integer | Total PR-review iterations run |
| `last_update` | string (ISO 8601 UTC) | `"2026-05-15T23:34:27Z"` |
| `log_dir` | string | Relative path: `"logs/implement-roadmap-task/issue-505-20260515-172600"` |
| `stage_counter` | integer | Monotonically increasing stage file counter (matches `stages/.counter`) |
| `current_model` | string | Model slug of most-recent agent invocation |
| `current_provider` | string | `"codex"` or `"claude"` |
| `pr_number` | integer \| null | GitHub PR number once created |
| `pr_url` | string \| null | Full PR URL once created |

**§1.2.1 `stages[name]` per-stage entry fields:**

Common fields (all stages):

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"completed"` / `"running"` / `"failed"` |
| `started_at` | string (ISO 8601 UTC) | Not always present (e.g. `quality_loop`) |
| `completed_at` | string (ISO 8601 UTC) | |

Stage-specific extra fields:

| Stage | Extra fields |
|---|---|
| `implement` | `task_progress` (string, e.g. `"5/5"`) |
| `quality_loop` | `iteration` (integer — last iteration number) |
| `test_loop` | `iteration`, `baseline_failures` (int), `last_regressions` (array\|null), `last_inherited` (array\|null), `last_run_type` (string `"full"`/`"incremental"`), `last_tested_commit` (SHA string), `incremental_files` (array of strings) |
| `pr` | `pr_number` (integer) |
| `pr_review` | `iteration` (integer) |

Observed stages (in execution order for `full` mode):
`setup`, `research`, `evaluate`, `plan`, `implement`, `quality_loop`, `test_loop`,
`docs`, `pr`, `pr_review`, `complete`, `extract`, `verify`

**§1.2.2 `tasks[i]` sub-task fields:**

| Field | Type | Notes |
|---|---|---|
| `id` | integer | 1-based |
| `description` | string | Natural language task description |
| `agent` | string | Agent slug, e.g. `"python-backend-developer"` |
| `quality_tier` | string | `"full"` / `"lite"` / `"none"` |
| `implementation_budget` | string | `"standard"` / `"short"` |
| `depends_on` | array of integers | IDs of prerequisite sub-tasks |
| `status` | string | `"completed"` / `"running"` / `"pending"` |
| `review_attempts` | integer | Number of review cycles taken |

---

### 1.3 `ralph-queue.json` — queue format

Written by external processes (operators or automation) to feed tasks into a running ralph
batch (queue mode). The current real file is `[]` (empty array — queue was drained).

Source: `/Users/craigperler/Development/heysoo/ralph-queue.json`

**Format:** JSON array of batch objects. An empty queue `[]` is the steady state. When
populated, each element is a batch (same shape as the CLI task-list argument). The
orchestrator's `ingest_batch` function reads and removes elements atomically.

No populated real example was present in the observed file. The log confirms the format
is a flat array of task specifiers (e.g. `"#NNN"` strings or structured objects with
`:override` suffixes). See `ralph-loop.sh` comment at `logs/ralph-20260515-173231/ralph.log:28`
(`Queue mode: true`, `Idle timeout: 300s`, `Queue file: ralph-queue.json`).

---

## §2 Batch run directory — `logs/ralph-<TS>/`

Directory created at batch start. Path: `logs/ralph-YYYYMMDD-HHMMSS/`

| File | Always present | Format | Description |
|---|---|---|---|
| `ralph.log` | yes | plain text | Structured timestamped log: `[ISO8601-local] message`. Written by ralph-loop.sh throughout. |
| `summary.json` | yes | JSON | Batch close summary (see §2.1) |
| `shared-baseline.json` | yes | JSON | Pre-existing test failures on base branch (see §2.2) |
| `dependency-analysis.json` | when ≥3 tasks | JSON array | One object per task from LLM dep-analysis (see §2.3) |
| `dependency-analysis.stderr` | when dep analysis runs | text | Raw stderr from dep-analysis agent |
| `dependency-analysis.stream.jsonl` | when dep analysis runs | JSONL | Claude Code telemetry events (one JSON object per line) |
| `task-issue-NNN.log` | yes (one per task) | plain text | Passthrough of that task's orchestrator stdout |
| `learnings-issue-NNN.md` | yes (one per task) | Markdown | Per-attempt failure summaries + learnings (appended on each retry) |

**§2.1 `summary.json` fields:**

| Field | Type | Notes |
|---|---|---|
| `state` | string | Terminal batch state |
| `progress` | object | Same schema as `status-ralph.json > progress` |
| `queue` | object | `{mode, batches_ingested, tasks_from_queue, final_queue_depth}` |
| `tasks` | array of objects | Each: `{key: "#NNN", value: {state, attempt, last_error}}` — abridged snapshot |
| `completed_at` | string (ISO 8601 UTC) | |

**§2.2 `shared-baseline.json` fields:**

| Field | Type | Notes |
|---|---|---|
| `captured_at` | string (ISO 8601 local TZ) | When baseline was captured |
| `base_branch` | string | `"main"` |
| `base_commit` | string | Short SHA of HEAD at capture time |
| `capture_status` | string | `"success"` |
| `failure_names` | array of strings | `"<spec-file>:<line>"` format |

**§2.3 `dependency-analysis.json` fields (per array element):**

Observed keys (all runs use same schema):

| Field | Type | Notes |
|---|---|---|
| `id` | string | `"#NNN"` |
| `depends_on` | array of strings | `"#NNN"` refs |
| `reason` | string | Dependency rationale |
| `mode` | string | `"full"` / `"lite"` / `"micro"` |
| `mode_reason` | string | Rationale for mode choice |
| `needs_deploy` | array | Deploy dependencies (usually `[]`) |

Note: `needs_brainstorm`, `brainstorm_reason`, `brainstorm_topics` are specified in the
schema file (`ralph-dependency-analysis.json`) and referenced in the LLM prompt, but were
**not observed** in any real `dependency-analysis.json` file (all 8 sampled runs use the
**6-field** shape in the table above — `id, depends_on, reason, mode, mode_reason, needs_deploy`).
The `brainstorm_*` fields are the candidate orphans (in schema + prompt, absent from real
output — the in-flight issue #505 feature). See §5.

---

## §3 Per-task run directory — `logs/implement-roadmap-task/<issue>-<TS>/`

Directory: `logs/implement-roadmap-task/issue-NNN-YYYYMMDD-HHMMSS/`

| File | Presence | Format | Description |
|---|---|---|---|
| `orchestrator.log` | always | plain text | Timestamped log from implement-orchestrator.sh |
| `status.json` | always | JSON | Full copy of per-task status (same schema as root `status-ralph-issue-NNN.json`) |
| `cost-summary.md` | after first full run | Markdown | Human-readable cost report (see §3.1) |
| `retrospective.md` | on failure | Markdown | Failure retrospective (see §3.2) |
| `.child_pids` | always | plain text | Single integer: OS PID of child claude/codex process |
| `context/` | always | directory | Named output artifacts from stages (see §3.3) |
| `stages/` | always | directory | Numbered per-stage log files (see §3.4) |

**§3.1 `cost-summary.md` layout:**

```
# Cost Summary

- **Mixed-provider invocations:** N
- **Total wall time:** N.N min
- **Estimated cost:** ~$N.NN USD

## Per-model breakdown

| Model | Invocations | Duration (min) | Est. cost (USD) |
|-------|-------------|----------------|-----------------|
| <model-slug> | N | N | $N |

## Notes

- Token counts come from the provider telemetry payload when available; cache tokens
  are priced at 10% (reads) / 125% (writes) of the base input rate.
- Estimates are order-of-magnitude. Real billing may differ.
- Raw per-invocation data: `context/stage-costs.jsonl`
```

**§3.2 `retrospective.md` layout:**

```
# Orchestrator Run Retrospective

- **Task:** #NNN
- **Final state:** `<state-slug>`
- **Current stage at failure:** `<stage-name>`
- **Log directory:** `logs/implement-roadmap-task/...`

## Detected patterns

<either "No known failure patterns matched automatically." or a list>

## Next step

Run `/improvement-loop` to analyze this failure and suggest pipeline fixes.
```

---

### §3.3 `context/` directory

Named output artifacts written by each stage as the canonical structured output of that
stage's LLM invocation.

| File | Present | Format | Description |
|---|---|---|---|
| `setup-output.json` | always | JSON | `{status, worktree, branch}` |
| `extract-output.json` | always | JSON | Full issue extraction: `{status, task: {task_id, title, status, full_title, description[], related_files[]}}` |
| `research-output.json` | always | JSON | `{status, context: {issue_title, issue_body, related_files[]}, error, commit, files_changed[]}` |
| `evaluate-output.json` | always | JSON | `{status, approach, rationale, risks[], summary, error, commit, files_changed[]}` |
| `plan-output.json` | always | JSON | `{status, plan_path, tasks[]}` — tasks array matches schema from §1.2.2 |
| `tasks.json` | after plan | JSON array | Copy of planned task list (same shape as `tasks[]` in status) |
| `baseline-failures.json` | always | JSON | Per-task baseline capture: `{captured_at, base_branch, base_commit, capture_status, failure_names[]}` |
| `review-comments.json` | after first review | JSONL (not valid single JSON) | One JSON object per line; each line: `{result, summary, issues[], status, commit, files_changed[], error}` |
| `stage-costs.jsonl` | always | JSONL | One row per stage invocation (see §3.3.1) |
| `convergence-task-N.txt` | per task with reviews | plain text | Deduplicated review findings, one per line (`file:finding`) |
| `last-tested-commit` | after test_loop | plain text | Single SHA, no newline |

**§3.3.1 `stage-costs.jsonl` row fields:**

| Field | Type | Notes |
|---|---|---|
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

### §3.4 `stages/` directory

| File | Presence | Format | Description |
|---|---|---|---|
| `.counter` | always | plain text | Current monotonic counter value (integer, no newline) |
| `index.md` | always | Markdown | Human-readable stage log index table (see §3.4.1) |
| `NN-<stage>.log` | always | plain text | Stage stdout from the agent or direct runner |
| `NN-<stage>.stderr` | agent stages | plain text | Stage stderr |
| `NN-<stage>.md` | agent stages | Markdown | Structured output written by the agent to a temp file then copied |
| `NN-<stage>.codex-events.jsonl` | codex-provider stages | JSONL | Claude Code event stream (system init, assistant messages, tool calls) |
| `NN-<stage>.codex-last-message.json` | codex-provider stages | JSON | Final structured output message from the agent (same schema as the stage's context/ output) |
| `NN-<stage>.stream.jsonl` | claude-provider stages | JSONL | Claude Code telemetry stream (system init events, rate limit info) |

For direct-runner stages (e.g. `test-direct-incremental`, `test-direct-full`):
only the `.log` file is written — no `.md`, `.stderr`, `.codex-events.jsonl`, or
`.codex-last-message.json`.

**§3.4.1 `stages/index.md` layout:**

```markdown
# Stage Log Index — Task #NNN

| # | Stage | Agent | Provider | Model | Timestamp | Log file |
|---|-------|-------|----------|-------|-----------|----------|
| NN | <stage-name> | <agent-slug> | <provider> | <model-slug> | HH:MM:SS | NN-<stage>.log |
```

Columns:
- `#` — zero-padded two-digit counter (matches file prefix)
- `Stage` — stage name string (matches the slug used in filenames)
- `Agent` — agent slug or `"default"` or `"direct"`
- `Provider` — `"codex"` / `"claude"` / `"n/a"` (direct runners)
- `Model` — model slug or absent for direct runners
- `Timestamp` — local-time `HH:MM:SS` of stage start
- `Log file` — relative filename

---

## §4 `docs/roadmap.md` — task source format

Source: `/Users/craigperler/Development/heysoo/docs/roadmap.md`

The roadmap is a **GitHub Issues pointer**, not a self-contained task list. Its structure:

- Phase and milestone descriptions in prose + bullet lists.
- Tasks are NOT enumerated in the file with machine-readable IDs.
- The header explicitly states: _"All tasks are tracked as **GitHub Issues**. See the [issue list](https://github.com/cperler/heysoo/issues) for current work."_
- Milestone completion is tracked as `(N/N)` suffix on bullet items.
- The roadmap serves as a human-readable product map; the canonical task source for the
  orchestrator is the GitHub issue list, accessed via `gh issue view NNN`.

---

## §5 Candidate Orphan Fields / DISPUTED

The following fields appear in JSON schemas or code paths but were **not observed** in
any real artifact files sampled:

| Field | Expected location | Evidence of writer | DISPUTED? |
|---|---|---|---|
| `needs_brainstorm` | `dependency-analysis.json[i]` | In LLM prompt and schema (`ralph-dependency-analysis.json`); fallback emits `false` | YES — schema specifies it required, but all 8 real files lack it; suggests the LLM consistently omits it and the fallback path alone writes it to in-memory status only |
| `brainstorm_reason` | `dependency-analysis.json[i]` | Same as above | YES |
| `brainstorm_topics` | `dependency-analysis.json[i]` | Same as above | YES |
| `progress.brainstorm_pending` | `status-ralph.json > progress` | Referenced in issue #505 research as a field that "does not yet exist"; the feature was the subject of this task | YES — not present in observed status file; was to be added by the #505 implementation |
| `tasks["#NNN"].needs_brainstorm` | `status-ralph.json > tasks[key]` | Same as above | YES — likewise absent in observed file |
| `stages.extract` | `status-ralph-issue-NNN.json > stages` | Present in observed file with `{started_at, status, completed_at}` keys but `stages_executed` list (from attempt 1 learnings) shows `"extract"` in the executed list for a failed run only | UNCERTAIN — may be a stage that only runs on failure or specific mode |
| `stages.verify` | `status-ralph-issue-NNN.json > stages` | Present in observed file with only `{completed_at, status}` — missing `started_at` unlike all other stages | ANOMALY — `started_at` absent; possible writer bug |
| `tasks[i].review_attempts` | `status-ralph-issue-NNN.json > tasks[i]` | Present in observed file but not in `context/tasks.json` (which is a copy of the plan output before execution) | EXPECTED — added by orchestrator as review cycles complete, not from planner |
| `summary.json > tasks[i].value.last_error` | `logs/ralph-*/summary.json` | Present with full error text in observed file | CANDIDATE ORPHAN — only meaningful for debugging; no known consumer reads `summary.json` downstream |
