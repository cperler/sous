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
from pathlib import Path

from .status_store import file_lock


def _text_fingerprint(text: object) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def cluster_key(entry: dict) -> str:
    """Stable grouping key: an explicit target wins, otherwise exact normalized text."""
    target = entry.get("target")
    if isinstance(target, dict):
        kind = str(target.get("kind") or "").strip().casefold()
        ref = re.sub(r"\s+", " ", str(target.get("ref") or "")).strip().casefold()
        if kind and ref:
            return f"{kind}:{ref}"
    return f"text:{_text_fingerprint(entry.get('text'))}"


def recurring_proposals(entries: list[dict], *, min_runs: int = 2) -> list[dict]:
    """Return process clusters represented by at least ``min_runs`` distinct runs."""
    if min_runs < 1:
        raise ValueError("min_runs must be >= 1")
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("kind") != "process" or not entry.get("text") or not entry.get("run_id"):
            continue
        grouped.setdefault(cluster_key(entry), []).append(entry)

    proposals: list[dict] = []
    for key in sorted(grouped):
        group = grouped[key]
        if len({str(entry["run_id"]) for entry in group}) < min_runs:
            continue
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
        evidence.sort(key=lambda row: (
            str(row.get("run_id") or ""), str(row.get("task_id") or ""),
            str(row.get("ts") or ""), str(row.get("text") or ""),
        ))
        proposals.append({"key": key, "target": target, "evidence": evidence})
    return proposals


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


def filed_keys(path: str | Path) -> set[str]:
    return {str(row["key"]) for row in read_filing_ledger(path)}


def append_filing(path: str | Path, row: dict) -> bool:
    """Append one filing once, under the repository's shared JSONL lock contract."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        if str(row.get("key")) in filed_keys(path):
            return False
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    return True


def proposal_title(proposal: dict) -> str:
    target = proposal.get("target")
    if isinstance(target, dict):
        return f"Meta-authoring: revise {target.get('kind')} {target.get('ref')}"
    evidence = proposal.get("evidence") or []
    lesson = str(evidence[0].get("text") or "recurring orchestration complaint") if evidence else "recurring orchestration complaint"
    return f"Meta-authoring: {lesson}"[:200]


def proposal_body(proposal: dict) -> str:
    """Render evidence plus the fixed instruction that turns the issue into a diff task."""
    target = proposal.get("target")
    lines = [
        "A process retrospective repeated across independent orchestration runs.",
        "",
        f"Cluster: `{proposal.get('key')}`",
    ]
    if isinstance(target, dict):
        lines.append(f"Target: `{target.get('kind')}:{target.get('ref')}`")
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
