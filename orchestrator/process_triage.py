"""Triage queue for REVIEW process lessons, and the ledger of what triage decided.

The engine's meta-authoring seam (``meta_authoring.py``) only surfaces a process lesson
once its lexical cluster spans two runs. A targetless lesson clusters on an exact-text
hash, so a uniquely worded one can never reach that floor and would sit in the KB unseen
forever. Grouping those by MEANING is a model's job, and the engine never calls a model —
so the grouping happens in the ``triage-followups`` skill, and this module is the
deterministic half it calls:

* :func:`triage_queue` (pure) — partition the KB's ``process`` lessons into the filed
  meta-authoring clusters (with their evidence) and the lessons no filing covers, minus
  whatever triage has already dispositioned. It returns counts for every row it excludes,
  so a lesson is never dropped from the view without the output saying so.
* ``triage-dispositions.jsonl`` — an append-only ledger beside the KB recording what the
  human decided for a lesson or a filed issue. The KB itself is never rewritten.

Lesson identity is the KB row's ``id`` (minted once at append, never rewritten — the same
handle ``kb prune --id`` uses). A legacy row without one falls back to a fingerprint of its
provenance and text. A filed issue is keyed by its tracker ref, and its disposition records
the cluster's evidence count at decision time, so new evidence the engine later comments
onto the issue brings it back into the queue rather than staying hidden behind an old
"keep".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .meta_authoring import cluster_key, evidence_cursor, filing_order, read_filing_ledger
from .status_store import file_lock

DISPOSITIONS_FILE = "triage-dispositions.jsonl"

# What a disposition may say, and the field each one requires so the record is actionable
# later: a filed/merged lesson names where it went, a closed one says why, an applied one
# names the commit that carried the change. ``kept`` is for a filed issue the human chose to
# leave open as-is (it resurfaces only when new evidence arrives).
DISPOSITION_REQUIRES: dict[str, str | None] = {
    "filed": "ref",
    "merged": "ref",
    "closed": "reason",
    "applied": "commit",
    "kept": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def lesson_key(entry: dict) -> str:
    """Return the stable identity of one KB lesson.

    ``lesson:<kb id>`` when the row has an id (every row ``append_learnings`` writes does);
    otherwise a fingerprint of run/task/ts/text, which is equally stable for an unchanging
    append-only row.
    """
    entry_id = str(entry.get("id") or "").strip()
    if entry_id:
        return f"lesson:{entry_id}"
    fp = hashlib.sha256(
        json.dumps(list(evidence_cursor(entry)), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]
    return f"lesson:fp-{fp}"


def issue_key(ref: str) -> str:
    """Return the ledger identity of a filed meta-authoring issue (``issue:<ref>``)."""
    return f"issue:{str(ref).strip()}"


def ref_matches(handle: str, ref: str) -> bool:
    """True when a human-typed issue handle names the ledger's ``ref``.

    The meta-proposals ledger stores whatever the tracker hook returned (a URL for GitHub),
    while a human types ``#544`` or ``544``; both forms match on the trailing issue number.
    """
    handle, ref = str(handle).strip(), str(ref).strip()
    if not handle or not ref:
        return False
    if handle == ref:
        return True
    num = re.search(r"(\d+)/?$", ref)
    return num is not None and handle.lstrip("#") == num.group(1)


def _is_process(entry: dict) -> bool:
    return entry.get("kind") == "process" and bool(entry.get("text"))


def _lesson_view(entry: dict, *, runs_in_cluster: int, cluster_size: int) -> dict:
    """One lesson with its provenance, ready for the skill to present."""
    target = entry.get("target") if isinstance(entry.get("target"), dict) else None
    key = cluster_key(entry)
    return {
        "key": lesson_key(entry),
        "id": entry.get("id"),
        "run_id": entry.get("run_id"),
        "task_id": entry.get("task_id"),
        "ts": entry.get("ts"),
        "text": entry.get("text"),
        "target": target,
        "cluster_key": key,
        "untargeted": key.startswith("text:"),
        "cluster_runs": runs_in_cluster,
        "cluster_size": cluster_size,
    }


def latest_dispositions(rows: Iterable[dict]) -> dict[str, dict]:
    """Fold ledger rows to each key's CURRENT disposition (the last row written wins)."""
    current: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("key") or "")
        if key:
            current[key] = row
    return current


def triage_queue(
    entries: list[dict],
    filings: list[dict],
    dispositions: list[dict],
    *,
    include_handled: bool = False,
) -> dict:
    """Partition process lessons into filed clusters and uncovered lessons. Pure.

    ``entries`` are KB lessons (``read_entries``; non-process rows are ignored),
    ``filings`` are meta-proposals ledger rows, ``dispositions`` are triage ledger rows.

    * ``filed_issues`` — one per filed cluster's CURRENT tracker ref, carrying the
      cluster's KB evidence. Hidden once dispositioned, unless the cluster has gained
      evidence since that decision.
    * ``lessons`` — process lessons whose cluster has never been filed (the withheld ones,
      including every uniquely worded untargeted lesson), minus dispositioned lessons.

    With ``include_handled`` the dispositioned items stay in, each carrying its
    ``disposition``. ``excluded`` counts what the default view left out and why.
    """
    process = [e for e in entries if _is_process(e)]
    by_cluster: dict[str, list[dict]] = {}
    for entry in process:
        by_cluster.setdefault(cluster_key(entry), []).append(entry)

    rows_by_key: dict[str, list[dict]] = {}
    for row in filings:
        if row.get("key"):
            rows_by_key.setdefault(str(row["key"]), []).append(row)
    # The cluster's CURRENT ref is its furthest-advanced row (a refiling carries a new ref),
    # chosen exactly as ``latest_filing`` chooses it.
    filed_by_key = {key: max(rows, key=filing_order) for key, rows in rows_by_key.items()}
    decided = latest_dispositions(dispositions)

    excluded = {"handled_lessons": 0, "handled_issues": 0, "covered_by_filed_issue": 0}
    filed_issues: list[dict] = []
    for key in sorted(filed_by_key):
        filing = filed_by_key[key]
        ref = str(filing.get("ref") or "")
        group = sorted(by_cluster.get(key, []), key=evidence_cursor)
        excluded["covered_by_filed_issue"] += len(group)
        runs = {str(e.get("run_id")) for e in group}
        item = {
            "key": issue_key(ref),
            "ref": ref,
            "cluster_key": key,
            "target": next((e.get("target") for e in group
                            if isinstance(e.get("target"), dict)), None),
            "last_action": filing.get("action"),
            "evidence_count": len(group),
            "evidence": [_lesson_view(e, runs_in_cluster=len(runs), cluster_size=len(group))
                         for e in group],
        }
        disposition = decided.get(issue_key(ref))
        if disposition is not None:
            seen = int(disposition.get("evidence_count") or 0)
            if not include_handled and len(group) <= seen:
                excluded["handled_issues"] += 1
                continue
            item["disposition"] = disposition
            item["new_evidence_since_disposition"] = max(0, len(group) - seen)
        filed_issues.append(item)

    lessons: list[dict] = []
    for key in sorted(by_cluster):
        if key in filed_by_key:
            continue
        group = by_cluster[key]
        runs = {str(e.get("run_id")) for e in group}
        for entry in group:
            view = _lesson_view(entry, runs_in_cluster=len(runs), cluster_size=len(group))
            disposition = decided.get(view["key"])
            if disposition is not None:
                if not include_handled:
                    excluded["handled_lessons"] += 1
                    continue
                view["disposition"] = disposition
            lessons.append(view)
    lessons.sort(key=lambda v: (not v["untargeted"], evidence_cursor(v)))

    return {
        "process_lessons": len(process),
        "filed_issues": filed_issues,
        "lessons": lessons,
        "untargeted": sum(1 for v in lessons if v["untargeted"]),
        "excluded": excluded,
        "include_handled": include_handled,
    }


def dispositions_path(kb_path: str | Path) -> Path:
    """The disposition ledger lives beside the KB, like ``meta-proposals.jsonl``."""
    return Path(kb_path).with_name(DISPOSITIONS_FILE)


def read_dispositions(path: str | Path) -> list[dict]:
    """Read ledger rows in file order, skipping a torn/corrupt line (crash tolerance)."""
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


def validate_disposition(disposition: str, *, ref: str | None, reason: str | None,
                         commit: str | None) -> str | None:
    """Return an error message when a disposition lacks its required field, else ``None``."""
    if disposition not in DISPOSITION_REQUIRES:
        return f"unknown disposition {disposition!r}; expected one of {sorted(DISPOSITION_REQUIRES)}"
    required = DISPOSITION_REQUIRES[disposition]
    values = {"ref": ref, "reason": reason, "commit": commit}
    if required and not str(values[required] or "").strip():
        return f"disposition {disposition!r} requires --{required}"
    return None


def _same_decision(a: dict, b: dict) -> bool:
    fields = ("disposition", "ref", "reason", "commit", "evidence_count")
    return all(a.get(f) == b.get(f) for f in fields)


def record_dispositions(path: str | Path, rows: list[dict]) -> list[dict]:
    """Append disposition rows under the shared JSONL lock; return the rows written.

    A row identical to its key's current decision is skipped, so re-running the CLI after
    an interrupted triage pass is idempotent. All rows go out in ONE write, so a group of
    lessons filed as one issue is recorded together or (on a crash mid-write, where only a
    torn final line can result) the torn line is ignored on read and the lessons resurface.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        current = latest_dispositions(read_dispositions(path))
        written: list[dict] = []
        for raw in rows:
            row = {"ts": raw.get("ts") or _now(), **{k: v for k, v in raw.items()
                                                     if v is not None and k != "ts"}}
            prev = current.get(str(row.get("key") or ""))
            if prev is not None and _same_decision(prev, row):
                continue
            current[str(row["key"])] = row
            written.append(row)
        if written:
            # A crash can leave a torn final line with no newline; appending straight onto
            # it would fuse this decision into the corrupt line and lose it on read.
            torn = path.exists() and path.stat().st_size > 0 and \
                not path.read_bytes().endswith(b"\n")
            with open(path, "a", encoding="utf-8") as fh:
                if torn:
                    fh.write("\n")
                fh.write("".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n"
                                 for r in written))
    return written


def read_queue(kb_path: str | Path, entries: list[dict], *, include_handled: bool = False) -> dict:
    """I/O wrapper: read the meta ledger and disposition ledger beside ``kb_path``."""
    kb_path = Path(kb_path)
    filings = read_filing_ledger(kb_path.with_name("meta-proposals.jsonl"))
    disp_path = dispositions_path(kb_path)
    queue = triage_queue(entries, filings, read_dispositions(disp_path),
                         include_handled=include_handled)
    queue["dispositions_path"] = str(disp_path)
    return queue
