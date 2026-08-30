# File-contention gate: what it actually cost, and the mode rule (#426)

**Date:** 2026-08-29 · **Task:** #426 · **Source:** #377

#377 shipped the declared-file contention gate deliberately over-strict: it serialized a
task whose approved SCOPE named *any* path a live task had claimed, with no notion of what
kind of edit either task was making. That was the right default — false serialization costs
some parallelism, while the collision it prevents (#370: five branches each claiming "the
next" `SCHEMA_VERSION`; a 2-tuple→3-tuple return auto-merging cleanly against new 2-value
call sites) costs the batch a remediation cycle. #377 also left an explicit instruction:
**measure before adding a heuristic.** This is that measurement, and what was built on it.

## The measurement

Folded from `dispatch_deferred_file_contention` / `file_claim_acquired` across every run
log under `runs/` (14 run dirs at the time of writing). Only one run has exercised the gate
at all — it landed mid-batch, so everything before `batch-0829` predates it.

| run | deferrals | tasks deferred | claims |
| --- | --- | --- | --- |
| `batch-0829` | 6 | 3 | 4 |
| all others | 0 | 0 | 0 |

Contended paths, by how many deferrals each appeared in:

| path | deferrals |
| --- | --- |
| `ARCHITECTURE.md` | 6 |
| `orchestrator/engine.py` | 3 |
| `orchestrator/cli.py` | 1 |
| `orchestrator/schemas/status.py` | 1 |
| `orchestrator/stages.py` | 1 |
| `orchestrator/state_machine.py` | 1 |

The individual deferrals:

```
#480 <- #409  ARCHITECTURE.md
#389 <- #409  orchestrator/engine.py, ARCHITECTURE.md
#426 <- #409  orchestrator/engine.py, orchestrator/schemas/status.py, ARCHITECTURE.md
#426 <- #389  orchestrator/engine.py, orchestrator/state_machine.py, orchestrator/stages.py, ARCHITECTURE.md
#480 <- #389  orchestrator/cli.py, ARCHITECTURE.md
#426 <- #480  ARCHITECTURE.md
```

**`ARCHITECTURE.md` appears in all six, and is the SOLE cause of two of them.** Those two
are the shape #377 predicted: a repo-wide docs file that nearly every task adds a paragraph
to, serializing tasks that share nothing else. The other four would have serialized anyway
on a genuine code collision (`orchestrator/engine.py`, `state_machine.py`, `status.py`),
so the gate earned those.

**Caveats, stated plainly.** n = 1 batch and 6 events; this is a shape, not a rate. And
whether those two docs-only deferrals were *truly* harmless is not something the log can
say — a task that rewrites the existing contention bullet in `ARCHITECTURE.md` genuinely
collides with another that does the same. That judgment is exactly what the change below
moves to SCOPE, which is the only actor that knows what edit it is about to make.

## What was built

Two things, in the order #377 asked for.

**1. A standing readout, so this stops being a hand-grep.** `events_audit` now returns a
`contention` block (`summarize_contention`, pure, in `orchestrator/file_contention.py`):
deferral count, distinct tasks deferred, claims acquired, a most-contended-first path
histogram, and `append_waiter_deferrals`. It rides on `status()`, so any finished run can
re-answer the question. A deferral logged before this change carries no `modes` key and is
counted as `mode_unknown` rather than assumed — absence is not the same fact as a value,
the same discipline as the dispatch-continuity audit.

**2. A SCOPE-declared per-path edit mode.** `files` may now contain
`{"path": ..., "mode": "append"|"rewrite"}` alongside bare strings. Two `append` declarers
on one path both run; any `rewrite` on a path serializes everything else on it, in either
direction.

### Why the mode, and not a path glob

The obvious cheap version is a list of patterns the engine treats as non-contending
(`tests/**`, `*.md`). It was rejected twice over:

- **It is the wrong predicate.** "Append-only" describes the EDIT, not the file. The same
  `tests/test_x.py` takes an independent new case from one task and a rewritten shared
  fixture from another; the same `ARCHITECTURE.md` takes a new section from one task and a
  rewrite of an existing bullet from another. A glob cannot tell those apart, and getting
  it wrong in the permissive direction reintroduces #370.
- **It is project-specific policy in a project-agnostic engine.** Which paths are
  append-shaped is a fact about a repo's conventions. `orchestrator/` may not hold that.
  If a project wants one it belongs to its adapter, which can shape what SCOPE declares.

SCOPE is the one actor that has already read the code and decided what it is going to do,
so it is where the claim belongs.

### The safety argument

Only a rewrite can produce the failure the gate exists for. Two tasks each adding a
self-contained block to one file cannot change a shared constant, a signature, or an
existing line out from under each other; the worst case is a textual conflict at merge,
which is loud, local, and cheap — not #370's clean auto-merge into a runtime break. The
pre-merge batch integration gate catches the loud kind anyway.

Every default points at `rewrite`: a bare string, an absent `mode`, a task doc written
before this change, and a mode the engine could not parse. That last one matters most — an
unusable *path* is dropped (there is nothing to serialize on), but an unusable *mode* sits
on a perfectly good path, so the path is **kept at `rewrite`** and only the mode is
rejected, with a `scope_file_claim_dropped` notice. Dropping the claim would un-serialize a
real edit surface, which is the one direction this gate must never fail in. A path declared
twice with different modes resolves to `rewrite` for the same reason.

The two invariants that make the wait graph safe are untouched, because the mode rule only
ever *removes* conflicts: holders never wait, waiters are considered in a strict run order
with provisional acquisition, and with no holder present the first waiter still has nothing
to conflict with — so a pass can never defer everybody.

## How to re-judge this later

Run the batch, then read `events_audit.contention` off the finished run:

- `deferrals` falling while `append_waiter_deferrals` stays near zero means the mode rule is
  absorbing the false serialization and nothing else.
- `append_waiter_deferrals` climbing means appenders are being held by rewriters — real
  collisions the rule deliberately keeps, not a reason to relax further.
- `paths` still topped by a docs file means SCOPE is not declaring `append` where it should;
  that is a prompt problem, not a gate problem.
- `mode_unknown_deferrals` above zero on a fresh run means something is emitting deferrals
  without modes — a bug, not old history.
