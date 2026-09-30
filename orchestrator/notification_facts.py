"""Pure builders for the notification payload's derived blocks (#524).

``Engine._notification_facts`` assembles every per-task alert payload; the blocks it
derives from a task doc, a described PR, or a run doc are built here. Pure: no I/O, no
clock, no events. The engine owns the guarding (each block is wrapped so one failing still
yields the others) and the ``notification_facts_degraded`` event, per the fold convention
that only the engine caller emits.

Presentation is the sink's business. These functions only choose WHICH facts travel and
bound their size, because the payload is also appended verbatim to ``events.jsonl``.
"""

from __future__ import annotations

import re
from datetime import datetime

from .render import format_review_issue
from .schemas.enums import StageStatus
from .schemas.status import Run, Task

# Caps on what a payload carries. Generous for a normal task; a sprawling PR or review
# still produces a mail that fits on a screen, and the cut is always stated as a count.
ISSUE_EXCERPT_MAX_CHARS = 600
ACCEPTANCE_EXCERPT_MAX_CHARS = 500
PR_MAX_FILES = 40
PR_MAX_COMMITS = 30
REVIEW_MAX_FINDINGS = 10
_FINDING_MAX_CHARS = 300

# A task-source body may carry the tracker discussion appended after this heading
# (``adapters/project/task_discussion``); the ask itself is what comes before it.
_DISCUSSION_HEADING = re.compile(r"^##\s+Discussion\s*$", re.MULTILINE)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Headings whose section states the ask better than the body's opening lines do.
_SUMMARY_HEADINGS = ("problem", "summary", "goal", "motivation", "context", "why")
_ACCEPTANCE_HEADINGS = ("acceptance", "done when", "definition of done")


def _clip(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars at a paragraph, line, or word boundary, and SAY it
    was cut with a trailing ellipsis (never silent)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n\n", "\n", " "):
        at = cut.rfind(sep)
        if at >= limit // 2:
            cut = cut[:at]
            break
    return cut.rstrip() + " …"


def _sections(body: str) -> list[tuple[str, str]]:
    """Split markdown into ``(heading, text)`` pairs; text before any heading gets ``""``."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            out.append((match.group(2).strip(), []))
        else:
            out[-1][1].append(line)
    return [(head, "\n".join(lines).strip()) for head, lines in out]


def _section(body: str, names: tuple[str, ...]) -> str | None:
    for head, text in _sections(body):
        if text and head.casefold().startswith(names):
            return text
    return None


def issue_facts(body: str) -> dict:
    """The ask, from the task-source body: ``{excerpt, acceptance}``, each bounded or None.

    ``excerpt`` prefers a Problem/Summary-style section and falls back to the body's
    opening; ``acceptance`` is the Acceptance section when the body has one. Tracker
    discussion appended after ``## Discussion`` and HTML comments are dropped first, so the
    excerpt is the author's ask rather than the thread about it."""
    text = _HTML_COMMENT.sub("", body or "")
    if match := _DISCUSSION_HEADING.search(text):
        text = text[: match.start()]
    text = text.strip()
    if not text:
        return {"excerpt": None, "acceptance": None}
    summary = _section(text, _SUMMARY_HEADINGS)
    if summary is None:
        # No labelled section: the opening prose, minus any leading heading lines.
        lead = next((t for _, t in _sections(text) if t), "")
        summary = lead or None
    acceptance = _section(text, _ACCEPTANCE_HEADINGS)
    return {
        "excerpt": _clip(summary, ISSUE_EXCERPT_MAX_CHARS) if summary else None,
        "acceptance": _clip(acceptance, ACCEPTANCE_EXCERPT_MAX_CHARS) if acceptance else None,
    }


def _duration_s(started: str | None, completed: str | None) -> float | None:
    if not started or not completed:
        return None
    try:
        delta = datetime.fromisoformat(completed) - datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return None
    seconds = delta.total_seconds()
    return round(seconds, 1) if seconds >= 0 else None


def stage_facts(task: Task) -> list[dict]:
    """One entry per stage that RAN, in pipeline order: outcome plus model, effort, lane,
    tokens, cost, the #319 ``metered`` flag that says whether that cost is a measurement,
    and wall time. ``cost_usd``/tokens are the stage's LATEST attempt (what the task doc
    keeps); the payload's ``cost`` roll-up is the all-attempts figure from the ledger."""
    order = list(task.pipeline) or list(task.stages)
    out = []
    for stage in order:
        rec = task.stages.get(stage)
        if rec is None or rec.status is StageStatus.PENDING:
            continue
        out.append({
            "stage": stage.value,
            "status": rec.status.value,
            "attempt": rec.attempt,
            "model": rec.model,
            "effort": rec.effort.value if rec.effort is not None else None,
            "lane": rec.lane.value if rec.lane is not None else None,
            "input_tokens": rec.input_tokens,
            "output_tokens": rec.output_tokens,
            "cost_usd": rec.cost_usd,
            "metered": rec.metered,
            "duration_s": _duration_s(rec.started_at, rec.completed_at),
            "error": rec.error,
        })
    return out


def _bounded_list(items: list[str], limit: int) -> tuple[list[str], int]:
    kept = [_clip(i, _FINDING_MAX_CHARS) for i in items[:limit]]
    return kept, max(0, len(items) - limit)


def review_facts(task: Task, review_output: dict | None) -> dict:
    """The review outcome: verdict, cycles, blocking issues, and non-blocking findings with
    their disposition, each list capped with the overflow COUNTED (``*_omitted``)."""
    review = review_output or {}
    blocking = [format_review_issue(i) for i in review.get("issues") or []]
    blocking, blocking_omitted = _bounded_list([b for b in blocking if b], REVIEW_MAX_FINDINGS)
    non_blocking: list[dict] = []
    raw_nb = [f for f in review.get("non_blocking") or [] if isinstance(f, dict)]
    for finding in raw_nb[:REVIEW_MAX_FINDINGS]:
        title = str(finding.get("title") or "").strip()
        if title:
            non_blocking.append({
                "title": _clip(title, _FINDING_MAX_CHARS),
                "disposition": str(finding.get("disposition") or "").strip() or None,
            })
    return {
        "approved": review.get("approved"),
        "cycles": task.review_cycles,
        "blocking": blocking,
        "blocking_omitted": blocking_omitted,
        "non_blocking": non_blocking,
        "non_blocking_omitted": max(0, len(raw_nb) - REVIEW_MAX_FINDINGS),
    }


def pr_facts(info: dict) -> dict:
    """A described PR (the task source's ``describe_pr`` return), reduced to what an alert
    shows and bounded: at most ``PR_MAX_FILES`` files and ``PR_MAX_COMMITS`` commits, with
    the rest counted in ``files_omitted``/``commits_omitted``. Keys the source did not
    supply stay None — an unknown diffstat or CI state is never rendered as zero/passing."""
    files = [f for f in info.get("files") or [] if isinstance(f, dict)]
    commits = [c for c in info.get("commits") or [] if isinstance(c, dict)]
    return {
        "url": info.get("url"),
        "number": info.get("number"),
        "title": info.get("title"),
        "state": info.get("state") or None,
        "draft": info.get("draft"),
        "base_ref": info.get("base_ref"),
        "head_ref": info.get("head_ref"),
        "additions": info.get("additions"),
        "deletions": info.get("deletions"),
        "changed_files": info.get("changed_files"),
        "files": [
            {"path": f.get("path"), "additions": f.get("additions"),
             "deletions": f.get("deletions")}
            for f in files[:PR_MAX_FILES]
        ],
        "files_omitted": max(0, len(files) - PR_MAX_FILES),
        "commits": [
            {"sha": c.get("sha"), "title": _clip(str(c.get("title") or ""), 120)}
            for c in commits[:PR_MAX_COMMITS]
        ],
        "commits_omitted": max(0, len(commits) - PR_MAX_COMMITS),
        "review_decision": info.get("review_decision"),
        "checks": info.get("checks"),
        "merged_at": info.get("merged_at"),
        # Stamped by the engine's #378 check, not the source: why this PR does not prove
        # delivery (None when it validated or was never checked).
        "delivery_problem": _clip(str(info["delivery_problem"]), 300)
        if info.get("delivery_problem") else None,
    }


def cost_rollup(rows: list[dict]) -> dict:
    """Ledger rows → ``{usd, invocations, unmetered_calls}``. The unmetered count travels
    WITH the figure (#319) so no consumer renders a confident total for unknown usage."""
    return {
        "usd": round(sum(r.get("cost_usd") or 0.0 for r in rows), 6),
        "invocations": len(rows),
        "unmetered_calls": sum(1 for r in rows if r.get("metered") is False),
    }


def run_counts(run: Run) -> dict:
    """Task states at finalize, for the run digest's headline: every state that occurred
    keyed by its value, plus ``total``."""
    counts: dict[str, int] = {}
    for ref in run.task_refs:
        counts[ref.state.value] = counts.get(ref.state.value, 0) + 1
    counts["total"] = len(run.task_refs)
    return counts


def run_duration_s(created_at: str | None, now: str) -> float | None:
    """Wall time from run creation to ``now`` (both ISO), or None if unparsable."""
    return _duration_s(created_at, now)
