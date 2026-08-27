"""Deterministic recurrence detection for process retrospectives (#71).

The engine never asks a model to cluster or edit itself. This module groups durable
``process`` KB entries lexically, records which groups have already been filed, and
renders the evidence passed to an ordinary task. That task authors the concrete diff
through the normal implementation/review/delivery pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .status_store import file_lock


def _text_fingerprint(text: object) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def cluster_key(entry: dict) -> str:
    """Return a stable grouping key for one process entry.

    An explicit artifact target groups differently worded complaints about the same
    harness surface. Targetless entries group only when their whitespace-normalized,
    case-folded text matches.
    """
    target = entry.get("target")
    if isinstance(target, dict):
        kind = str(target.get("kind") or "").strip().casefold()
        ref = re.sub(r"\s+", " ", str(target.get("ref") or "")).strip().casefold()
        if kind and ref:
            return f"{kind}:{ref}"
    return f"text:{_text_fingerprint(entry.get('text'))}"


# How many new evidence rows one update comment renders in full. A cluster that has been
# accumulating for months can outgrow a readable comment; the renderer says so explicitly
# rather than letting a capped comment read as the complete set (the "never silent" rule).
UPDATE_EVIDENCE_ROW_CAP = 25


def evidence_cursor(row: dict) -> tuple[str, str, str, str]:
    """Return one evidence row's total-order position (#406).

    Timestamp first so the order is temporal, then run/task/text so rows sharing a
    timestamp still order deterministically. This tuple is both the evidence sort key and
    the shape of the filing watermark, which is what makes "everything after the last
    filing" an exact set rather than an approximation.
    """
    return (
        str(row.get("ts") or ""),
        str(row.get("run_id") or ""),
        str(row.get("task_id") or ""),
        _text_fingerprint(row.get("text")),
    )


def evidence_watermark(proposal: dict) -> dict:
    """Return the filing watermark for a cluster: its row count and its highest cursor.

    Recorded on the ledger row so a later run can tell "already filed, nothing new" from
    "already filed, and 12 lessons have piled up behind it since".
    """
    evidence = proposal.get("evidence") or []
    cursors = [evidence_cursor(row) for row in evidence]
    return {
        "evidence_count": len(evidence),
        "evidence_cursor": list(max(cursors)) if cursors else [],
    }


def _cursor_of_filing(filing: dict | None) -> tuple[str, ...]:
    """Read a ledger row's stored cursor, tolerating a pre-#406 row that has none.

    A watermark-less row reads as the empty cursor, which sorts below every real one, so
    the first run after the upgrade reports the whole accumulated backlog once instead of
    silently swallowing it.
    """
    if not filing:
        return ()
    raw = filing.get("evidence_cursor")
    return tuple(str(part) for part in raw) if isinstance(raw, list) and raw else ()


def new_evidence(proposal: dict, filing: dict | None) -> list[dict]:
    """Return the cluster's evidence rows recorded after ``filing``'s watermark.

    Rows at or below the watermark were already carried to the tracker. A row that
    arrives with an OLDER timestamp than the watermark (a backfilled KB) is deliberately
    not re-reported; the engine's skip receipt still carries both counts, so the
    discrepancy is visible rather than invented.
    """
    watermark = _cursor_of_filing(filing)
    if not watermark:
        return list(proposal.get("evidence") or [])
    return [
        row for row in (proposal.get("evidence") or [])
        if evidence_cursor(row) > watermark
    ]


def _grouped_process_entries(entries: list[dict]) -> dict[str, list[dict]]:
    """Group usable ``process`` rows by cluster key (shared by the two cluster views)."""
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("kind") != "process" or not entry.get("text") or not entry.get("run_id"):
            continue
        grouped.setdefault(cluster_key(entry), []).append(entry)
    return grouped


def _build_proposal(key: str, group: list[dict]) -> dict:
    """Render one cluster's stable proposal shape from its raw KB rows."""
    target = next(
        (entry.get("target") for entry in group if isinstance(entry.get("target"), dict)),
        None,
    )
    evidence = [
        {
            "run_id": entry.get("run_id"),
            "task_id": entry.get("task_id"),
            "ts": entry.get("ts"),
            "text": entry.get("text"),
        }
        for entry in group
    ]
    evidence.sort(key=evidence_cursor)
    return {"key": key, "target": target, "evidence": evidence}


def recurring_proposals(entries: list[dict], *, min_runs: int = 2) -> list[dict]:
    """Return deterministic process clusters spanning at least ``min_runs`` runs.

    Non-process or provenance-free rows are ignored, and repetitions within one run do
    not count toward the threshold. Each result carries its cluster key, optional target,
    and stably ordered evidence rows. ``min_runs < 1`` raises ``ValueError``.
    """
    if min_runs < 1:
        raise ValueError("min_runs must be >= 1")
    grouped = _grouped_process_entries(entries)
    proposals: list[dict] = []
    for key in sorted(grouped):
        group = grouped[key]
        if len({str(entry["run_id"]) for entry in group}) < min_runs:
            continue
        proposals.append(_build_proposal(key, group))
    return proposals


def withheld_clusters(entries: list[dict], *, min_runs: int = 2) -> list[dict]:
    """Return the clusters held BACK by the ``min_runs`` floor, with their evidence (#406).

    The mirror image of :func:`recurring_proposals`: a cluster seen in fewer than
    ``min_runs`` distinct runs never files, and used to leave no trace outside the KB
    JSONL. Returning it lets the engine emit a receipt so a run's own event log says how
    much evidence it declined to surface. Same purity contract — no I/O, no clock.
    """
    if min_runs < 1:
        raise ValueError("min_runs must be >= 1")
    held: list[dict] = []
    grouped = _grouped_process_entries(entries)
    for key in sorted(grouped):
        group = grouped[key]
        runs = {str(entry["run_id"]) for entry in group}
        if len(runs) >= min_runs:
            continue
        proposal = _build_proposal(key, group)
        proposal["runs"] = len(runs)
        held.append(proposal)
    return held


def read_filing_ledger(path: str | Path) -> list[dict]:
    """Read valid ledger rows in order, tolerating an interrupted/corrupt line."""
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("key"):
            rows.append(row)
    return rows


def latest_filing(path: str | Path, key: str) -> dict | None:
    """Return the highest-watermark ledger row for ``key``, or ``None`` when unfiled.

    The ledger is append-only and a cluster may be recorded more than once (an update
    comment or a re-file after the tracked issue closed), so the CURRENT state of a
    cluster is its furthest-advanced row, not merely the first one written.
    """
    rows = [row for row in read_filing_ledger(path) if str(row["key"]) == str(key)]
    if not rows:
        return None
    return max(rows, key=lambda row: (_cursor_of_filing(row), int(row.get("evidence_count") or 0)))


@contextmanager
def proposal_filing_guard(path: str | Path, key: str) -> Iterator[dict | None]:
    """Serialize the filing decision and side effect for one proposal cluster.

    The caller must keep the context open across both the external tracker call and
    :func:`append_filing`. The yielded value is the cluster's current ledger row, or
    ``None`` when nothing has been filed for it yet — enough for the caller to choose
    between filing, commenting new evidence onto the tracked issue, and re-filing a
    closed one. A per-cluster lock lets unrelated proposals file independently while
    preventing two engines from creating duplicate tracker issues for the same evidence.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:20]
    guard_path = path.with_name(f"{path.name}.{lock_key}.filing")
    with file_lock(guard_path):
        yield latest_filing(path, key)


def append_filing(path: str | Path, row: dict) -> bool:
    """Append one filing under the shared JSONL lock contract.

    Returns ``True`` when the row is written and ``False`` when it does not ADVANCE the
    cluster's recorded watermark. A repeat of the same evidence is therefore still
    refused — that is the duplicate-issue guard two concurrent finalizers race on — while
    a row carrying genuinely newer evidence supersedes the previous one. The parent
    directory and append-only ledger are created as needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        current = latest_filing(path, str(row.get("key")))
        if current is not None and _cursor_of_filing(row) <= _cursor_of_filing(current):
            return False
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    return True


def proposal_title(proposal: dict) -> str:
    """Render a tracker title naming the target, or a bounded targetless complaint."""
    target = proposal.get("target")
    if isinstance(target, dict):
        return f"Meta-authoring: revise {target.get('kind')} {target.get('ref')}"
    evidence = proposal.get("evidence") or []
    lesson = str(evidence[0].get("text") or "recurring orchestration complaint") if evidence else "recurring orchestration complaint"
    return f"Meta-authoring: {lesson}"[:200]


def proposal_body(proposal: dict, *, prior_ref: str | None = None) -> str:
    """Render provenance and the fixed instruction that turns a cluster into a diff task.

    ``prior_ref`` names the CLOSED issue this cluster was filed under before it recurred,
    so a re-filing reads as a recurrence with history rather than as a first sighting.
    """
    target = proposal.get("target")
    lines = [
        "A process retrospective repeated across independent orchestration runs.",
        "",
        f"Cluster: `{proposal.get('key')}`",
    ]
    if isinstance(target, dict):
        lines.append(f"Target: `{target.get('kind')}:{target.get('ref')}`")
    if prior_ref:
        lines.append(
            f"Recurred after {prior_ref} was closed; that issue's fix did not hold, "
            "or covered only part of the cluster."
        )
    lines.extend(["", "Evidence:"])
    for row in proposal.get("evidence") or []:
        lines.append(
            f"- run `{row.get('run_id')}`, task `{row.get('task_id')}`"
            f" ({row.get('ts') or 'timestamp unavailable'}): {row.get('text')}"
        )
    lines.extend([
        "",
        "Propose a concrete diff to the named artifact and take it through the normal "
        "implementation and review pipeline. Stage prompt templates live in "
        "`orchestrator/stages.py` (`STAGE_SPECS`); stage schemas live in "
        "`orchestrator/schemas/stages/`; personas in `.claude/agents/`; skills in "
        "`.claude/skills/*/SKILL.md`; scaffold assets in `templates/project-default/`.",
        "",
        "This task is human-gated before delivery; applying the proposal still requires "
        "explicit approval and merge.",
    ])
    return "\n".join(lines)


def proposal_update_body(
    proposal: dict, rows: list[dict], *, prior_ref: str, cap: int = UPDATE_EVIDENCE_ROW_CAP
) -> str:
    """Render the new-evidence comment posted onto an already-filed cluster's issue (#406).

    Carries ONLY the rows recorded since the last filing — the issue body already holds
    the earlier ones — and, when the run is over ``cap``, says how many rows it left out
    instead of letting a truncated list read as the complete set.
    """
    shown = rows[-cap:] if cap > 0 and len(rows) > cap else list(rows)
    omitted = len(rows) - len(shown)
    lines = [
        f"Recurred again since this issue was filed: {len(rows)} new process "
        f"retrospective(s) cluster to `{proposal.get('key')}` ({prior_ref}).",
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
        "Filed automatically by the orchestration engine's meta-authoring seam; the "
        "proposal above still applies, and this evidence may narrow or widen it.",
    ])
    return "\n".join(lines)
