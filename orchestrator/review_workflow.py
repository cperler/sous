"""Deterministic engine-side synthesis of a multi-agent REVIEW (#73, design §3).

A plan-bearing REVIEW dispatch fans out BELOW the seam (independent finder lenses, then
one adversarial verifier per blocking finding) and returns ONE ``StageResult`` carrying
the raw, unfolded panel output in ``sub_results``. This module turns that into canonical
``review.json`` — and it does so as a **pure fold**:

* no wall-clock, no random, no uuid, no I/O, no event sink;
* **no synthesizer model call** — the verdict is arithmetic over what survived, so a
  model can never talk the panel's findings back out of the verdict;
* byte-stable for a given ``sub_results`` (fixed lens order, fixed sort keys), so replay
  reproduces the recorded output exactly.

The engine calls ``synthesize`` at ``record()`` time and then proceeds into the UNCHANGED
downstream (``_merge_policy_findings`` → ``_review_verdict`` → ``_apply_review_rejection``
→ evidence-out). The raw ``sub_results`` persist in the per-stage log as evidence; the
folded review is what the status doc, the context plane, and the convergence math consume.

**Why the signature is ``(review, notices)``, not the shorthand ``-> dict``.** Per the
repo's pure-fold norm (#235/#201), a fold that drops or coerces something observable
RETURNS a notice of what it dropped and lets the engine call site emit the warning-grade
event — the fold never gets an event sink of its own. The panel's input is model-authored
and only schema-shaped (an unvalidated lane, a shim, or a retired schema can hand us a
plain-string finding, a verdict for a finding nobody raised, or two verdicts for one
fingerprint), so silent tolerance would be exactly the invisibility the convention exists
to prevent. ``review`` is the canonical dict the task's shorthand names; ``notices`` is
the audit trail of everything the fold refused to swallow silently.

**Failure direction.** Verification may only REMOVE scrutiny it has affirmatively earned:
a finding is blocking unless a matching verdict explicitly refutes it, so a verifier that
errored, went missing, returned an unmatchable fingerprint, or returned a verdict value we
do not recognize leaves its finding in ``issues``. Refuted findings are never erased —
they land in ``non_blocking`` prefixed ``refuted:`` with the verifier's reasoning, so
evidence-out still files them for a human and the false-negative loop stays closed.
"""

from __future__ import annotations

import re

from .render import format_review_issue

# The named dedupe rule both lanes normalize with (``ReviewPlan.dedupe_rule``): the
# ``file:description`` fingerprint, whitespace-collapsed, casefolded, 160 chars.
FINGERPRINT_RULE = "fingerprint-v1"

# Fixed fold order. A differently-ordered ``findings_by_lens`` dict MUST fold identically,
# so the walk is driven by this tuple (then any unknown lenses, sorted by name) — never by
# the input dict's own key order.
LENS_ORDER: tuple[str, ...] = ("find:code", "find:spec", "find:design", "find:tests")

# The one lens whose ``tests_meaningful`` self-report the fold honours (#13's strong form:
# test-meaningfulness as its own independent dispatch).
TESTS_LENS = "find:tests"

_SEVERITY_RANK: dict[str, int] = {"critical": 0, "important": 1, "suggestion": 2}
# An absent/unrecognized severity is treated as blocking — fail toward scrutiny, the same
# direction as a missing verdict. (``_review_verdict``'s severity gate likewise only
# relaxes on an EXPLICIT ``suggestion``.)
_DEFAULT_SEVERITY = "important"

_MAX_NOTICE_DETAIL = 200  # a notice's detail string, bounded like #201's drop notices
_MAX_TITLE = 80  # a non_blocking title, matching _merge_policy_findings' advisory shape


def issue_fingerprint(issue: object) -> str:
    """Stable convergence/dedupe key for one review finding — the named
    ``fingerprint-v1`` rule (#15, ports the as-built ``file:description`` fingerprint,
    OC:993-999): normalized so cosmetic rewording of the same finding still matches.

    Canonical here (rather than on ``Engine``) because BOTH lanes and the verifier
    sub-calls must normalize identically: a runner computes it to address a verdict at
    a finding, and the fold re-computes it to match that verdict back."""
    if isinstance(issue, dict):
        base = f"{str(issue.get('file') or '').strip()}:{str(issue.get('description') or '').strip()}"
    else:
        base = str(issue)
    return re.sub(r"\s+", " ", base).casefold()[:160]


def synthesize(sub_results: object) -> tuple[dict, tuple[dict[str, str], ...]]:
    """Fold one plan-bearing REVIEW's raw ``sub_results`` into canonical ``review.json``.

    Input (design §2): ``{"findings_by_lens": {lens: review_findings, ...},
    "verdicts": [review_verdict, ...]}``. Every shape here is tolerated rather than
    raised on — a malformed panel must never break ``record()``.

    Rules, in order:

    1. Walk lenses in ``LENS_ORDER``, then any unknown lens sorted by name.
    2. Dedupe findings by fingerprint (first occurrence in that walk wins).
    3. Match verdicts to findings by NORMALIZED fingerprint; the first verdict for a
       fingerprint wins.
    4. ``issues`` = findings that are blocking-severity AND not explicitly refuted,
       stable-sorted by (severity rank, fingerprint). The finding objects are passed
       through UNCHANGED so ``format_review_issue`` / the convergence fingerprint keep
       working on them.
    5. ``non_blocking`` = suggestion findings, plus every refuted finding prefixed
       ``refuted:`` and carrying the verifier's reasoning. Disposition is deliberately
       absent so evidence-out files them (schema default: ``file``).
    6. ``tests_meaningful`` = ``find:tests``'s report; only an explicit ``false`` is
       vacuous (fail-OPEN preserved), so a docs-only plan that omits the lens folds to
       ``true``.
    7. ``approved`` = ``issues`` empty AND tests not vacuous. Nothing else.
    8. ``improvement`` / ``retrospective`` = the first non-null in lens order, one each
       (the single-improvement/single-retrospective shape today's consumers expect);
       omitted entirely when no lens supplied one.

    Returns ``(review, notices)`` — see the module docstring for why.
    """

    notices: list[dict[str, str]] = []
    root: dict = {}
    if isinstance(sub_results, dict):
        root = sub_results
    elif sub_results is not None:
        notices.append(_notice(
            "sub_results_malformed",
            f"expected an object, got {type(sub_results).__name__} — folded as an empty panel",
        ))

    by_lens = _lens_map(root.get("findings_by_lens"), notices)
    verdicts = _index_verdicts(root.get("verdicts"), notices)

    ranked: list[tuple[int, str, object]] = []
    non_blocking: list[dict[str, str]] = []
    seen: set[str] = set()
    tests_meaningful: bool | None = None
    improvement: object = None
    retrospective: object = None

    for lens in _lens_walk(by_lens, notices):
        payload = by_lens[lens]
        if not isinstance(payload, dict):
            notices.append(_notice(
                "lens_payload_malformed",
                f"lens {lens!r} payload is {type(payload).__name__}, not an object — skipped",
            ))
            continue

        reported = payload.get("tests_meaningful")
        if reported is not None:
            if lens != TESTS_LENS:
                notices.append(_notice(
                    "tests_meaningful_ignored",
                    f"lens {lens!r} reported tests_meaningful={reported!r}; only "
                    f"{TESTS_LENS} judges test meaningfulness — ignored",
                ))
            elif not isinstance(reported, bool):
                notices.append(_notice(
                    "tests_meaningful_ignored",
                    f"{TESTS_LENS} reported a non-boolean tests_meaningful "
                    f"({type(reported).__name__}) — ignored (fails OPEN)",
                ))
            elif tests_meaningful is None:
                tests_meaningful = reported

        if payload.get("improvement") is not None:
            if improvement is None:
                improvement = payload["improvement"]
            else:
                notices.append(_notice(
                    "improvement_dropped",
                    f"lens {lens!r} supplied an improvement but an earlier lens already "
                    "did — review.json carries one",
                ))
        if payload.get("retrospective") is not None:
            if retrospective is None:
                retrospective = payload["retrospective"]
            else:
                notices.append(_notice(
                    "retrospective_dropped",
                    f"lens {lens!r} supplied a retrospective but an earlier lens already "
                    "did — review.json carries one",
                ))

        for raw_finding in _findings(payload, lens, notices):
            finding = _keep(raw_finding)
            if finding is None:
                notices.append(_notice(
                    "finding_skipped",
                    f"lens {lens!r}: malformed or description-less finding "
                    f"{_bound(repr(raw_finding))}",
                ))
                continue
            fingerprint = issue_fingerprint(finding)
            if fingerprint in seen:  # cross-lens agreement: the contract, not a drop
                continue
            seen.add(fingerprint)
            severity = _severity(finding, lens, notices)
            verdict = verdicts.get(fingerprint)
            if verdict is not None and verdict["verdict"] == "refuted":
                non_blocking.append(_refuted_entry(finding, verdict["reasoning"]))
            elif severity == "suggestion":
                non_blocking.append(_advisory_entry(finding))
            else:
                ranked.append((_SEVERITY_RANK[severity], fingerprint, finding))

    for fingerprint in verdicts:
        if fingerprint not in seen:
            notices.append(_notice(
                "verdict_without_finding",
                f"verdict for fingerprint {_bound(fingerprint)} matches no finding — ignored",
            ))

    issues = [finding for _, _, finding in sorted(ranked, key=lambda r: (r[0], r[1]))]
    vacuous = tests_meaningful is False
    review: dict = {
        "approved": not issues and not vacuous,
        "issues": issues,
        "non_blocking": non_blocking,
        "tests_meaningful": not vacuous,
    }
    if improvement is not None:
        review["improvement"] = improvement
    if retrospective is not None:
        review["retrospective"] = retrospective
    return review, tuple(notices)


def _notice(kind: str, detail: str) -> dict[str, str]:
    """One audit notice: what the fold refused to swallow silently. Flat strings only —
    the engine splats it into a ``review_synthesis_notice`` event."""
    return {"notice": kind, "detail": _bound(detail)}


def _bound(text: str) -> str:
    return text if len(text) <= _MAX_NOTICE_DETAIL else text[:_MAX_NOTICE_DETAIL] + " … [truncated]"


def _lens_map(raw: object, notices: list[dict[str, str]]) -> dict[str, object]:
    """Normalize ``findings_by_lens`` to ``{str lens: payload}``; anything else is noticed
    and folded as an empty panel (a REVIEW with no findings, which then approves — the
    same fail-OPEN direction a single reviewer returning ``issues: []`` has)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        notices.append(_notice(
            "findings_by_lens_malformed",
            f"findings_by_lens is {type(raw).__name__}, not an object — folded as an empty panel",
        ))
        return {}
    out: dict[str, object] = {}
    for key, value in raw.items():
        if isinstance(key, str):
            out[key] = value
        else:
            notices.append(_notice(
                "lens_key_malformed", f"non-string lens key {_bound(repr(key))} — skipped"
            ))
    return out


def _lens_walk(by_lens: dict[str, object], notices: list[dict[str, str]]) -> list[str]:
    """The fixed walk order: known lenses in ``LENS_ORDER``, then unknown ones sorted by
    name (so an unrecognized lens still contributes, deterministically, after the ones
    whose precedence the design fixes)."""
    unknown = sorted(k for k in by_lens if k not in LENS_ORDER)
    for lens in unknown:
        notices.append(_notice(
            "unknown_lens",
            f"lens {lens!r} is not one of {', '.join(LENS_ORDER)} — folded after the known lenses",
        ))
    return [lens for lens in LENS_ORDER if lens in by_lens] + unknown


def _findings(payload: dict, lens: str, notices: list[dict[str, str]]) -> list[object]:
    raw = payload.get("findings")
    if raw is None:
        return []
    if not isinstance(raw, list):
        notices.append(_notice(
            "lens_findings_malformed",
            f"lens {lens!r} findings is {type(raw).__name__}, not a list — folded as none",
        ))
        return []
    return list(raw)


def _keep(raw: object) -> object | None:
    """A finding survives normalization only if it says something: a dict with a non-empty
    ``description`` (the schema's one required field, and what
    ``_merge_policy_findings`` demands of a policy finding) or a non-empty plain string
    (``review.json`` accepts string issues, and ``format_review_issue`` renders them).
    Survivors are returned UNCHANGED — the engine's downstream reads them as issues."""
    if isinstance(raw, dict):
        return raw if str(raw.get("description") or "").strip() else None
    if isinstance(raw, str):
        return raw if raw.strip() else None
    return None


def _severity(finding: object, lens: str, notices: list[dict[str, str]]) -> str:
    """The finding's severity, lowercased. Absent (or a plain-string finding, which has no
    severity slot) defaults to blocking silently — it is a legitimate schema shape. A
    PRESENT but unrecognized value is a coercion, so it gets a notice."""
    if not isinstance(finding, dict):
        return _DEFAULT_SEVERITY
    raw = finding.get("severity")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _DEFAULT_SEVERITY
    value = str(raw).strip().lower()
    if value in _SEVERITY_RANK:
        return value
    notices.append(_notice(
        "unknown_severity",
        f"lens {lens!r}: severity {raw!r} is not critical/important/suggestion — "
        f"treated as {_DEFAULT_SEVERITY} (blocking)",
    ))
    return _DEFAULT_SEVERITY


def _index_verdicts(raw: object, notices: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Index the adversarial verifiers' verdicts by NORMALIZED fingerprint, first-wins.

    Everything ambiguous resolves toward scrutiny: a malformed verdict is dropped (its
    finding stays blocking) and an unrecognized ``verdict`` value is recorded as
    ``confirmed`` — only an explicit ``refuted`` may demote a finding."""
    index: dict[str, dict[str, str]] = {}
    if raw is None:
        return index
    if not isinstance(raw, list):
        notices.append(_notice(
            "verdicts_malformed",
            f"verdicts is {type(raw).__name__}, not a list — every finding stays blocking",
        ))
        return index
    for entry in raw:
        if not isinstance(entry, dict):
            notices.append(_notice(
                "verdict_malformed",
                f"verdict {_bound(repr(entry))} is not an object — ignored",
            ))
            continue
        raw_fp = entry.get("fingerprint")
        if not isinstance(raw_fp, str) or not raw_fp.strip():
            notices.append(_notice(
                "verdict_malformed",
                f"verdict {_bound(repr(entry))} carries no fingerprint — ignored "
                "(its finding, whichever it was, stays blocking)",
            ))
            continue
        fingerprint = issue_fingerprint(raw_fp)
        value = str(entry.get("verdict") or "").strip().lower()
        if value not in ("confirmed", "refuted"):
            notices.append(_notice(
                "unknown_verdict",
                f"verdict {entry.get('verdict')!r} for {_bound(fingerprint)} is not "
                "confirmed/refuted — the finding stays blocking",
            ))
            value = "confirmed"
        if fingerprint in index:
            notices.append(_notice(
                "duplicate_verdict",
                f"extra verdict for {_bound(fingerprint)} ignored — the first verdict wins",
            ))
            continue
        index[fingerprint] = {
            "verdict": value,
            "reasoning": str(entry.get("reasoning") or "").strip(),
        }
    return index


def _description(finding: object) -> str:
    if isinstance(finding, dict):
        text = str(finding.get("description") or "").strip()
        return text or format_review_issue(finding)
    return str(finding).strip()


def _advisory_entry(finding: object) -> dict[str, str]:
    """A suggestion-severity finding as a ``non_blocking`` entry — same shape
    ``_merge_policy_findings`` gives an advisory policy finding."""
    return {"title": _description(finding)[:_MAX_TITLE], "detail": format_review_issue(finding)}


def _refuted_entry(finding: object, reasoning: str) -> dict[str, str]:
    """A refuted finding as a ``non_blocking`` entry. The adversary killed it, so it must
    not block — but it must not vanish either: evidence-out files it (no ``disposition``
    key => the schema's ``file`` default) with the refutation attached, so a human closes
    the false-negative loop the verifier just opened."""
    detail = format_review_issue(finding)
    detail += (
        f"\n\nRefuted by the adversarial verifier: {reasoning}" if reasoning
        else "\n\nRefuted by the adversarial verifier (no reasoning given)."
    )
    return {"title": f"refuted: {_description(finding)}"[:_MAX_TITLE], "detail": detail}
