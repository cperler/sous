"""Spec-conformance gate (#18 bullet 2): does the assembled BATCH deliver the SPEC?

Per-task review checks each task against *its own* issue. Nothing, until this module,
ever checks the assembled whole against the spec the tasks were sliced from — slice-level
green is not whole-spec green. Acceptance criteria live in the spec/summary and get sliced
into per-task bodies; a batch can pass every per-task review and still miss what the spec
promised.

This is the DETERMINISTIC HALF of the gate — it assembles the conformance CHECKLIST, not
the judgment. For each spec task it gathers: the filed issue ref (from the archived spec's
``filed`` provenance, written by ``spec file --archive-dir``), that issue's state
(open/closed) and any discoverable PR url (best-effort, via the task source's duck-typed
``describe_issue`` hook), and the acceptance-criteria text parsed out of the task body
(the ``## Acceptance criteria`` convention the spec-intake skill authors; full body when
absent). ``complete`` is the mechanical fact that every filed issue is closed. The
JUDGMENT half — walking each criterion against the actual merged code and filing
``spec-gap`` follow-ups for anything not genuinely met — is a skill step (spec-intake's
"Acceptance pass"), never engine code. The engine never calls a model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .spec_intake import load_spec, spec_label, spec_slug

_ACCEPTANCE_HEADING = re.compile(r"\s*#{1,6}\s+acceptance criteria\s*:?\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"\s*#{1,6}\s+\S")
_BULLET = re.compile(r"(?:[-*+]|\d+[.)])\s+(.*)")


def extract_criteria(body: str) -> list[str]:
    """Pull the acceptance criteria out of an issue/task body.

    Convention (authored by the spec-intake skill): a ``## Acceptance criteria`` markdown
    heading (any level, case-insensitive) whose section runs until the next heading or end
    of body. Bullet / numbered list items become one criterion each; non-empty prose lines
    are kept verbatim. When the heading is absent, falls back to the whole body as a single
    criterion — a criterion still to be checked, just not itemized."""
    lines = body.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _ACCEPTANCE_HEADING.match(line):
            start = i + 1
            break
    if start is None:
        stripped = body.strip()
        return [stripped] if stripped else []

    items: list[str] = []
    for line in lines[start:]:
        if _ANY_HEADING.match(line):
            break
        s = line.strip()
        if not s:
            continue
        m = _BULLET.match(s)
        items.append(m.group(1).strip() if m else s)
    return items


def _describe(task_source: Any, ref: str) -> dict:
    """Best-effort issue lookup via the source's duck-typed ``describe_issue`` hook.

    Returns ``{state, pr}`` with ``state`` one of open/closed/unknown. A missing hook, a
    raising hook, or a missing ref all degrade to ``unknown`` (the gate then treats the
    task as unverified) — the checklist is advisory plumbing, never a hard dependency on
    the network being reachable."""
    describe = getattr(task_source, "describe_issue", None) if task_source else None
    if not callable(describe):
        return {"state": "unknown", "pr": None}
    try:
        info = describe(ref) or {}
    except Exception:  # noqa: BLE001 - best-effort lookup; a flaky source degrades to unknown
        return {"state": "unknown", "pr": None}
    state = str(info.get("state") or "unknown").lower()
    if state not in ("open", "closed"):
        state = "unknown"
    return {"state": state, "pr": info.get("pr")}


def conformance_report(spec_path: str | Path, task_source: Any = None) -> dict:
    """Assemble the conformance checklist for a filed spec (the deterministic gate).

    Loads and validates the spec (ideally an archived one, carrying the ``filed``
    provenance ``spec file --archive-dir`` writes). For each task it resolves the filed
    issue ref from that provenance, looks up the issue state + PR (best-effort, via
    ``task_source.describe_issue``), and extracts the acceptance criteria from the body.

    Returns::

        {"spec": {title, slug, summary, label},
         "tasks": [{id, title, issue, state, pr, criteria: [...]}],
         "complete": bool,        # every task has a known CLOSED issue
         "unverified": [id, ...]} # tasks with no known ref or an unknown state

    ``complete`` is the mechanical half of the gate: True only when every task's issue is
    closed. It says nothing about whether the criteria were *met* — that judgment is the
    skill's Acceptance pass."""
    spec = load_spec(spec_path)
    filed = spec.get("filed") if isinstance(spec.get("filed"), dict) else None
    mapping: dict[str, str] = dict(filed.get("mapping", {})) if filed else {}
    label = (filed or {}).get("spec_label") or spec_label(spec)

    tasks_out: list[dict] = []
    unverified: list[str] = []
    for t in spec["tasks"]:
        tid = t["id"]
        ref = mapping.get(tid)
        criteria = extract_criteria(t.get("body", ""))
        if ref:
            info = _describe(task_source, ref)
            state, pr = info["state"], info["pr"]
        else:
            state, pr = "unknown", None
        tasks_out.append(
            {"id": tid, "title": t["title"], "issue": ref,
             "state": state, "pr": pr, "criteria": criteria}
        )
        if ref is None or state == "unknown":
            unverified.append(tid)

    complete = bool(tasks_out) and all(t["state"] == "closed" for t in tasks_out)
    return {
        "spec": {
            "title": spec["title"],
            "slug": spec_slug(spec),
            "summary": spec["summary"],
            "label": label,
        },
        "tasks": tasks_out,
        "complete": complete,
        "unverified": unverified,
    }


_STATE_MARK = {"closed": "[x]", "open": "[ ]", "unknown": "[?]"}


def render_conformance(checklist: dict) -> str:
    """Human-readable Markdown for a ``conformance_report`` checklist — what the CLI prints
    without ``--json`` and what the skill's Acceptance pass reads criterion by criterion."""
    spec = checklist["spec"]
    tasks = checklist["tasks"]
    closed = sum(1 for t in tasks if t["state"] == "closed")
    verdict = "COMPLETE" if checklist["complete"] else "INCOMPLETE"

    lines = [
        f"# Spec conformance — {spec['title']}",
        f"Batch label: {spec['label']}",
        f"Status: {verdict} ({closed}/{len(tasks)} issue(s) closed)",
        "",
        "> Deterministic checklist only — closed issues do NOT prove the criteria were met.",
        "> Walk each criterion against the merged changes (the skill's Acceptance pass).",
        "",
    ]
    for t in tasks:
        mark = _STATE_MARK.get(t["state"], "[?]")
        ref = t["issue"] or "(not filed / no provenance)"
        lines.append(f"## {mark} {t['id']} — {t['title']}")
        lines.append(f"Issue: {ref} · state: {t['state']}")
        lines.append(f"PR: {t['pr'] or '—'}")
        lines.append("Acceptance criteria:")
        if t["criteria"]:
            lines.extend(f"  - {c}" for c in t["criteria"])
        else:
            lines.append("  - (none stated in body)")
        lines.append("")
    if checklist["unverified"]:
        lines.append(f"Unverified (no filed ref or unknown state): "
                     f"{', '.join(checklist['unverified'])}")
    return "\n".join(lines).rstrip() + "\n"
