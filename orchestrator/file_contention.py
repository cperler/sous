"""Declared-file contention between tasks of the same run (#377).

The scheduler fans tasks out on DAG readiness alone, so nothing consults what a task is
going to *touch*: two tasks that both rewrite a schema module, a migrations file or a
shared store API are dispatched in parallel and only meet at merge, where git either
conflicts noisily or — worse — auto-merges into a runtime break (#370 catalogues the
damage: five branches each claiming "the next" SCHEMA_VERSION, a 2-tuple→3-tuple return
change auto-merging cleanly against new 2-value call sites, a CLI subcommand registered
twice). #370's batch integration gate DETECTS that late; this prevents it.

This module is the pure half: normalizing what a SCOPE result declared, and deciding
which gate-waiters must wait for whom. It has no clock, no I/O and no event sink — the
same rules the ``state_machine`` fold layer follows — so both halves are deterministic and
replayable, and the engine call site owns every write and every emitted event.

Two rules make the wait graph safe:

* **Exclusive acquisition.** A claim is acquired once, when the gate first admits the
  task, and held until the task is terminal. A holder never waits, so nothing that
  already owns a path can be part of a cycle.
* **A strict total order over waiters.** Waiters are considered in run order and a waiter
  provisionally acquires the paths it wins, so at most one waiter per contended path is
  admitted per pass and the winner is the same on every tick. With no holder present the
  first waiter in the order is always admitted, so the pass can never defer everybody.

Contention gates on EVERY declared path — there is no file-kind heuristic. Two tasks
appending independent tests to one file are therefore serialized. That false
serialization is the deliberate safe default: it costs some parallelism, while the
failure it prevents costs the batch a remediation cycle.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from typing import NamedTuple

#: Most declared paths kept from one SCOPE result. A plan naming more files than this is
#: not describing a bounded change, and an unbounded claim list would be copied into every
#: later contention decision.
MAX_CLAIMS = 40

#: Longest single claim path kept. An over-long path is DROPPED rather than truncated: a
#: truncated path is a *different* path, and it would silently match its own prefix.
MAX_PATH_LEN = 200

#: Bound on a rejected value's repr inside a drop notice, so an adversarial model value
#: cannot bloat the audit event (mirrors the fold layer's ``_MAX_DROPPED_VALUE``).
_MAX_NOTICE_VALUE = 200


class ClaimEntry(NamedTuple):
    """One task's standing in the contention decision, in run order.

    ``holding`` is the durable fact that this task already passed the gate (its claim was
    stamped) — a holder is never re-considered as a waiter, which is what keeps the claim
    monotonic across a review fix cycle.
    """

    task_id: str
    claims: tuple[str, ...]
    holding: bool


class Deferral(NamedTuple):
    """Why one waiter is not dispatchable: who holds the contended paths, and which ones."""

    task_id: str
    blocked_by: tuple[str, ...]
    paths: tuple[str, ...]


class ContentionPlan(NamedTuple):
    """The pass's decision: waiters to hold back, and waiters that just won their claim.

    ``admitted`` is the set the caller must stamp as holding before it dispatches them —
    an admitted task that the caller then declines to dispatch must NOT be stamped, or it
    would hold a path it is not using.
    """

    deferrals: dict[str, Deferral]
    admitted: tuple[str, ...]


def _bound(value: object) -> str:
    text = repr(value)
    if len(text) > _MAX_NOTICE_VALUE:
        return text[:_MAX_NOTICE_VALUE] + " … [truncated]"
    return text


def _normalize_one(raw: object) -> tuple[str | None, str | None]:
    """One declared path → ``(claim, reject_reason)``; exactly one side is set.

    Rejects rather than repairs anything that would make the claim mean a different file:
    a non-string, an absolute path (it names a file outside the task's worktree), a path
    escaping the repo root, a bare directory marker, and an over-long path.
    """
    if not isinstance(raw, str):
        return None, "not_a_string"
    text = raw.strip().replace("\\", "/")
    if not text:
        return None, "empty"
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None, "absolute_path"
    normalized = posixpath.normpath(text)
    if normalized in (".", "..") or normalized.startswith("../"):
        return None, "escapes_repo_root"
    if len(normalized) > MAX_PATH_LEN:
        return None, "too_long"
    return normalized, None


def normalize_claims(raw: object) -> tuple[tuple[str, ...], list[dict[str, object]]]:
    """A SCOPE ``files`` declaration → ``(claims, notices)``.

    Claims are repo-relative, deduped and order-preserving. Every rejected entry produces
    a notice — the caller turns each into a warning-grade event — so a declaration the
    engine could not use is never silently read as "this task touches nothing", which is
    exactly the reading that would let it fan out against a task it collides with.

    Pure and total: a malformed declaration (not a list, wrong element types) yields no
    claims and a notice saying so, never an exception into ``record()``.
    """
    notices: list[dict[str, object]] = []
    if raw is None:
        return (), notices
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return (), [{"reason": "not_a_list", "value": _bound(raw)}]
    claims: list[str] = []
    seen: set[str] = set()
    for item in raw:
        claim, reason = _normalize_one(item)
        if claim is None:
            notices.append({"reason": reason, "value": _bound(item)})
            continue
        if claim in seen:
            continue
        seen.add(claim)
        claims.append(claim)
    if len(claims) > MAX_CLAIMS:
        notices.append({
            "reason": "claim_cap",
            "value": _bound(claims[MAX_CLAIMS]),
            "dropped": len(claims) - MAX_CLAIMS,
            "cap": MAX_CLAIMS,
        })
        claims = claims[:MAX_CLAIMS]
    return tuple(claims), notices


def plan_deferrals(entries: Sequence[ClaimEntry]) -> ContentionPlan:
    """Decide, for one dispatch pass, which waiters must wait and which win their claim.

    ``entries`` is in run order and carries every LIVE claim-bearing task: the holders
    (whatever their state — a holder parked at a human gate or in flight still owns its
    paths) and the waiters the caller would otherwise dispatch now. A terminal task must
    be left out, which is how its claim is released — including a FAILED one, so a waiter
    is never starved behind a task that will never finish.

    Deterministic and side-effect free: the same entries always produce the same winner.
    """
    held: dict[str, str] = {}
    for entry in entries:
        if not entry.holding:
            continue
        for path in entry.claims:
            held.setdefault(path, entry.task_id)

    deferrals: dict[str, Deferral] = {}
    admitted: list[str] = []
    for entry in entries:
        if entry.holding or not entry.claims:
            continue
        conflicts = tuple(p for p in entry.claims if p in held)
        if conflicts:
            blockers = tuple(sorted({held[p] for p in conflicts}))
            deferrals[entry.task_id] = Deferral(
                task_id=entry.task_id, blocked_by=blockers, paths=conflicts
            )
            continue
        # Won this pass: provisionally own its paths so a LATER waiter in the same pass
        # cannot be admitted onto the same file (the total-order tie-break).
        admitted.append(entry.task_id)
        for path in entry.claims:
            held[path] = entry.task_id
    return ContentionPlan(deferrals=deferrals, admitted=tuple(admitted))


def describe(deferral: Deferral) -> str:
    """One human-readable line for status output — why this task is sitting still."""
    return (
        f"waiting on {', '.join(deferral.blocked_by)} for "
        f"{', '.join(deferral.paths)}"
    )
