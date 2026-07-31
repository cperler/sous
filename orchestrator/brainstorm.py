"""Brainstorm front door (#2): divergent exploration → ranked shortlist → filed ideas.

The upstream of the upstream. The ``spec_intake`` front door (#18) turns a *known idea*
into dependency-ordered issues; ``batch_plan`` (#57) turns *already-filed issues* into a
scheduled batch. But nothing sits ABOVE those to turn a *fuzzy area/goal* into the
candidate ideas in the first place — that divergent-then-convergent exploration is what
this module backstops.

Lineage: the reference bash system had an ``innovation_brainstorm`` field bolted onto
task *completion* — a free-text reflection ("what's the single smartest addition you could
make to the roadmap?") that surfaced as a PR comment and then vaporized. We keep the
capability (surface innovation ideas → route them to the roadmap) but rebuild the SHAPE as
a first-class front door: a structured, replayable session that DIVERGES (generates N
candidate ideas, each with problem/proposal/impact/effort/risk/evidence) and CONVERGES
(deterministic ranking → a shortlist the human picks from), then hands off — small ideas
file as standalone enhancement issues, larger ones feed ``spec-intake``. (The per-run
reflection the old field served already has a home here: the retrospective + the
self-improvement loop's ``file_followup``.)

The conversation — exploring the codebase/backlog/run-history, writing the ideas — is
model work and lives in the ``brainstorm`` skill. This module is the deterministic half:
it loads and schema-validates the session file, ranks the ideas with NAMED, transparent
weights (impact desc, then effort asc, then risk asc — ties stable by authored order),
renders the shortlist, and files the human's selections as issues via a project's task
source (label ``brainstorm``, provenance line in the body).

Pure and project-agnostic: it calls no model and knows nothing about a specific repo.
Issue creation reuses the same duck-typed ``create_task`` hook that ``spec_intake`` files
through (``orchestrator/ports/project.py``) — one filing seam across all the producers, not two.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .errors import OrchestratorError

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "brainstorm.json"

# The label every filed idea carries, so a brainstorm's output is queryable as a group.
BRAINSTORM_LABEL = "brainstorm"

# --- Ranking: named, transparent weights ------------------------------------------
# The shortlist order is deterministic and explainable: impact descending, then effort
# ascending (cheaper first), then risk ascending (safer first). We encode that lexicographic
# order as a single composite score so it's displayable AND sortable in one pass — the band
# widths (100 >> 10 >> 1) guarantee impact dominates effort dominates risk with no overlap,
# so sorting by score descending reproduces the tie-break chain exactly. Equal scores keep
# authored order (Python's sort is stable).
IMPACT_SCORE = {"high": 3, "medium": 2, "low": 1}
EFFORT_SCORE = {"small": 1, "medium": 2, "large": 3}
RISK_SCORE = {"low": 1, "medium": 2, "high": 3}
IMPACT_WEIGHT = 100  # impact is the primary key (descending)
EFFORT_WEIGHT = 10   # effort breaks impact ties (ascending — subtracted)
RISK_WEIGHT = 1      # risk breaks the remaining ties (ascending — subtracted)


class BrainstormError(OrchestratorError):
    """A brainstorm session file failed schema validation (or a bad selection index)."""


@functools.cache
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _format_schema_errors(doc: Any) -> list[str]:
    validator = Draft202012Validator(_schema())
    msgs = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        msgs.append(f"{loc}: {err.message}")
    return msgs


def validate_brainstorm(doc: Any) -> None:
    """Raise ``BrainstormError`` (listing every violation) unless ``doc`` matches the
    brainstorm schema. (There's no DAG here — ideas are independent candidates.)"""
    errors = _format_schema_errors(doc)
    if errors:
        raise BrainstormError(
            "brainstorm failed schema validation:\n  - " + "\n  - ".join(errors)
        )


def load_brainstorm(path: str | Path) -> dict:
    """Read, parse, and validate a brainstorm session file. Raises ``BrainstormError``
    with a clear message on a missing file, malformed JSON, or a schema violation."""
    p = Path(path)
    if not p.exists():
        raise BrainstormError(f"brainstorm file not found: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BrainstormError(f"brainstorm file is not valid JSON ({p}): {exc}") from exc
    validate_brainstorm(doc)
    return doc


def score_idea(idea: dict) -> int:
    """The idea's composite rank score (higher = shortlist higher). Encodes impact desc,
    then effort asc, then risk asc via the named band weights above — a single sortable
    number that reproduces that exact tie-break chain."""
    return (
        IMPACT_SCORE[idea["impact"]] * IMPACT_WEIGHT
        - EFFORT_SCORE[idea["effort"]] * EFFORT_WEIGHT
        - RISK_SCORE[idea["risk"]] * RISK_WEIGHT
    )


def rank_ideas(doc: dict) -> list[dict]:
    """The session's ideas in shortlist order (best first), stable by authored order on a
    tie. Pure — no side effects; this is what ``render_shortlist`` and filing rank against."""
    # enumerate keeps the authored index as a stable secondary key AND lets us expose it.
    return sorted(doc["ideas"], key=lambda i: -score_idea(i))


def render_shortlist(doc: dict) -> str:
    """A human-readable ranked shortlist: the area and each idea in rank order with its
    score and impact/effort/risk, plus a one-line summary. No side effects — this is the
    durable, replayable record of the session, and what ``brainstorm capture`` prints."""
    ranked = rank_ideas(doc)
    lines = [
        f"Brainstorm area: {doc['area']}",
        f"Ideas: {len(ranked)} (ranked shortlist — impact desc, effort asc, risk asc)",
        "",
    ]
    for pos, idea in enumerate(ranked, 1):
        lines.append(
            f"{pos}. [{idea['impact']}/{idea['effort']}/{idea['risk']} "
            f"score={score_idea(idea)}] {idea['title']}"
        )
        lines.append(f"     problem:  {idea['problem']}")
        lines.append(f"     proposal: {idea['proposal']}")
        evidence = idea.get("evidence") or []
        if evidence:
            lines.append(f"     evidence: {', '.join(evidence)}")
    lines.append("")
    lines.append("(legend: impact=high>medium>low, effort=small<medium<large, "
                 "risk=low<medium<high)")
    lines.append("Pick with: brainstorm capture <file> --file-selected <rank,rank,...> "
                 "(ranks are the positions above)")
    return "\n".join(lines)


def _compose_body(idea: dict, area: str) -> str:
    """The issue body for a filed idea: its problem, proposal, evidence, and a provenance
    line naming the brainstorm session it came from — so the idea's rationale travels with
    the issue instead of vaporizing in the conversation."""
    parts = [
        "## Problem",
        idea["problem"],
        "",
        "## Proposal",
        idea["proposal"],
    ]
    evidence = idea.get("evidence") or []
    if evidence:
        parts += ["", "## Evidence", *(f"- {e}" for e in evidence)]
    parts += [
        "",
        "---",
        f"Provenance: filed by `orchestrator brainstorm` from area \"{area}\" "
        f"(impact={idea['impact']}, effort={idea['effort']}, risk={idea['risk']}).",
    ]
    return "\n".join(parts)


def _resolve_selection(ranked: list[dict], selected: list[int]) -> list[dict]:
    """Translate 1-based shortlist ranks (what the human sees / picks) into the ideas they
    name, in the order given. Raises ``BrainstormError`` on an out-of-range or duplicate
    rank so a typo can't silently file the wrong idea."""
    if not selected:
        raise BrainstormError("no ideas selected (pass --file-selected <rank,rank,...>)")
    seen: set[int] = set()
    picks: list[dict] = []
    for rank in selected:
        if rank in seen:
            raise BrainstormError(f"rank {rank} selected more than once")
        seen.add(rank)
        if rank < 1 or rank > len(ranked):
            raise BrainstormError(
                f"rank {rank} out of range (the shortlist has {len(ranked)} idea(s), "
                "ranks 1..N)"
            )
        picks.append(ranked[rank - 1])
    return picks


def file_selected(
    doc: dict,
    task_source: Any,
    selected: list[int],
    *,
    dry_run: bool = False,
) -> dict:
    """File the human's picked ideas as issues via ``task_source``, in the order selected.

    ``selected`` is 1-based shortlist ranks (the positions ``render_shortlist`` prints).
    Each picked idea files with its title, a body composed of problem/proposal/evidence
    plus a provenance line, and the ``brainstorm`` label. Returns
    ``{area, label, selected, filed[...], dry_run}``.

    ``dry_run`` files nothing — it maps each pick to a ``(dry-run)`` placeholder ref so the
    caller sees exactly what would be created. A real run needs the source to expose
    ``create_task`` (the same duck-typed hook ``spec_intake`` files through)."""
    validate_brainstorm(doc)
    ranked = rank_ideas(doc)
    picks = _resolve_selection(ranked, selected)
    area = doc["area"]

    create: Any = getattr(task_source, "create_task", None)
    if not dry_run and not callable(create):
        raise BrainstormError(
            "task source cannot file issues: it exposes no create_task(title, body, "
            "labels) hook (use --dry-run to preview, or plug in a source that supports it)"
        )

    filed: list[dict] = []
    for rank, idea in zip(selected, picks, strict=True):
        body = _compose_body(idea, area)
        labels = [BRAINSTORM_LABEL]
        ref = "(dry-run)" if dry_run else create(idea["title"], body, labels)
        filed.append(
            {
                "rank": rank,
                "ref": ref,
                "title": idea["title"],
                "labels": labels,
                "impact": idea["impact"],
                "effort": idea["effort"],
                "risk": idea["risk"],
            }
        )
    return {
        "area": area,
        "label": BRAINSTORM_LABEL,
        "selected": list(selected),
        "filed": filed,
        "dry_run": dry_run,
    }
