# Fragment: 09 — Entry points & periphery (wrappers + roadmap I/O)

Source commit: `0dd5d09d641510ee595e0300f2e9422005194d58` (repo `/Users/craigperler/Development/heysoo`)
Files mapped:
- `/Users/craigperler/Development/heysoo/.claude/scripts/implement-roadmap-task-orchestrator.sh` (1–16)
- `/Users/craigperler/Development/heysoo/.claude/scripts/implement-issue-orchestrator.sh` (1–31)
- `/Users/craigperler/Development/heysoo/.claude/scripts/batch-runner.sh` (1–342)
- `/Users/craigperler/Development/heysoo/.claude/scripts/extract-roadmap-task.sh` (1–236)
- `/Users/craigperler/Development/heysoo/.claude/scripts/update-roadmap-status.sh` (1–102)

Schemas cross-referenced (read-only):
- `.claude/scripts/schemas/implement-roadmap-task-extract.json`
- `.claude/scripts/schemas/implement-roadmap-task-update.json`

---

## 1. Role & entry points — who invokes it, with what argv

These five scripts form the **periphery** of the harness: the thin entry-point
wrappers, the batch driver, and the roadmap-as-task-source I/O. None of them is
the engine; the engine is `implement-orchestrator.sh` (103 KB, not in this
fragment's scope), which all wrappers `exec` into.

| Script | Role | Invoked by | Delegates to |
|---|---|---|---|
| `implement-roadmap-task-orchestrator.sh` | Backward-compat wrapper, pure pass-through | Humans / older callers using the legacy roadmap-task entry name | `exec implement-orchestrator.sh "$@"` (`implement-roadmap-task-orchestrator.sh:15`) |
| `implement-issue-orchestrator.sh` | Backward-compat wrapper, **argv-translating** | Humans / older callers using `--issue N` | `exec implement-orchestrator.sh "${args[@]}"` after rewriting `--issue N` → `--task #N` (`implement-issue-orchestrator.sh:30`) |
| `batch-runner.sh` | Per-issue 2-stage driver (implement-issue → process-pr), emits one JSON result line | `handle-issues` batch loop (per header comment, `batch-runner.sh:2`,`6`) | `claude -p "/implement-issue …"` then `claude -p "/process-pr …"` (`batch-runner.sh:153`,`253`) |
| `extract-roadmap-task.sh` | TASK-SOURCE reader: resolve a task ID → normalized task JSON | The engine (`implement-orchestrator.sh:492`) calls it as a **plain bash subprocess and parses stdout with `jq`** — **no `--json-schema`, no runtime validation**; `implement-roadmap-task-extract.json` only *documents* the contract (DEAD schema, never enforced) | `gh issue view` (GitHub branch) or `grep`/`awk`/`sed` over `docs/roadmap.md` (roadmap branch) |
| `update-roadmap-status.sh` | TASK-SINK writer: mark a roadmap task complete | The engine, on successful task completion; **DEPRECATED**, no Claude/schema call — `implement-roadmap-task-update.json` is likewise DEAD/documentation-only | `sed -i` over `docs/roadmap.md` + `git add`/`git commit` |

**Entry argv contracts:**
- `implement-roadmap-task-orchestrator.sh --task <id> --branch <b> [--lite] [--learnings-file …]` — passes everything through verbatim. `--task` accepts `1.5.2`, `#123`, or `123` (header examples, `:9–12`).
- `implement-issue-orchestrator.sh --issue <N> --branch <b> [--agent <name>] [--lite] …` — only `--issue` is intercepted; all other flags pass through (`:23–26`).
- `batch-runner.sh <issue_number> <base_branch>` — **positional**, both required (`batch-runner.sh:31–32`).
- `extract-roadmap-task.sh <task-id>` — single positional arg; `#N` → GitHub, `X.Y[.Z]` → roadmap (`extract-roadmap-task.sh:23–27`,`36`,`95`).
- `update-roadmap-status.sh <task-id>` — single positional arg; `#…` → no-op success (`update-roadmap-status.sh:23–34`).

---

## 2. Inputs — flags, env vars, files read

**implement-roadmap-task-orchestrator.sh**
- No flag parsing of its own. `SCRIPT_DIR` derived from `BASH_SOURCE` (`:14`). Reads nothing; forwards `$@`.

**implement-issue-orchestrator.sh**
- `--issue <N>` (required value; missing value → `exit 3`, `:19`). Translated to `--task "#N"` (`:20`).
- All other args collected into `args[]` untouched (`:24`). `SCRIPT_DIR` from `BASH_SOURCE` (`:13`).

**batch-runner.sh**
- Positional `$1` ISSUE_NUMBER, `$2` BASE_BRANCH — both required via `${…:?}` (`:31–32`).
- No env vars consumed for config (relies on ambient `claude`/`gh`/`git`/`jq` auth/context).
- Reads back its own temp files `IMPLEMENT_OUTPUT`/`PROCESS_OUTPUT` (`mktemp`, `:148`,`250`).
- Creates `logs/` dir; lock file `logs/.handle-issues.lock` (`:42`,`47`).

**extract-roadmap-task.sh**
- Positional `$1` TASK_ID; exactly one arg required (`:23`).
- `ROADMAP_FILE="docs/roadmap.md"` — **relative path**, CWD-dependent (`:20`).
- GitHub branch reads `gh issue view <N> --json title,body,labels,state` (`:40`).
- Roadmap branch reads `docs/roadmap.md` (must exist, `:89`).

**update-roadmap-status.sh**
- Positional `$1` TASK_ID; exactly one arg required (`:23`).
- `ROADMAP_FILE="docs/roadmap.md"` — relative (`:20`).
- Reads `docs/roadmap.md` for the task line (`:47`,`54`).

---

## 3. Outputs — files written, exit codes, side effects

**implement-roadmap-task-orchestrator.sh** — no output of its own; `exec` replaces process, so exit code is the engine's.

**implement-issue-orchestrator.sh** — same, except it can `exit 3` itself if `--issue` value missing (`:19`). Otherwise `exec`'s the engine.

**batch-runner.sh**
- **stdout**: exactly one JSON object via `output_json()` (`:89–101`). Always-present fields: `stage`, `status`, `issue` (number), `base_branch`. Conditionally added: `pr` (number), `session_id`, `retry_after` (number|null), `reset_at` (string), `error`, `follow_up_issues` (array). Header documents the full union (`:9–20`).
- `status` values observed: `success`, `rate_limit`, `error`, `changes_requested`, `approved` (`:158`,`165`,`190`,`262`,`264`,`296`). Note header only advertises `success|rate_limit|error` (`:12`) — see §9.
- **Log file**: `logs/batch-runner-<YYYYmmdd-HHMMSS>-issue-<N>.log` (`:44`), appended throughout; full stage output `cat`'d in (`:241`,`325`).
- **Lock file**: `logs/.handle-issues.lock` containing PID (`:68`); removed via `trap release_lock EXIT` (`:81`).
- **Exit codes** (`:36–40`): `0` success, `10` rate limit, `20` parse error (PR not found), `30` logic error (stage failed), `40` fatal (lock contention).
- **Side effects**: spawns `claude -p` (which itself touches git/gh/network), `gh pr list` fallback (`:222`), temp files removed.

**extract-roadmap-task.sh**
- **stdout** JSON on success (`{status:"success", task:{…}}`); conforms to `implement-roadmap-task-extract.json`.
- **stderr** JSON `{status:"error", error:…}` on failure (`:41`,`96`,`108`).
- **Exit codes** (`:13–16`): `0` success, `4` task/issue not found, `1` other (bad args / bad ID format / missing roadmap file).
- No file writes, no git. GitHub branch performs a network read (`gh issue view`).

**update-roadmap-status.sh**
- **stdout** JSON `{status:"success", commit:…, summary:…}` (`:55`,`92–99`); stderr JSON on error. Conforms to `implement-roadmap-task-update.json`.
- **Exit codes** (`:16–17`): `0` success (incl. no-op and already-complete), `1` error.
- **Side effects**: rewrites `docs/roadmap.md` in place via `sed -i.tmp` (`:64`); writes/removes `.bak` and `.tmp` (`:61`,`76`); `git add` + `git commit --no-verify` (`:80`,`83`). This is the only script in the fragment that mutates the repo.

---

## 4. Control flow — state machines, loops, caps

**Wrappers** — no control flow beyond:
- issue wrapper: single `while [[ $# -gt 0 ]]` argv loop, one special case `--issue` (`implement-issue-orchestrator.sh:16–28`), then `exec`. No retries, no loop caps.

**batch-runner.sh** — strictly linear two-stage pipeline, **no loops** (the retry/wait loop lives in the unseen `handle-issues` caller):
1. `acquire_lock` (`:84`) → if live lock held, `exit 40` (`:60`); stale lock (PID not alive) removed (`:62–64`).
2. STAGE 1 implement-issue (`:153`). On non-zero exit: if `check_rate_limit` → emit `rate_limit`, `exit 10` (`:164–187`); else emit `error`, `exit 30` (`:205–210`).
3. PR-number extraction (`:215`); regex-on-output first, then `gh pr list` fallback (`:218–230`); if still empty → `exit 20` (`:232–238`).
4. STAGE 2 process-pr (`:253`). Same rate-limit/error branching → `exit 10`/`exit 30` (`:271–318`). On success, status refined to `changes_requested`/`approved` by grepping output (`:261–265`).
5. Emit final JSON with `pr` + `follow_up_issues` (`:337–339`).
`set -euo pipefail` at `:29`. Lock released via EXIT trap (`:81`).

**extract-roadmap-task.sh** — branch on TASK_ID shape:
- `^#[0-9]+$` → GitHub branch, single `gh issue view`, map state→status (`case`, `:51–55`), build JSON, `exit 0` (`:36–82`).
- `^[0-9]+(\.[0-9]+)+$` → roadmap branch (`:95`). Else → error exit 1 (`:96–98`).
- Roadmap branch: `grep -n` for task line (`:105`); if missing `exit 4` (`:107`). Then four independent `awk`/`sed` extractions (status, title, description bullets, milestone, phase) — no loop, single forward `awk` scans bounded by "stop at empty line / next `**Task` / `^##` heading" (`:139`).

**update-roadmap-status.sh** — linear guard chain:
`#…` no-op (`:31`) → file exists (`:37`) → task line exists else exit 1 (`:47`) → already-complete short-circuit (`:54`) → backup → `sed -i` → verify-or-restore (`:67–72`) → commit-or-error (`:86`). No loops.

---

## 5. External invocations — verbatim

**batch-runner.sh**
- Stage 1 (`:153–156`):
  ```
  claude -p "/implement-issue $ISSUE_NUMBER $BASE_BRANCH" \
      --dangerously-skip-permissions \
      --output-format json
  ```
- Stage 2 (`:253–256`):
  ```
  claude -p "/process-pr $PR_NUMBER $ISSUE_NUMBER $BASE_BRANCH" \
      --dangerously-skip-permissions \
      --output-format json
  ```
- PR fallback (`:222–225`):
  ```
  gh pr list --state open --json number,title \
      --jq ".[] | select(.title | test(\"issue-${ISSUE_NUMBER}[^0-9]\") or test(\"issue-${ISSUE_NUMBER}\$\")) | .number"
  ```
- No model pin, no schema flag on the `claude` calls (relies on the `/implement-issue` and `/process-pr` skill definitions for model/schema).

**extract-roadmap-task.sh**
- `gh issue view "$ISSUE_NUM" --json title,body,labels,state` (`:40`). (Note: `labels` requested but never used downstream.)
- Many `jq -n` constructors; roadmap text mined with `grep`/`awk`/`sed` only (no claude/codex).

**update-roadmap-status.sh**
- `git add "$ROADMAP_FILE"` (`:80`).
- `git commit -m "$COMMIT_MSG" --no-verify 2>&1` (`:83`) — commit message `chore: mark Task $TASK_ID complete in roadmap` (`:79`).
- `sed -i.tmp "s/^\(- \)\[ \]\( \*\*Task ${TASK_ID_SED}:[^*]*\*\*\)\$/\1[x]\2 ✓/"` (`:64`).

No `claude`/`codex` invocations in either roadmap script — they are deterministic text tools.

---

## 6. Constants & tunables

- Exit-code constants: `EXIT_SUCCESS=0 / EXIT_RATE_LIMIT=10 / EXIT_PARSE_ERROR=20 / EXIT_LOGIC_ERROR=30 / EXIT_FATAL=40` (`batch-runner.sh:36–40`); extract uses `4`/`1`/`0` (`extract-roadmap-task.sh:13–16`); issue wrapper uses `3` (`implement-issue-orchestrator.sh:19`).
- Error-message truncation cap: `2000` chars (`batch-runner.sh:192–193`,`298–299`).
- `LOG_DIR="logs"` (`batch-runner.sh:42`); `ROADMAP_FILE="docs/roadmap.md"` (`extract:20`, `update:20`).
- Rate-limit detection regex (`batch-runner.sh:110`,`114`):
  `rate.limit|429|403.*forbidden|secondary.*rate|API rate limit|too many requests|quota.exceeded|retry.after`.
- Task-ID format regexes: `^#[0-9]+$` and `^[0-9]+(\.[0-9]+)+$` (`extract:36`,`95`).
- No timeouts, no sleeps, no pricing, no model pins in any of the five scripts.

---

## 7. Failure handling

**batch-runner.sh**
- No in-script retries — it runs each stage once and reports a status code; **retry/backoff is delegated to the `handle-issues` caller** (the `retry_after`/`reset_at` fields exist to feed that caller, `:182–183`,`289–290`).
- Rate-limit path: detect via regex on combined stdout+stderr (claude run with `2>&1`, `:156`), extract `retry_after` (digits after "retry after") and `reset_at` (RFC3339 or `reset at …`) (`extract_rate_limit_info`, `:122–139`), emit and `exit 10`.
- PR-extraction fallback chain: output-regex → `gh pr list` query → give up (`exit 20`) (`:215–238`).
- `session_id` parse failures degrade to empty string with a logged WARNING; never fatal (`:171–176`,`196–201`,`277–282`,`302–307`).
- Lock: stale-lock detection via `kill -0 <pid>` (`:56`); live lock → `exit 40` (fatal, stop batch).

**update-roadmap-status.sh**
- Backup-before-edit (`cp …bak`, `:61`) + verify-after-edit; on verify failure **restores the backup** (`mv …bak`, `:69`) and exits 1 (`:67–72`).
- `git commit … || true` so a failed commit doesn't abort under `set -e`; absence of a parsed SHA → error exit 1 (`:83–88`).

**extract-roadmap-task.sh**
- `gh issue view` failure → error JSON + `exit 4` (`:40–44`).
- Missing task / bad format / missing file → distinct error JSON paths (`:96`,`108`,`89`).
- No retries.

**Wrappers** — no failure handling beyond the `--issue` arg guard (`exit 3`).

---

## 8. Coupling — generic vs Hey Soo!-specific

| Item | Verdict | Generic shape it should take |
|---|---|---|
| **`docs/roadmap.md` as the task source** | Hey Soo!-coupled (hard-coded relative path, `extract:20`/`update:20`) | Pluggable **task-source provider** interface: `resolve(task_id) -> TaskSpec`. roadmap.md and GitHub Issues are two implementations behind it. The engine should depend on the normalized JSON contract, not the file. |
| **Roadmap markdown grammar** (see format below) | Hey Soo!-specific surface syntax | The provider owns parsing; the spec only fixes the *normalized output schema* (`implement-roadmap-task-extract.json`). |
| **Dotted task numbering `X.Y[.Z]`** + `#N` issue form | Mixed: `#N`/GitHub is generic-ish, dotted hierarchy is Hey Soo! roadmap convention | Treat task IDs as opaque strings the provider validates; don't bake `^[0-9]+(\.[0-9]+)+$` into the engine. |
| **GitHub Issues backend** (`gh issue view`, `gh pr list`) | Generic-ish but VCS-host-coupled to GitHub | Abstract behind an issue/PR provider; the rate-limit and PR-number heuristics are GitHub-shaped. |
| **`/implement-issue`, `/process-pr` slash commands** | Hey Soo! skill names | Configurable command names / stage definitions. |
| **PR-title convention `issue-<N>`** (`batch-runner.sh:224`) | Hey Soo!-specific naming | Provider-supplied PR↔task linkage, not a title regex. |
| **`✓` completion marker + `[x]` checkbox** in roadmap | Hey Soo!-specific | Status writeback is part of the task-source provider's `mark_complete()`; format is its concern. |
| **`git commit` of roadmap on completion** | Hey Soo!-specific side effect | Optional writeback hook; not all task sources commit. |

### Roadmap.md format that `extract-roadmap-task.sh` expects (TASK-SOURCE grammar)
Reconstructed from the parser (`extract-roadmap-task.sh:105`,`131`,`139`,`153`,`172`):
- **Task line** (the anchor): `- [ ] **Task X.Y.Z: <title>**` — leading `- [`, a single status char in `[.]`, then literally `**Task <dotted-id>: <title>**`.
  - Status char: `[x]` → completed, `[ ]` → pending (`:117–120`); a trailing `✓` anywhere on the line also forces completed (`:126`).
  - Title = text between `Task X.Y.Z: ` and the closing `**` (`:131`).
- **Description bullets**: indented `  - <text>` lines immediately under the task line, terminated by the first blank line, next `- [.] **Task`, or any `##` heading (`:136–149`).
- **Milestone**: nearest preceding `### Milestone <id>: <title>` heading (`:153`,`166–167`).
- **Phase**: nearest preceding `## Phase <id>: <title>` heading (`:172`,`185–186`).
- Update script's matching `sed` only flips `- [ ]` → `- [x]` and appends ` ✓` when the line ends exactly at the closing `**` (`update:64`).

**Normalized output (the contract worth keeping)** — `{status, task:{task_id, title, status∈{pending,completed,deferred}, full_title, description:[…], milestone:{id,title}|null, phase:{id,title}|null, source:"roadmap"|"github"}}` (`extract:67–79`,`221–233`; schema `implement-roadmap-task-extract.json`). Note: GitHub branch sets `milestone`/`phase` to `null` (`:75–76`) and derives `description` by splitting the issue body on newlines (`:58`).

---

## 9. Anomalies, dead code, contradictions

1. **The roadmap branch of `extract-roadmap-task.sh` is effectively dead against the live repo.** At commit `0dd5d09`, `docs/roadmap.md` contains **zero** `- [ ] **Task X.Y.Z:` lines, **zero** `### Milestone` headings, and zero checkbox lines (verified by grep). The file explicitly states "All tasks are tracked as **GitHub Issues**" and uses `**Milestone X.Y**:` *bullets* (not `### Milestone` *headings*) and `## Phase N:` headings. So: any `extract-roadmap-task.sh X.Y.Z` call now returns `exit 4` (task not found). Only the `#N` GitHub branch is live. `update-roadmap-status.sh` is self-described as **DEPRECATED** (`update:4–6`) and is a guaranteed no-op for `#N` IDs. **The entire roadmap-markdown task source is legacy.** This strongly supports the §8 recommendation to model it as one (now-dormant) provider behind a pluggable interface.

2. **`batch-runner.sh` status enum mismatch (header vs code).** Header advertises `status: success|rate_limit|error` (`:12`) but the code also emits `changes_requested` and `approved` (`:262`,`264`) and the success branch passes `$PROCESS_STATUS` straight through (`:337`). Downstream `handle-issues` must handle the extra values; the header under-documents the contract.

3. **`gh issue view … labels` fetched but unused** (`extract:40`) — `labels` is in `--json` but never read; harmless dead field.

4. **`extract`'s GitHub-branch `status` enum can violate its own schema.** It can emit `status:"unknown"` (`:54`) for non-OPEN/CLOSED states, but `implement-roadmap-task-extract.json` restricts task.status to `pending|completed|deferred` (schema lines 22–24). `unknown` is also producible in the roadmap branch (`:121`). Schema-validation of extract output would reject these.

5. **`update`-script SHA parsing is brittle.** It scrapes the commit SHA from `git commit` *stdout* by grabbing the first `[...]` group (`:84`), which actually captures the branch-name/short-SHA token like `[main abc1234]` → yields `main abc1234`, not a clean SHA. The schema only promises a "Git commit SHA" string (`implement-roadmap-task-update.json:11`); the value is imprecise. Low severity (informational field).

6. **Relative `docs/roadmap.md` path** (`extract:20`,`update:20`) makes both scripts **CWD-sensitive** — they only work when invoked from the repo root. No guard validates CWD; combined with anomaly 1 this is a latent footgun. (Possible contradiction with any spec claim of location-independence — flag for `docs/orchestration-template.md` reconciliation.)

7. **`set -euo pipefail` vs `grep … || true`/`|| echo ""` patterns** are used deliberately throughout to keep non-matches non-fatal (e.g. `batch-runner.sh:215`,`322`; `update:83`). Not a bug, but worth noting the engine relies on these guards for correctness under `-e`.

### DISPUTED
- **D1 — Roadmap task source: dead vs dormant-by-design.** I assert the roadmap-markdown path is dead code at this commit (grep shows no parseable lines, and the roadmap text says tasks moved to GitHub Issues). An alternative reading is that it is *intentionally retained legacy* for older branches/repos that still carry `- [ ] **Task` roadmaps, i.e. dormant rather than dead. I could not find a live roadmap with the expected grammar in this repo to settle it. Resolve against `docs/orchestration-template.md` intent.
- **D2 — Who invokes `extract-roadmap-task.sh` / `update-roadmap-status.sh`.** I attribute invocation to the engine `implement-orchestrator.sh` based on the schema pairing and naming, but I did **not** read `implement-orchestrator.sh` (out of fragment scope, 103 KB). The exact call sites/argv from the engine are unverified here and should be confirmed by the fragment covering the engine.
- **D3 — `batch-runner.sh` caller.** Header comment says `handle-issues` parses its JSON (`:6`), but I did not see `handle-issues` to confirm it consumes exit codes 10/20/30/40 and the extra status values. Cross-reference with whichever fragment maps the batch loop.
