# Run-level settings: process-boundary persistence audit (#206)

**Source:** self-improvement follow-up filed from the #196 review (PR #204).
**Trigger:** #196 found that `max_filed_followups`, a run-wide cap chosen at run-create
time, was lost by the time a task finished — because the CLI subcommand that finalizes a
task rebuilds the Engine from constructor defaults. This audit generalizes that finding:
enumerate every run-level setting and confirm each that is consulted *after* a CLI
process boundary is persisted on a durable document, not merely held in engine memory.

## The rule

`orchestrator/cli.py::_engine(args)` builds the Engine for **every** subcommand as:

```python
return Engine(store, ledger, project, router=router, registry=registry)
```

It passes **none** of the tuning knobs — so every subcommand's Engine carries the
constructor *defaults*. A run is driven by a sequence of separate CLI invocations
(`init-run`, repeated `next-work`/`record`, `deliver`, …), each a fresh process with a
fresh Engine. Therefore:

> A run-level setting chosen at run-create (or add-task) time and consulted at any
> **later** stage boundary — dispatch, retry, review-gate, filing, completion — MUST be
> persisted on the `Run` (or `Task`) document and re-read there. Relying on the Engine
> constructor value only works within the single process that set it.

`max_filed_followups` (#196) is the worked example: persisted on `Run` and re-read via
`load_run` at filing time (`engine._file_review_followups`).

## Audit table

| Engine setting | Consumed at | Crosses a CLI process boundary? | Persisted? | Verdict |
|---|---|---|---|---|
| `max_attempts` | retry decision, `record()` | yes | **Task.max_attempts** (stamped at `add_task`; read as `task.max_attempts`) | OK — persisted |
| `max_filed_followups` | filing, task finalize | yes | **Run.max_filed_followups** + **Task.max_filed_followups** (read via `load_run`) | OK — persisted (#196) |
| `max_review_cycles` | review gate, `record()` | yes | no (engine default only) | Safe *only* because no run-level override exists — must persist on Run before one is added |
| `max_rate_limit_waits` | rate-limit cooldown, `record()` | yes | no | same — persist-on-Run-first when exposed |
| `rate_limit_cooldown_s` | rate-limit cooldown, `record()` | yes | no | same |
| `max_infra_resets` | infra reset loop, `record()` | yes | no | same |
| `max_salvage_keeps` | salvage loop, `record()` | yes | no | same |
| `breaker_threshold` | circuit breaker, `record()` | yes | no | same |
| `budget_soft_fraction` | soft budget warning, `record()` | yes | no (`Run.budget_usd`/`budget_warning_sent` are on Run; the *fraction* is engine-only) | same |
| `concurrency_ceiling` | scheduler fan-out, within one supervising process | no | n/a | Genuinely process-local — leases are re-derived from task state each process |
| `progress_throttle_s` (+ `_last_progress_at`) | progress-comment throttle | no (per-process best-effort by design) | n/a — deliberately in-memory | Process-local; a resumed run re-publishes harmlessly (upsert) |
| `use_learnings_kb` | intake context fold | re-evaluated each process | env hatch `ORCHESTRATOR_NO_LEARNINGS_KB` | Process-local toggle; no run-scoped value to preserve |

## Findings

1. **No live bug.** Every run-level setting that is *currently configurable* per run
   (`Engine.create_run(...)` — `budget_usd`, `route_by_cost`, `route_by_capacity`,
   `cross_provider_fallback`, `warm_retry`, `progress_comments`, `max_filed_followups`)
   is a field on `Run`, and the two boundary-crossing tuning knobs that have a durable
   home (`max_attempts`, `max_filed_followups`) read it back correctly.

2. **Latent trap.** The retry/gate budgets (`max_review_cycles`, `max_rate_limit_waits`,
   `rate_limit_cooldown_s`, `max_infra_resets`, `max_salvage_keeps`, `breaker_threshold`,
   `budget_soft_fraction`) are all consumed at `record()`-time, i.e. across the boundary.
   They are safe *today* purely because they are default-only — no `create_run` argument
   or CLI flag can vary them. The `#206` bug reappears the instant one is exposed at run
   scope without first landing on `Run`. This is the pattern future contributors must know.

## Enforcement

- **Guard test:** `tests/test_run_settings_persistence.py` asserts every
  `Engine.create_run` parameter is a `Run` model field, so a new run-level knob cannot be
  added to `create_run` without a persisted home.
- **Write-site comment:** the `Engine.__init__` docstring block classifies each setting
  (persisted / boundary-crossing-but-default-only / process-local).
- **CLAUDE.md working norm:** points here so the pattern is learned before the bug is
  written.
