"""Engine-AUTHORED input to the meta-authoring seam (#400).

The seam (#71, :mod:`orchestrator.meta_authoring`) files recurring process complaints as
tracker issues against this repo, and it works — but its only input is what a stage model
volunteers in its REVIEW retrospective. Anything the driver or the scheduler does BETWEEN
stages therefore has no author, so a whole class of harness defect could only reach the
tracker when a human happened to read ``events.jsonl`` afterwards. #399 is the shape: a
``run-headless`` driver ended a live run on a cooldown-boundary race, no stage was running
when it happened, nothing was written to the learnings KB, and the run sat idle for nine
hours.

This module is the second input. It reads what the run ALREADY recorded — the
warning-grade rows the engine emits about itself in ``events.jsonl`` and ``driver.jsonl``
— and turns an explicit ALLOWLIST of them into observations shaped exactly like the KB
clusters the seam already files. Three properties are deliberate:

* **Allowlist plus predicate, never "every warning".** A rate-limit cooldown is normal
  operation; a ``driver_exit`` on unfinished work is not. A DELIVER stage rerouted onto
  the deterministic lane is the documented #364 veto; any OTHER stage rerouted is a new
  veto path nobody designed. Each spec carries the predicate that separates the two.
* **The recurrence threshold is a property of the SIGNAL, not a module constant.** A model
  gripe wants ``min_runs=2`` because one complaint is noise. A deterministic engine bug is
  believable on its first sighting, so most specs here declare ``min_runs=1`` — while a
  signal that really is only meaningful when it repeats (a stray attribution trailer) says
  so itself.
* **Pure.** No clock, no I/O, no event sink — the engine caller gathers the facts, does
  the writing, and emits the receipts. Following the fold convention, detection RETURNS
  what it declined (:class:`ScanNotices`) instead of silently dropping it.

The proposals built here carry their own title/body/labels and are filed through the SAME
``proposal_filing_guard``/``append_filing`` ledger path as the model-authored ones, so
idempotency, the per-cluster lock, the evidence watermark, comment-on-recurrence and
refile-after-close are one implementation rather than two.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .driver_log import REC_EXIT
from .meta_authoring import evidence_cursor
from .status_store import file_lock

# The cluster-key namespace for an engine-authored proposal. Distinct from the
# ``text:``/``<target-kind>:`` keys ``meta_authoring.cluster_key`` mints, so the two inputs
# can share one filing ledger without a key from either ever colliding with the other's.
SIGNAL_KEY_PREFIX = "signal:"

# Scheduler exit reasons this module reads out of ``driver.jsonl``. Declared here rather
# than imported: ``orchestrator.scheduler`` imports ``Engine``, which imports this module,
# so importing back the other way would close a cycle. ``tests/test_engine_signals.py``
# pins these to the scheduler's own constants, so the duplication cannot drift.
EXIT_NOTHING_DISPATCHABLE = "nothing_dispatchable"
EXIT_BLOCKED_ORPHANED = "blocked_on_orphaned_dispatches"

# One observation's ``text`` ceiling — mirrors the learnings KB's per-entry bound so an
# engine-authored evidence row is the same size as a model-authored one.
MAX_TEXT = 500


def _bound(text: str) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= MAX_TEXT else flat[: MAX_TEXT - 1].rstrip() + "…"


@dataclass(frozen=True)
class DispatchOrphan:
    """One dispatch lease no terminal event ever closed, with the time it was OPENED.

    The ``ts`` is why this is a record rather than a bare id. The dispatch/record
    imbalance is a property of the whole log, so no single event dates it — and a sighting
    dated by the SCANNER's clock would get a fresh identity on every re-scan, which
    silently breaks the store's replay dedupe (the same run's unchanged orphan would file
    twice and comment "recurred" on itself). The opening dispatch's own timestamp is
    data-derived and therefore stable, and it is also the date a human diagnosing the
    orphan actually wants.
    """

    work_item_id: str
    ts: str = ""


@dataclass(frozen=True)
class RunFacts:
    """Run state a predicate needs beyond the raw log records.

    Gathered by the engine caller (which owns the store) and passed in, so detection
    itself stays pure and a test can pose any run shape without a status store.

    ``unfinished_tasks`` is the #399 predicate's whole substance: tasks that are
    non-terminal, NOT parked at a human gate, still hold retry budget, and hold no
    dispatch lease — i.e. work the driver should have been able to dispatch and wasn't.
    ``run_terminal`` covers the ordinary case where the driver stops because the run
    genuinely finished, and ``run_state`` names a deliberate human pause/park.
    """

    run_id: str
    run_state: str = ""
    run_terminal: bool = False
    unfinished_tasks: tuple[str, ...] = ()
    dispatch_orphans: tuple[DispatchOrphan, ...] = ()


# Run states in which "the driver stopped with work outstanding" is a DECISION, not a
# defect: a human paused the batch or parked it for a fresh supervisor.
_DELIBERATE_STOP_RUN_STATES = frozenset({"paused", "parked"})


@dataclass(frozen=True)
class Observation:
    """One engine-authored sighting of one signal, in one run."""

    signal: str
    run_id: str
    ts: str
    text: str
    task_id: str | None = None

    def fingerprint(self) -> str:
        """Content identity within (signal, run). The idempotency key for the store: a
        re-scan of the same run re-derives byte-identical observations, so replay adds
        nothing."""
        normalized = re.sub(r"\s+", " ", self.text).strip().casefold()
        return hashlib.sha256(
            f"{self.signal}\x00{self.ts}\x00{self.task_id or ''}\x00{normalized}".encode()
        ).hexdigest()[:20]

    def as_row(self) -> dict:
        """The stored/evidence row shape — the same keys a KB-derived evidence row uses,
        so both inputs render and sort through one code path."""
        return {
            "signal": self.signal,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "ts": self.ts,
            "text": self.text,
            "fp": self.fingerprint(),
        }


# A detector reads (events, driver records, facts) and returns raw sightings as
# ``(ts, task_id, text)``. It never mints ids, reads a clock, or touches the filesystem.
Detector = Callable[[Sequence[dict], Sequence[dict], RunFacts], list[tuple[str, str | None, str]]]


@dataclass(frozen=True)
class SignalSpec:
    """One allowlisted harness defect: how to spot it, and how believable one sighting is."""

    id: str
    title: str
    rationale: str
    detect: Detector
    # Distinct runs a cluster must span before it files. 1 for a deterministic engine bug
    # (a single sighting is evidence, not noise); >1 for a signal whose one-off occurrence
    # is plausibly a fluke.
    min_runs: int = 1
    # `meta-authoring` is FUNCTIONAL (Engine parks a task sourced from an issue carrying it
    # before deliver); `bug` is the ordinary severity read. An engine-authored signal is a
    # defect report, not the `enhancement` a model's process gripe files as.
    labels: tuple[str, ...] = ("meta-authoring", "bug")

    @property
    def key(self) -> str:
        return f"{SIGNAL_KEY_PREFIX}{self.id}"


def _events_of(events: Iterable[dict], etype: str) -> list[dict]:
    return [ev for ev in events if ev.get("type") == etype]


def _ts(record: dict) -> str:
    return str(record.get("ts") or "")


def _task_of(record: dict) -> str | None:
    task_id = record.get("task_id")
    return str(task_id) if task_id else None


# --- detectors ------------------------------------------------------------------------
#
# Each is deliberately small and named for the defect rather than the event, because the
# event type alone is never the whole predicate.


def _detect_driver_exit_on_unfinished_work(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """#399: the driver declared "nothing dispatchable" while dispatchable work remained.

    Every clause matters. A driver that stops because the run FINISHED is the normal exit.
    A driver that stops on a paused/parked run is obeying a human. A task at a human gate,
    out of retry budget, or holding a lease is legitimately undispatchable — which is why
    the caller filters those out of ``unfinished_tasks`` rather than this counting
    non-terminal tasks.
    """
    if facts.run_terminal or facts.run_state in _DELIBERATE_STOP_RUN_STATES:
        return []
    if not facts.unfinished_tasks:
        return []
    listed = ", ".join(facts.unfinished_tasks[:10])
    more = len(facts.unfinished_tasks) - 10
    if more > 0:
        listed = f"{listed} (+{more} more)"
    return [
        (
            _ts(rec),
            None,
            f"driver exited `{EXIT_NOTHING_DISPATCHABLE}` while run state was "
            f"`{facts.run_state or 'unknown'}` and task(s) {listed} were still "
            f"dispatchable (non-terminal, not human-held, retry budget left, no lease "
            f"held). The run was abandoned with work it could have done.",
        )
        for rec in driver_records
        if rec.get("type") == REC_EXIT and rec.get("reason") == EXIT_NOTHING_DISPATCHABLE
    ]


def _detect_blocked_on_orphaned_dispatches(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """#313: the loop gave up holding leases it was not allowed to reclaim.

    Read from BOTH logs because they fail independently: the scheduler's
    ``scheduler_exit_blocked`` event, and the driver's own exit record for the case where
    the engine event never made it to disk. Deduped by the caller's fingerprint when both
    describe the same stop.
    """
    out: list[tuple[str, str | None, str]] = [
        (
            _ts(ev),
            None,
            f"scheduler stopped `{EXIT_BLOCKED_ORPHANED}`: task(s) "
            f"{', '.join(str(t) for t in (ev.get('in_flight') or [])) or '(unrecorded)'} "
            f"held a dispatch lease this driver could not reclaim, so the run stopped "
            f"unfinished and needs a human `abandon`.",
        )
        for ev in _events_of(events, "scheduler_exit_blocked")
    ]
    if out:
        return out
    return [
        (
            _ts(rec),
            None,
            f"driver exited `{EXIT_BLOCKED_ORPHANED}`: the run stopped with dispatch "
            f"leases it could not reclaim and no scheduler_exit_blocked event was "
            f"recorded alongside it.",
        )
        for rec in driver_records
        if rec.get("type") == REC_EXIT and rec.get("reason") == EXIT_BLOCKED_ORPHANED
    ]


def _detect_meta_proposal_failed(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """The seam failing to file is reported nowhere but the event log — including, without
    this, its own failure to file THIS report."""
    return [
        (
            _ts(ev),
            None,
            f"meta-authoring seam failed to file cluster "
            f"`{ev.get('key') or '(detection)'}`: {_bound(str(ev.get('error') or ''))}",
        )
        for ev in _events_of(events, "meta_proposal_failed")
    ]


def _detect_result_rejected(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """#311: a StageResult refused at the lease boundary. The work was done and thrown
    away, which is either a stale-dispatch bug or a runner that answered the wrong item."""
    return [
        (
            _ts(ev),
            _task_of(ev),
            f"stage result rejected at the lease boundary "
            f"(stage `{ev.get('stage')}`, attempt {ev.get('attempt')}, reason "
            f"`{ev.get('reason')}`): {_bound(str(ev.get('detail') or ''))}",
        )
        for ev in _events_of(events, "result_rejected")
    ]


def _detect_commit_attribution_trailer(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """#317/#322: a committing stage signed its commit anyway.

    ``min_runs=2`` on this one, and the exception proves the rule about thresholds being a
    per-signal property: ONE model ignoring the prompt directive is a model being a model.
    The same miss in two independent runs means the directive is not doing its job, and
    per CLAUDE.md a rule the model keeps ignoring wants structural enforcement — which is
    a harness change, i.e. exactly what this seam files.
    """
    return [
        (
            _ts(ev),
            _task_of(ev),
            f"commit {str(ev.get('sha') or '')[:12] or '(unknown)'} from stage "
            f"`{ev.get('stage')}` carries a model-attribution trailer despite the "
            f"no-attribution directive: {_bound(str(ev.get('line') or ''))}",
        )
        for ev in _events_of(events, "commit_attribution_trailer_found")
    ]


def _detect_unexpected_engine_lane_reroute(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """A lane-capability veto fired on a stage nobody designed one for.

    DELIVER-on-codex is the documented #364 veto and is normal operation, so it is
    excluded by the predicate rather than by hoping nobody looks. Any other stage losing
    its model lane means a new veto path — silent capability loss is exactly what
    ``lane_audit`` exists to catch, and it deserves an issue.
    """
    return [
        (
            _ts(ev),
            _task_of(ev),
            f"stage `{ev.get('stage')}` was rerouted off provider `{ev.get('from')}` onto "
            f"the deterministic ENGINE lane (reason: {ev.get('reason')}). Only DELIVER has "
            f"a designed lane veto (#364); this one runs no model and was not planned for.",
        )
        for ev in _events_of(events, "stage_rerouted_to_engine_lane")
        if str(ev.get("stage") or "").lower() != "deliver"
    ]


def _detect_orphaned_dispatch_leases(
    events: Sequence[dict], driver_records: Sequence[dict], facts: RunFacts
) -> list[tuple[str, str | None, str]]:
    """#142/#175: dispatches that no terminal event ever closed.

    The balance itself comes from ``Engine.events_audit`` (one implementation of the join,
    not two); the caller hands the resulting orphan list over in ``facts``.

    Dated by the EARLIEST orphaned dispatch rather than by the scan, because the whole-log
    imbalance owns no event of its own and a scan-time date would give the same unchanged
    condition a new identity on every re-scan — and this scan runs at least twice for an
    ordinary run (finalize and the driver's exit path), plus whenever a human re-runs the
    standalone check.
    """
    orphans = facts.dispatch_orphans
    if not orphans:
        return []
    dates = sorted(o.ts for o in orphans if o.ts)
    listed = ", ".join(o.work_item_id for o in orphans[:10])
    return [
        (
            dates[0] if dates else "",
            None,
            f"dispatch/record balance does not close: {len(orphans)} "
            f"dispatch lease(s) — {listed} — were opened "
            f"and never closed by a recorded/superseded/abandoned/reclaimed event.",
        )
    ]


SIGNAL_SPECS: tuple[SignalSpec, ...] = (
    SignalSpec(
        id="driver_exit_on_unfinished_work",
        title="Driver exited `nothing_dispatchable` while dispatchable work remained",
        rationale=(
            "The run-headless driver ended a run that was not finished. Nothing was "
            "in flight, so no stage model observed it and no retrospective could report "
            "it; the run simply went idle until a human noticed."
        ),
        detect=_detect_driver_exit_on_unfinished_work,
    ),
    SignalSpec(
        id="blocked_on_orphaned_dispatches",
        title="Scheduler stopped blocked on dispatch leases it could not reclaim",
        rationale=(
            "The loop exited with tasks still holding leases from a driver it may not "
            "steal from. Recovery is a manual `orchestrator abandon`, so every occurrence "
            "is unfinished work waiting on a human who has to find out first."
        ),
        detect=_detect_blocked_on_orphaned_dispatches,
    ),
    SignalSpec(
        id="meta_proposal_failed",
        title="Meta-authoring seam failed to file a proposal",
        rationale=(
            "The self-improvement exhaust silently blocked. A seam that cannot file has "
            "no other channel — including, but for this signal, for its own failure."
        ),
        detect=_detect_meta_proposal_failed,
    ),
    SignalSpec(
        id="result_rejected",
        title="Stage result rejected at the lease boundary",
        rationale=(
            "A completed stage's result was refused and its work discarded. Either a "
            "stale dispatch outlived its lease or a runner answered the wrong work item."
        ),
        detect=_detect_result_rejected,
    ),
    SignalSpec(
        id="unexpected_engine_lane_reroute",
        title="A stage lost its model lane to an undesigned capability veto",
        rationale=(
            "Only DELIVER has a designed ENGINE-lane veto (#364). Another stage running "
            "deterministically means it silently ran no model at all."
        ),
        detect=_detect_unexpected_engine_lane_reroute,
    ),
    SignalSpec(
        id="orphaned_dispatch_leases",
        title="Dispatch/record balance does not close (orphaned leases)",
        rationale=(
            "A dispatch opened a lease that no terminal event ever closed — the #142 "
            "accounting bug, which was originally caught only by hand-counting a timeline."
        ),
        detect=_detect_orphaned_dispatch_leases,
    ),
    SignalSpec(
        id="commit_attribution_trailer",
        title="Committing stages keep adding a model-attribution trailer",
        rationale=(
            "The no-attribution rule (#317) is prose in a prompt. One miss is a model "
            "being a model; the same miss across independent runs means the directive is "
            "not enforceable as written and wants a structural fix."
        ),
        detect=_detect_commit_attribution_trailer,
        min_runs=2,
    ),
)

SPECS_BY_ID: dict[str, SignalSpec] = {spec.id: spec for spec in SIGNAL_SPECS}


@dataclass(frozen=True)
class ScanNotices:
    """What detection saw but did not turn into an observation.

    The fold convention (#201/#289/#311): a pure function that drops something observable
    RETURNS the notice and lets the engine call site emit the warning-grade event, rather
    than being handed an event sink and losing its determinism.
    """

    # Allowlisted event types present in the log whose predicate declined them — the
    # "normal operation" half of the allowlist, counted so a reader can tell "we looked and
    # it was fine" from "we never looked".
    declined: dict[str, int] = field(default_factory=dict)


def detect_observations(
    events: Sequence[dict],
    driver_records: Sequence[dict],
    facts: RunFacts,
) -> tuple[list[Observation], ScanNotices]:
    """Run every allowlisted detector over one run's logs. Pure.

    There is deliberately no clock here — not even one passed in. Every observation's
    timestamp is read out of the records themselves, so a scan is a total function of its
    inputs and a replay re-derives BYTE-IDENTICAL observations, which is exactly what
    makes the store's fingerprint dedupe exact. A scan-time stamp would break that for any
    sighting whose own record lacks a date: the same unchanged condition would take a
    fresh identity every scan, and since this runs at least twice per run (finalize and
    the driver's exit path) a single orphan would file once and then immediately comment
    "recurred" on the issue it had just filed. A date that cannot be derived from the log
    is left EMPTY and rendered as "timestamp unavailable" rather than invented.
    """
    observations: list[Observation] = []
    declined: dict[str, int] = {}
    for spec in SIGNAL_SPECS:
        raw = spec.detect(events, driver_records, facts)
        seen: set[str] = set()
        for ts, task_id, text in raw:
            obs = Observation(
                signal=spec.id,
                run_id=facts.run_id,
                ts=str(ts or ""),
                text=_bound(text),
                task_id=task_id,
            )
            fp = obs.fingerprint()
            if fp in seen:
                continue  # two records describing one sighting (both logs saw it)
            seen.add(fp)
            observations.append(obs)
    # Count the allowlisted rows a predicate deliberately let through as normal operation,
    # so "the log had five of these and none qualified" is a statement the run can make.
    expected_reroutes = sum(
        1
        for ev in _events_of(events, "stage_rerouted_to_engine_lane")
        if str(ev.get("stage") or "").lower() == "deliver"
    )
    if expected_reroutes:
        declined["unexpected_engine_lane_reroute"] = expected_reroutes
    observations.sort(key=lambda o: (o.ts, o.signal, o.fingerprint()))
    return observations, ScanNotices(declined=declined)


# --- the cross-run observation store --------------------------------------------------


def read_observations(path: str | Path) -> list[dict]:
    """Read stored observation rows in order, tolerating an interrupted/corrupt line.

    Same tolerance contract as the filing ledger and the driver log: a torn append must
    not make the record unreadable to the pass that has to diagnose it.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("signal") and row.get("run_id"):
            rows.append(row)
    return rows


def append_observations(path: str | Path, observations: Sequence[Observation]) -> list[dict]:
    """Append the observations not already recorded, under the shared JSONL lock.

    Idempotent on ``(signal, run_id, fingerprint)``: re-scanning a run — which the CLI
    check and the driver/finalize triggers all make easy to do more than once — adds
    nothing and cannot inflate a cluster's run count. Returns the rows actually written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        known = {
            (str(row.get("signal")), str(row.get("run_id")), str(row.get("fp") or ""))
            for row in read_observations(path)
        }
        fresh: list[dict] = []
        for obs in observations:
            row = obs.as_row()
            ident = (row["signal"], str(row["run_id"]), row["fp"])
            if ident in known:
                continue
            known.add(ident)
            fresh.append(row)
        if fresh:
            with open(path, "a", encoding="utf-8") as fh:
                for row in fresh:
                    fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    return fresh


# --- proposals ------------------------------------------------------------------------


def _proposal(spec: SignalSpec, rows: list[dict]) -> dict:
    evidence = [
        {"run_id": row.get("run_id"), "task_id": row.get("task_id"),
         "ts": row.get("ts"), "text": row.get("text")}
        for row in rows
    ]
    evidence.sort(key=evidence_cursor)
    return {
        "key": spec.key,
        "signal": spec.id,
        "source": "engine",
        "target": None,
        "title": f"Meta-authoring: {spec.title}",
        "labels": list(spec.labels),
        "min_runs": spec.min_runs,
        "runs": len({str(row.get("run_id")) for row in rows}),
        "evidence": evidence,
    }


def _grouped(rows: Sequence[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        signal = str(row.get("signal") or "")
        if signal in SPECS_BY_ID and row.get("text") and row.get("run_id"):
            grouped.setdefault(signal, []).append(row)
    return grouped


def signal_proposals(rows: Sequence[dict]) -> list[dict]:
    """Return the engine-authored clusters that have met their OWN ``min_runs`` threshold.

    The mirror of :func:`meta_authoring.recurring_proposals`, but the floor comes from the
    signal spec instead of a module constant — the whole reason #400 argued the threshold
    belongs to the signal. Output shape is deliberately the model-authored proposal shape
    plus ``title``/``labels``, so one filing implementation serves both inputs.
    """
    return [
        _proposal(SPECS_BY_ID[signal], group)
        for signal, group in sorted(_grouped(rows).items())
        if len({str(row.get("run_id")) for row in group}) >= SPECS_BY_ID[signal].min_runs
    ]


def withheld_signals(rows: Sequence[dict]) -> list[dict]:
    """Return the clusters held BACK by their own ``min_runs`` floor, with their evidence.

    A suppression that leaves no trace is the silent failure #406 closed on the KB side;
    the engine emits a receipt from these so a run's log says what it declined to surface.
    """
    return [
        _proposal(SPECS_BY_ID[signal], group)
        for signal, group in sorted(_grouped(rows).items())
        if len({str(row.get("run_id")) for row in group}) < SPECS_BY_ID[signal].min_runs
    ]


def signal_body(proposal: dict, *, prior_ref: str | None = None) -> str:
    """Render the tracker body for an engine-authored proposal.

    Distinct from :func:`meta_authoring.proposal_body`, which opens "a process
    retrospective repeated across independent runs" and asks for a diff to a named
    authoring artifact. Neither claim is true here: nobody authored this, and the target
    is engine code.
    """
    spec = SPECS_BY_ID.get(str(proposal.get("signal") or ""))
    lines = [
        "Filed by the orchestration engine from its OWN run logs (#400) — no model "
        "authored this; it is a deterministic signal read out of `events.jsonl` / "
        "`driver.jsonl`.",
        "",
        f"Signal: `{proposal.get('signal')}` (cluster `{proposal.get('key')}`, "
        f"files at min_runs={proposal.get('min_runs')})",
    ]
    if spec is not None:
        lines.extend(["", f"Why this is a defect: {spec.rationale}"])
    if prior_ref:
        lines.append(
            f"\nRecurred after {prior_ref} was closed; that issue's fix did not hold, "
            "or covered only part of the signal."
        )
    lines.extend(["", "Evidence:"])
    for row in proposal.get("evidence") or []:
        lines.append(
            f"- run `{row.get('run_id')}`, task `{row.get('task_id')}`"
            f" ({row.get('ts') or 'timestamp unavailable'}): {row.get('text')}"
        )
    lines.extend([
        "",
        "Diagnose from the named run's `runs/<run>/events.jsonl` and `driver.jsonl` "
        "(retained until a human prunes them), then take the fix through the normal "
        "implementation and review pipeline. This is ENGINE work: it belongs in "
        "`orchestrator/`, not in any product project's tracker.",
        "",
        "This task is human-gated before delivery; applying the fix still requires "
        "explicit approval and merge.",
    ])
    return "\n".join(lines)


def signal_update_body(
    proposal: dict, rows: list[dict], *, prior_ref: str, cap: int = 25
) -> str:
    """Render the new-evidence comment for an already-filed engine-authored cluster.

    Carries only the rows recorded since the last filing, and says how many it left out
    rather than letting a capped list read as the complete set (the "never silent" rule).
    """
    shown = rows[-cap:] if cap > 0 and len(rows) > cap else list(rows)
    omitted = len(rows) - len(shown)
    lines = [
        f"Recurred since this issue was filed: {len(rows)} new sighting(s) of signal "
        f"`{proposal.get('signal')}` ({prior_ref}).",
        "",
        "New evidence:",
    ]
    if omitted:
        lines.append(f"- _(…{omitted} earlier row(s) omitted; showing the most recent {len(shown)})_")
    for row in shown:
        lines.append(
            f"- run `{row.get('run_id')}`, task `{row.get('task_id')}`"
            f" ({row.get('ts') or 'timestamp unavailable'}): {row.get('text')}"
        )
    lines.extend([
        "",
        "Filed automatically by the engine's own run-log scan (#400); the report above "
        "still applies, and this evidence may narrow or widen it.",
    ])
    return "\n".join(lines)
