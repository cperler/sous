"""Snapshot tests for the rendered notification mails (#524).

One fixed, hand-written payload per kind (plus a degraded completion) is rendered to its
subject, plain-text body, and HTML body and compared byte-for-byte with the golden files
under ``tests/snapshots/email/``. A layout change therefore shows up as a readable diff of
the actual mail in review, rather than as a handful of substring assertions.

To accept an intended change, regenerate and review the diff:

    UPDATE_EMAIL_SNAPSHOTS=1 uv run pytest tests/test_email_snapshots.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from adapters.project.email_sink import render_body, render_html, render_subject

_DIR = Path(__file__).parent / "snapshots" / "email"
_UPDATE = os.environ.get("UPDATE_EMAIL_SNAPSHOTS") == "1"

_ISSUE = "https://github.com/acme/widgets/issues/524"
_PR = "https://github.com/acme/widgets/pull/530"

_NOTE = """## Orchestration run complete — #524

- **Task:** Rework completion emails
- **PR:** https://github.com/acme/widgets/pull/530
- **Review:** ✅ approved

| # | Stage | Status | Model | Effort | In | Out | Cost |
|---:|---|---|---|---|---:|---:|---:|
| 01 | intake | completed | — | — | — | — | $0 (engine) |
| 02 | implement | completed | `claude-opus-5-5` | high | 1.2M | 45.1k | $2.9000 |
| 03 | review | completed | `claude-fable-5-1` | high | 310.0k | 8.2k | $1.1000 |

**Total:** $4.0000 over 6 model call(s)

### Follow-ups filed (non-blocking findings)
- Add a digest-only mode → https://github.com/acme/widgets/issues/531

### Noted, not filed
- Rename `_trim_note` — noted, not tracked

_Produced by the orchestration harness — nothing dropped._
"""

_STAGES = [
    {"stage": "intake", "status": "completed", "attempt": 1, "model": None, "effort": None,
     "lane": "engine", "input_tokens": None, "output_tokens": None, "cost_usd": 0.0,
     "metered": True, "duration_s": 1.4, "error": None},
    {"stage": "scope", "status": "completed", "attempt": 1, "model": "claude-fable-5-1",
     "effort": "high", "lane": "headless", "input_tokens": 88_000, "output_tokens": 3_100,
     "cost_usd": 0.42, "metered": True, "duration_s": 95.0, "error": None},
    {"stage": "implement", "status": "failed", "attempt": 3, "model": "claude-opus-5-5",
     "effort": "high", "lane": "headless", "input_tokens": None, "output_tokens": None,
     "cost_usd": 0.0, "metered": False, "duration_s": 754.2,
     "error": "pytest: 3 failed, 212 passed"},
]

_FACTS = {
    "run_id": "batch-7",
    "task_id": "#524",
    "title": "Rework completion emails",
    "issue_number": 524,
    "issue_url": _ISSUE,
    "labels": ["enhancement", "ux"],
    "issue_excerpt": "The completion emails are useful, but the body repeats itself and says "
                     "less than it could.",
    "issue_acceptance": "- A sample email shows the issue summary and PR diffstat.\n"
                        "- Snapshot tests for each kind.",
    "attempt": 1,
    "review_cycles": 1,
    "run_dir": "/Users/me/Development/runs/widgets/2026-09-30/batch-7",
}

PAYLOADS: dict[str, tuple[str, dict]] = {
    "task_completed": ("task_completed", {
        **_FACTS,
        "kind": "task_completed",
        "summary": f"task #524 COMPLETED — Rework completion emails ({_PR})",
        "task_state": "completed",
        "pr_url": _PR,
        "pr_number": 530,
        "pr": {
            "url": _PR, "number": 530, "title": "Rework completion notification emails",
            "state": "OPEN", "draft": False, "base_ref": "main", "head_ref": "task/524",
            "additions": 412, "deletions": 96, "changed_files": 3,
            "files": [
                {"path": "adapters/project/email_sink.py", "additions": 350, "deletions": 80},
                {"path": "orchestrator/engine.py", "additions": 50, "deletions": 14},
                {"path": "orchestrator/render.py", "additions": 12, "deletions": 2},
            ],
            "files_omitted": 0,
            "commits": [
                {"sha": "0f1e2d3c4b5a", "title": "Rework completion notification emails"},
                {"sha": "a1b2c3d4e5f6", "title": "Address review: escape note HTML"},
            ],
            "commits_omitted": 0,
            "review_decision": "APPROVED",
            "checks": "success",
            "merged_at": None,
        },
        "review_approved": True,
        "review": {"approved": True, "cycles": 1, "blocking": [], "blocking_omitted": 0,
                   "non_blocking": [{"title": "Add a digest-only mode", "disposition": "file"}],
                   "non_blocking_omitted": 0},
        "stages": _STAGES[:2],
        "cost": {"usd": 4.0, "invocations": 6, "unmetered_calls": 0},
        "followups": [{"title": "Add a digest-only mode", "ref": f"{_ISSUE[:-3]}531"}],
        "followups_filed": 1,
        "improvement_ref": None,
        "optimize_suggestion_refs": [],
        "note_md": _NOTE,
    }),
    # A completion whose derived blocks all failed: no PR summary, no note, no stages or
    # cost. The mail must still say what landed and where, and nothing more.
    "task_completed_degraded": ("task_completed", {
        "run_id": "batch-7", "task_id": "#524", "kind": "task_completed",
        "summary": "task #524 COMPLETED — Rework completion emails",
        "title": "Rework completion emails", "issue_number": 524, "issue_url": None,
        "labels": [], "task_state": "completed", "attempt": 1, "review_cycles": 0,
        "pr_url": _PR, "pr_number": 530,
        "run_dir": "/Users/me/Development/runs/widgets/2026-09-30/batch-7",
        "followups": [], "followups_filed": 0, "improvement_ref": None,
        "optimize_suggestion_refs": [], "note_md": None,
    }),
    "task_failed": ("task_failed", {
        **_FACTS,
        "kind": "task_failed",
        "summary": "task #524 FAILED at implement: pytest: 3 failed, 212 passed",
        "task_state": "failed",
        "attempt": 3,
        "review_cycles": 0,
        "pr_url": None,
        "pr_number": None,
        "stage": "implement",
        "reason": "pytest: 3 failed, 212 passed",
        "review_approved": None,
        "review": {"approved": None, "cycles": 0, "blocking": [], "blocking_omitted": 0,
                   "non_blocking": [], "non_blocking_omitted": 0},
        "stages": _STAGES,
        "cost": {"usd": 0.42, "invocations": 5, "unmetered_calls": 3},
    }),
    "task_blocked": ("task_blocked", {
        **_FACTS,
        "kind": "task_blocked",
        "summary": "task #524 BLOCKED_ON_HUMAN at deliver (before:deliver) — needs a human "
                   "decision",
        "task_state": "blocked_on_human",
        "pr_url": None,
        "pr_number": None,
        "stage": "deliver",
        "hold_before": "deliver",
        "gate": "before:deliver",
        "reason": "meta-authoring task held before DELIVER for a human read",
        "review_approved": True,
        "review": {"approved": True, "cycles": 1, "blocking": [], "blocking_omitted": 0,
                   "non_blocking": [], "non_blocking_omitted": 0},
        "stages": _STAGES[:2],
        "cost": {"usd": 0.42, "invocations": 1, "unmetered_calls": 0},
        "actions": [
            {"label": "approve — release the gate and continue",
             "command": "orchestrator approve batch-7 '#524' --by <you>"},
            {"label": "reject — close the task as infeasible",
             "command": "orchestrator reject batch-7 '#524' --reason '<why>'"},
        ],
    }),
    "run_finalized": ("run_finalized", {
        "run_id": "batch-7",
        "kind": "run_finalized",
        "state": "failed",
        "summary": "run batch-7 finalized failed — 2/4 tasks completed",
        "run_dir": "/Users/me/Development/runs/widgets/2026-09-30/batch-7",
        "tasks": [
            {"task_id": "#524", "state": "completed", "title": "Rework completion emails",
             "pr_url": _PR, "issue_number": 524, "issue_url": _ISSUE,
             "cost": {"usd": 4.0, "invocations": 6, "unmetered_calls": 0}},
            {"task_id": "#525", "state": "completed", "title": "Tighten the run digest",
             "pr_url": "https://github.com/acme/widgets/pull/532", "issue_number": 525,
             "issue_url": "https://github.com/acme/widgets/issues/525",
             "cost": {"usd": 1.25, "invocations": 4, "unmetered_calls": 1}},
            {"task_id": "#526", "state": "failed", "title": "Flaky fixture", "pr_url": None,
             "issue_number": 526, "issue_url": "https://github.com/acme/widgets/issues/526",
             "cost": {"usd": 0.0, "invocations": 2, "unmetered_calls": 2}},
            {"task_id": "#527", "state": "closed_infeasible", "title": None, "pr_url": None,
             "issue_number": None, "issue_url": None, "cost": None},
        ],
        "counts": {"completed": 2, "failed": 1, "closed_infeasible": 1, "total": 4},
        "duration_s": 8130.0,
        "cost": {"usd": 5.25, "invocations": 12, "unmetered_calls": 3},
        "integration_gate": {"green": False, "failing": ["pytest"],
                             "filed": "https://github.com/acme/widgets/issues/540"},
    }),
}


def _check(name: str, actual: str) -> None:
    path = _DIR / name
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.exists(), f"missing snapshot {path}; run with UPDATE_EMAIL_SNAPSHOTS=1"
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{name} changed; if intended, regenerate with UPDATE_EMAIL_SNAPSHOTS=1 and review "
        f"the diff.\n--- actual ---\n{actual}"
    )


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_rendered_mail_matches_snapshot(name: str) -> None:
    kind, payload = PAYLOADS[name]
    _check(f"{name}.subject.txt", render_subject(kind, payload) + "\n")
    _check(f"{name}.body.txt", render_body(kind, payload) + "\n")
    _check(f"{name}.body.html", render_html(kind, payload))


def test_every_snapshot_file_has_a_payload() -> None:
    """A renamed or dropped payload must not leave a stale golden file that nothing
    checks."""
    expected = {f"{n}.{part}" for n in PAYLOADS
                for part in ("subject.txt", "body.txt", "body.html")}
    assert {p.name for p in _DIR.iterdir()} == expected
