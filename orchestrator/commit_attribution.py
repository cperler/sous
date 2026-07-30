"""#322 — deterministic detection of model/agent attribution trailers on commit messages.

#317 established the norm (a run-produced commit carries NO model attribution trailer) and
enforced it with ONE mechanism: a prompt directive (``stages._NO_ATTRIBUTION_DIRECTIVE``)
telling every committing stage not to sign its commits. An instruction is not a guarantee —
and the failure it guards against has already happened twice in this repo's own history
(``batch-headless-1`` and ``batch-headless-2`` each landed a commit signed
``Claude Opus 4.5``, a model NEITHER run dispatched). Both were noticed only because a human
happened to read ``git log``.

This module is the post-hoc half: a pure, deterministic, project- and provider-agnostic scan
of a commit message. It answers "does this message attribute authorship to a model/agent?"
and nothing else — no git I/O, no wall-clock, no model call — so the engine can call it over
whatever commits a stage produced and emit a warning-grade event when the directive was
ignored (``Engine._audit_commit_attribution``).

Deliberately report-only: the caller never rewrites a commit. DELIVER pushes its branch
before its checkpoint lands, so amending here would rewrite already-remote history.

False-positive discipline: only a line that IS a trailer (a known attribution key at the
start of the line, followed by a colon and a value) counts. Prose that merely NAMES the
trailer — the directive's own wording, a commit message explaining this policy — is not a
signature and must not be flagged, or the check cries wolf on the very change that adds it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Trailer keys that attribute authorship to a second party. Kept provider-neutral: the
# defect is "some agent signed the commit", not "Claude signed the commit", and a codex- or
# future-provider-produced trailer must trip the same wire. Compared after normalization
# (lowercased, spaces folded to hyphens), so ``Co-Authored-By``/``co-authored-by``/
# ``Co Authored By`` are one key.
#
# ``Signed-off-by`` is deliberately ABSENT: it is the DCO trailer, a legitimate human
# convention in many projects, and flagging it would make this check unusable anywhere it
# is in use.
ATTRIBUTION_TRAILER_KEYS = frozenset({
    "co-authored-by",
    "coauthored-by",
    "co-author",
    "assisted-by",
    "generated-by",
    "ai-assisted-by",
})

# A git trailer: ``Key: value`` anchored at the start of the line. The key charset is
# deliberately narrow (letters, hyphens, single spaces) so a prose line containing a colon
# ("note: see the Co-Authored-By discussion") cannot masquerade as one.
_TRAILER_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z -]{0,40}?)[ \t]*:[ \t]*(?P<value>\S.*)$")

# The other shape harness attribution takes: a generated-with footer line rather than a
# trailer (``🤖 Generated with [<tool>](<url>)``). Anchored at line start and required to
# carry a subject, so prose mentioning the phrase mid-sentence does not trip it.
_GENERATED_WITH_RE = re.compile(
    r"^[ \t]{0,3}(?:\N{ROBOT FACE}[ \t]*)?generated with[ \t]+\S", re.IGNORECASE
)

# Findings ride into an event line; a pathological commit message must not blow it up.
_LINE_CAP = 200


def _normalize_key(key: str) -> str:
    return re.sub(r"[ \t]+", "-", key.strip().lower())


def attribution_findings(message: str) -> tuple[dict[str, str], ...]:
    """Every model/agent attribution marker in ``message``, in the order it appears.

    Each finding is ``{"reason", "detail", "line"}``: ``reason`` is the marker CLASS
    (``attribution_trailer`` / ``generated_with_marker``), ``detail`` names the specific
    marker (the normalized trailer key, or ``generated-with``), and ``line`` is the
    offending line itself (capped) so a reader can see the evidence without opening the
    repo. Empty tuple == the commit is clean, which is the expected answer.
    """
    findings: list[dict[str, str]] = []
    for raw in (message or "").splitlines():
        line = raw.rstrip()
        match = _TRAILER_RE.match(line)
        if match is not None:
            key = _normalize_key(match.group("key"))
            if key in ATTRIBUTION_TRAILER_KEYS:
                findings.append(
                    {"reason": "attribution_trailer", "detail": key, "line": _cap(line)}
                )
                continue
        if _GENERATED_WITH_RE.match(line):
            findings.append(
                {"reason": "generated_with_marker", "detail": "generated-with",
                 "line": _cap(line)}
            )
    return tuple(findings)


def scan_commits(commits: Iterable[tuple[str, str]]) -> tuple[dict, ...]:
    """Scan ``(sha, message)`` pairs and return one entry per OFFENDING commit.

    Entry shape: ``{"sha", "subject", "findings": [...]}``. A clean commit contributes
    nothing, so an empty result means every commit scanned was clean — the caller still
    records HOW MANY it scanned, because "clean" and "never looked" must not read alike.
    """
    flagged: list[dict] = []
    for sha, message in commits:
        findings = attribution_findings(message)
        if not findings:
            continue
        subject = next((ln for ln in (message or "").splitlines() if ln.strip()), "")
        flagged.append(
            {"sha": sha, "subject": _cap(subject.strip()), "findings": list(findings)}
        )
    return tuple(flagged)


def _cap(text: str) -> str:
    return text if len(text) <= _LINE_CAP else text[:_LINE_CAP] + "…"
