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

Contention keys on the declared path AND the declared EDIT MODE (#426). "Append-only" is
a claim about the edit, not about the file — the same `tests/test_x.py` can take an
independent new test case from one task and a rewritten fixture from another — so the
classification comes from SCOPE (a per-path ``append``/``rewrite`` mode), never from a
path glob the engine owns. A glob list would be project-specific policy inside a
project-agnostic engine; if a project wants one it belongs to its adapter, which can
shape what SCOPE declares.

The rule is asymmetric, because only a REWRITE can produce the silent breakage the gate
exists to prevent:

* two ``append`` declarers on one path both run — neither can change a shared constant,
  a signature or an existing line, so the worst case is a textual conflict at merge, which
  is loud and cheap, not the #370 clean-auto-merge-into-a-runtime-break;
* any ``rewrite`` on a path serializes everything else on it, in either direction.

``rewrite`` is the default and the fallback for anything unclear, so a declaration that
predates #426, omits the mode, or gets the mode wrong behaves exactly as it did before.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
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

#: The declared edit mode for one claimed path (#426). ``REWRITE`` is the DEFAULT in every
#: direction — a bare-string declaration, an absent mode, a task doc written before #426,
#: and a mode the engine could not parse all mean ``REWRITE`` — so the gate can only ever
#: get *more* permissive by an explicit, well-formed claim.
MODE_APPEND = "append"
MODE_REWRITE = "rewrite"
MODES = (MODE_APPEND, MODE_REWRITE)


class ClaimEntry(NamedTuple):
    """One task's standing in the contention decision, in run order.

    ``holding`` is the durable fact that this task already passed the gate (its claim was
    stamped) — a holder is never re-considered as a waiter, which is what keeps the claim
    monotonic across a review fix cycle.

    ``append_paths`` (#426) is the subset of ``claims`` this task declared as append-only.
    A path absent from it is a REWRITE, which is why the field is additive and defaults to
    empty: an entry built from a pre-#426 task doc contends on every path exactly as it
    did before. It is a frozenset rather than a mapping so the default is immutable and
    cannot be shared-mutated across entries.
    """

    task_id: str
    claims: tuple[str, ...]
    holding: bool
    append_paths: frozenset[str] = frozenset()

    def mode(self, path: str) -> str:
        """This entry's declared edit mode for ``path`` — ``rewrite`` unless declared."""
        return MODE_APPEND if path in self.append_paths else MODE_REWRITE


class ClaimDeclaration(NamedTuple):
    """A normalized SCOPE ``files`` declaration.

    ``modes`` carries ONLY the non-default (append) paths, so a task doc stores nothing
    extra for the common case and an absent entry always reads as ``rewrite``. Returned as
    a named 3-tuple rather than a bare one because this module's whole reason for existing
    is #370's silent tuple-arity auto-merge — an unpack that has drifted should fail loudly
    at the call site, and a named field should be reachable without counting positions.
    """

    claims: tuple[str, ...]
    modes: dict[str, str]
    notices: list[dict[str, object]]


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
    """A rejected claim's repr, capped so an adversarial/oversized value can't bloat the
    drop notice (mirrors the fold layer's ``_bound_dropped_value``)."""
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


def _normalize_entry(raw: object) -> tuple[str | None, str, str | None]:
    """One declared entry → ``(claim, mode, notice_reason)`` (#426).

    Accepts both declaration forms: a bare string (always ``rewrite``, the pre-#426
    contract, unchanged) and ``{"path": ..., "mode": "append"|"rewrite"}``.

    A bad MODE and a bad PATH are handled differently on purpose. An unusable path is
    dropped — there is no file to serialize on. An unusable mode sits on a perfectly good
    path, so the path is KEPT at ``rewrite`` and only the mode is rejected: dropping the
    claim would un-serialize a real edit surface, which is the one direction this gate must
    never fail in. Either way a notice comes back, so neither is silent.
    """
    if isinstance(raw, Mapping):
        claim, reason = _normalize_one(raw.get("path"))
        if claim is None:
            return None, MODE_REWRITE, reason
        mode = raw.get("mode", MODE_REWRITE)
        if mode not in MODES:
            # Path kept, mode refused: the conservative direction (see docstring).
            return claim, MODE_REWRITE, "unknown_mode"
        return claim, str(mode), None
    claim, reason = _normalize_one(raw)
    return claim, MODE_REWRITE, reason


def normalize_claims(raw: object) -> ClaimDeclaration:
    """A SCOPE ``files`` declaration → ``(claims, modes, notices)``.

    Claims are repo-relative, deduped and order-preserving. Every rejected entry produces
    a notice — the caller turns each into a warning-grade event — so a declaration the
    engine could not use is never silently read as "this task touches nothing", which is
    exactly the reading that would let it fan out against a task it collides with.

    Pure and total: a malformed declaration (not a list, wrong element types) yields no
    claims and a notice saying so, never an exception into ``record()``.

    When one path is declared twice with DIFFERENT modes, the stricter one wins: a task
    that both appends to and rewrites a file is a task that rewrites it. Dedupe still keeps
    the first occurrence's position, so ordering is unchanged.
    """
    notices: list[dict[str, object]] = []
    if raw is None:
        return ClaimDeclaration((), {}, notices)
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        return ClaimDeclaration((), {}, [{"reason": "not_a_list", "value": _bound(raw)}])
    claims: list[str] = []
    modes: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw:
        claim, mode, reason = _normalize_entry(item)
        if reason is not None:
            notices.append({"reason": reason, "value": _bound(item)})
        if claim is None:
            continue
        if claim in seen:
            # Stricter mode wins on a repeat; never relax an already-recorded rewrite.
            if mode == MODE_REWRITE:
                modes[claim] = MODE_REWRITE
            continue
        seen.add(claim)
        claims.append(claim)
        modes[claim] = mode
    if len(claims) > MAX_CLAIMS:
        notices.append({
            "reason": "claim_cap",
            "value": _bound(claims[MAX_CLAIMS]),
            "dropped": len(claims) - MAX_CLAIMS,
            "cap": MAX_CLAIMS,
        })
        claims = claims[:MAX_CLAIMS]
    # Only the non-default (append) paths are carried forward, and only for kept claims:
    # an absent entry means ``rewrite`` everywhere downstream, so the stored map stays
    # empty for the overwhelmingly common declaration.
    kept = set(claims)
    appends = {p: m for p, m in modes.items() if m == MODE_APPEND and p in kept}
    return ClaimDeclaration(tuple(claims), appends, notices)


def plan_deferrals(entries: Sequence[ClaimEntry]) -> ContentionPlan:
    """Decide, for one dispatch pass, which waiters must wait and which win their claim.

    ``entries`` is in run order and carries every LIVE claim-bearing task: the holders
    (whatever their state — a holder parked at a human gate or in flight still owns its
    paths) and the waiters the caller would otherwise dispatch now. A terminal task must
    be left out, which is how its claim is released — including a FAILED one, so a waiter
    is never starved behind a task that will never finish.

    Deterministic and side-effect free: the same entries always produce the same winner.

    The mode rule (#426) is asymmetric: a REWRITE hold excludes everyone from the path,
    while APPEND holds exclude only rewriters. Both safety invariants survive it, because
    it only ever REMOVES conflicts: holders still never wait, the waiter order is still
    total with provisional acquisition, and with no holder present the first waiter still
    has nothing to conflict with — so a pass can never defer everybody.
    """
    # A path can have at most one rewrite holder (a rewrite hold excludes every later
    # admission), but any number of append holders — all of whom block a rewriting waiter,
    # so they are all named as its blockers.
    held_rewrite: dict[str, str] = {}
    held_append: dict[str, list[str]] = {}

    def _take(entry: ClaimEntry) -> None:
        for path in entry.claims:
            if entry.mode(path) == MODE_APPEND:
                held_append.setdefault(path, []).append(entry.task_id)
            else:
                held_rewrite.setdefault(path, entry.task_id)

    def _blockers_for(entry: ClaimEntry, path: str) -> list[str]:
        """Who this waiter must wait for on ``path`` — empty when it may proceed."""
        blockers: list[str] = []
        if path in held_rewrite:
            blockers.append(held_rewrite[path])
        # Append-vs-append is the whole point of the mode: two independent additions to one
        # file cannot silently break each other, so only a REWRITING waiter is held by them.
        if entry.mode(path) == MODE_REWRITE:
            blockers.extend(held_append.get(path, ()))
        return blockers

    for entry in entries:
        if entry.holding:
            _take(entry)

    deferrals: dict[str, Deferral] = {}
    admitted: list[str] = []
    for entry in entries:
        if entry.holding or not entry.claims:
            continue
        conflicts: list[str] = []
        blocked_by: set[str] = set()
        for path in entry.claims:
            blockers = _blockers_for(entry, path)
            if blockers:
                conflicts.append(path)
                blocked_by.update(blockers)
        if conflicts:
            deferrals[entry.task_id] = Deferral(
                task_id=entry.task_id,
                blocked_by=tuple(sorted(blocked_by)),
                paths=tuple(conflicts),
            )
            continue
        # Won this pass: provisionally own its paths so a LATER waiter in the same pass
        # cannot be admitted onto the same file (the total-order tie-break).
        admitted.append(entry.task_id)
        _take(entry)
    return ContentionPlan(deferrals=deferrals, admitted=tuple(admitted))


def summarize_contention(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Fold a run's event stream into the gate's deferral rate (#426).

    #377 shipped the gate deliberately over-strict and left the question "how much
    parallelism is this actually costing, and how much of it was warranted?" to be answered
    from real batch data. That answer had to be hand-grepped out of ``events.jsonl``, which
    means in practice it was answered once. This makes it a standing readout on
    ``status()``, so the strictness can be re-judged against any finished run.

    Reported per run: how many deferrals were issued, over how many distinct tasks, which
    paths caused them, and — the part that says whether the mode rule is pulling its weight
    — how many deferrals held a waiter that had declared the contended path APPEND-only.
    Those are the deferrals the #426 rule kept on purpose (an appender held by a rewriter);
    an append-vs-append pair no longer produces an event at all, so a falling
    ``append_waiter_deferrals`` alongside a falling total is the mode rule working.

    Pure: no clock, no I/O. A deferral logged before #426 carries no ``modes`` key and is
    counted as ``mode_unknown`` rather than assumed to be a rewrite — absence is not the
    same fact as an explicit value, and reading it as one would report a confident number
    about logs that cannot answer (the same discipline as the dispatch-continuity audit).
    """
    deferred_tasks: set[str] = set()
    paths: dict[str, int] = {}
    deferrals = 0
    claims = 0
    append_waiter_deferrals = 0
    mode_unknown_deferrals = 0
    for ev in events:
        etype = ev.get("type")
        if etype == "file_claim_acquired":
            claims += 1
            continue
        if etype != "dispatch_deferred_file_contention":
            continue
        deferrals += 1
        task_id = ev.get("task_id")
        if isinstance(task_id, str):
            deferred_tasks.add(task_id)
        contended = ev.get("files")
        contended = list(contended) if isinstance(contended, Sequence) and not isinstance(
            contended, str
        ) else []
        for path in contended:
            if isinstance(path, str):
                paths[path] = paths.get(path, 0) + 1
        modes = ev.get("modes")
        if not isinstance(modes, Mapping):
            mode_unknown_deferrals += 1
        elif any(modes.get(path) == MODE_APPEND for path in contended):
            append_waiter_deferrals += 1
    return {
        "deferrals": deferrals,
        "tasks_deferred": len(deferred_tasks),
        "claims": claims,
        # Sorted by count then path: a stable, most-contended-first ordering, so two reads
        # of the same log render identically and the worst offender is the first line.
        "paths": dict(sorted(paths.items(), key=lambda kv: (-kv[1], kv[0]))),
        "append_waiter_deferrals": append_waiter_deferrals,
        "mode_unknown_deferrals": mode_unknown_deferrals,
    }


def describe(deferral: Deferral) -> str:
    """One human-readable line for status output — why this task is sitting still."""
    return (
        f"waiting on {', '.join(deferral.blocked_by)} for "
        f"{', '.join(deferral.paths)}"
    )
