"""Durable, per-project, cross-run learnings knowledge base (#72).

Task learnings (``task.learnings`` — the failure/review/salvage/infra notes the engine
accumulates and folds back into retry prompts) live and die with their run: a new run
re-pays to learn the same flaky infra quirk, the same tricky module, the same review nit.
This module gives those learnings a durable home ACROSS runs of the same project.

Design constraints (engine invariant): retrieval is DETERMINISTIC — pure lexical/structural
matching (file-path overlap, failure-kind, stage, title-token overlap), never an embedding
or a model call. The store is an append-only JSONL file at a per-project location (default
``<runs-root>/learnings-kb.jsonl`` — it lives with the run logs, is retained like them, and
is NOT committed because ``runs/`` is gitignored).

Entry shape::

    {id, ts, run_id, task_id, kind (failure|review|infra|salvage|manual|process),
     text (bounded by KIND — see ``text_cap``), files (list),
     failure_kind (classifier kind|null), stage (stage value|null),
     target ({kind, ref}|null), task_outcome (terminal TaskState value; absent when
     unknown — see ``resolved_defect``)}

Because the log is append-only, the manual maintenance surface (``orchestrator kb prune`` /
``kb backfill-outcomes``, #480) never rewrites a row — it APPENDS an amendment naming one::

    {id, ts, kind: "amendment", amends (target entry id), retired?, task_outcome?, reason?}

``read_entries`` folds those onto their targets at read time, so a retired row leaves recall
while both the original line and the decision that demoted it stay in the file for audit.

Pure functions over the file (the engine wires the path in), so they are trivially
testable and never depend on the engine's working directory.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .schemas.enums import TERMINAL_TASK_STATES, FailureKind, Stage, TaskState

# One entry's ``text`` ceiling. The DEFAULT mirrors the context-plane per-item bound so a
# KB hit folded into a prompt is already the right size.
MAX_TEXT = 500

# #401: the cap is chosen by what an entry's KIND MEANS, not by one global number — the
# same reasoning #289 applied to the context plane's ``_ITEM_CAP_BY_KEY``. A ``failure``
# row is a terse head line plus a failing-test list; 500 chars is the right size for it.
# A ``review`` row is a reviewer's rejection PROSE and a ``process`` row a REVIEW
# retrospective's title+detail, and 500 chars routinely lands mid-sentence there — so the
# lesson recalls into a later task as a fragment and reads as noise even when it is still
# true. Prose kinds get a cap sized to hold a whole rejection detail instead.
#
# Budget: recall folds at most 5 hits (``relevant_learnings(limit=5)``) into
# ``task.context["prior_learnings"]``, so the worst case is ~7.5KB against the context
# plane's 16KB ceiling — and ``prior_learnings`` is the FIRST key that ceiling sheds
# (``ENGINE_INJECTED_KEYS``), so a fat recall can never evict durable stage context.
MAX_TEXT_PROSE = 1500
_MAX_TEXT_BY_KIND: dict[str, int] = {"review": MAX_TEXT_PROSE, "process": MAX_TEXT_PROSE}

# Appended in place of what was cut, so a truncated row is never mistaken for a whole one.
_TRUNC_SUFFIX = " … [truncated]"

# A sentence terminator (with any trailing quote/bracket) followed by whitespace or EOS.
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*(?=\s|$)")

# How much of the budget a boundary cut must retain to be preferred over a hard cut. Below
# this, backing up to the last sentence/word would throw away more meaning than the ragged
# edge costs, so the hard cut wins.
_MIN_BOUNDARY_FRACTION = 0.6

VALID_KINDS = frozenset({"failure", "review", "infra", "salvage", "manual", "process"})

# #480: a maintenance record, NOT a lesson kind — deliberately outside ``VALID_KINDS`` so
# ``append_learnings`` can never mint one and recall can never surface one as advice. An
# amendment carries no ``text``, so a reader predating this change drops it on the existing
# text-required filter rather than folding a contentless row into a prompt.
AMENDMENT_KIND = "amendment"

VALID_PROCESS_TARGET_KINDS = frozenset(
    {"stage-template", "agent", "skill", "stage-schema", "kit"}
)

_STAGE_VALUES = frozenset(s.value for s in Stage)
_FAILURE_KIND_VALUES = frozenset(k.value for k in FailureKind)
_TASK_STATE_VALUES = frozenset(s.value for s in TaskState)
_TERMINAL_STATE_VALUES = frozenset(s.value for s in TERMINAL_TASK_STATES)

# Cheap English/boilerplate stopwords dropped from title-token matching so overlap is
# driven by the substantive terms (module names, feature nouns), not glue words.
_STOP = frozenset({
    "the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "is", "it", "this",
    "that", "with", "from", "by", "at", "as", "be", "are", "add", "fix", "use", "via",
    "into", "when", "not", "no", "new", "make", "run", "task", "issue",
})

_ATTEMPT_RE = re.compile(r"\(attempt \d+\)", re.IGNORECASE)
_STAGE_PREFIX_RE = re.compile(r"^([a-z_]+) \(attempt", re.IGNORECASE)
_FAILURE_KIND_TAG_RE = re.compile(r"\[(" + "|".join(sorted(_FAILURE_KIND_VALUES)) + r")\]")

# --- capacity/rate-limit notices (#384) ---------------------------------------------
#
# A provider capacity notice ("You've hit your session limit · resets 3:50pm") is an INFRA
# event, already durable in ``events.jsonl`` + ``stage-costs.jsonl``. As a cross-run learning
# it teaches nothing and carries a wall-clock time that is meaningless by the next run, yet
# it competed for — and won — recall slots ahead of real lessons. These markers are a
# deliberate engine-side COPY of ``adapters.execution.transport``'s: the dependency arrow
# points inward (#273), so ``orchestrator/`` may not import ``adapters`` to share them.
#
# The two channels are asymmetric for the same reason they are in the transport. A learning's
# BODY carries task content (a test log, a reviewer's prose about a rate limiter the task is
# implementing), so only the provider CLI's own first-person phrasing is matched there. The
# broad status words are matched only against an engine-generated head line's error text, and
# only as a PREFIX of it — that is where ``_failure_learning``/``_harvest_retrospective`` put
# ``result.error``, and nowhere a task's own subject matter can reach.
_PROVIDER_LIMIT_NOTICE_MARKERS = (
    "hit your session limit", "hit your usage limit",
    "session limit reached", "usage limit reached",
)
_HEAD_LIMIT_PREFIXES = (
    "rate-limited", "rate limited", "rate_limited", "rate limit", "ratelimit",
    "429", "too many requests", "overloaded", "usage limit", "session limit",
)
# The engine-authored prefixes a learning's error text can follow: one failed attempt
# (``_failure_learning``) or one distilled retrospective pattern (``_harvest_retrospective``).
_LEARNING_HEAD_RE = re.compile(
    r"^(?:[a-z_]+ \(attempt \d+\)|recurring failure at [a-z_]+ \([^)]*\)):\s*",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return f"lk-{uuid.uuid4().hex[:12]}"


def text_cap(kind: str | None = None) -> int:
    """The write-time ``text`` cap for one entry, by its KIND (#401).

    Prose kinds (``review``, ``process``) get ``MAX_TEXT_PROSE``; everything else — and any
    unrecognized kind — gets the default ``MAX_TEXT``. Pure and total."""
    return _MAX_TEXT_BY_KIND.get(str(kind or ""), MAX_TEXT)


def bound_text(text: str, kind: str | None = None) -> str:
    """Bound one entry's text to its kind's cap, cutting on a SENTENCE or WORD boundary.

    Deterministic and pure — no wall-clock, no randomness — so a replayed harvest writes
    byte-identical rows.

    Over the cap, the text is cut to the last sentence end that still retains
    ``_MIN_BOUNDARY_FRACTION`` of the budget; failing that, the last word boundary meeting
    the same floor; failing that, a hard cut (a single unbroken token has no boundary to
    find). The result always ends in ``_TRUNC_SUFFIX`` and never exceeds the cap, so a
    truncated row still reads as truncated (#401 — a mid-sentence fragment recalls as noise
    even when the lesson it carries is true)."""
    text = str(text or "").strip()
    cap = text_cap(kind)
    if len(text) <= cap:
        return text
    budget = cap - len(_TRUNC_SUFFIX)
    if budget <= 0:  # pathological cap; degrade to a hard cut rather than over-run it
        return text[:cap]
    head = text[:budget]
    floor = int(budget * _MIN_BOUNDARY_FRACTION)

    cut = -1
    for m in _SENTENCE_END_RE.finditer(head):
        if m.end() >= floor:
            cut = m.end()  # ascending scan, so the LAST qualifying end wins
    if cut < 0:
        ws = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
        if ws >= floor:
            cut = ws
    kept = (head[:cut] if cut > 0 else head).rstrip()
    return kept + _TRUNC_SUFFIX


def tokenize(text: str) -> list[str]:
    """Lexical tokens for title-overlap scoring: lowercased alphanumeric runs, minus
    stopwords and 1-2 char noise. Deterministic — a pure function of the text."""
    toks = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _STOP]


def _fingerprint(text: str) -> str:
    """Normalized-text dedupe key: collapse whitespace, blank the varying ``(attempt N)``
    counter, casefold. So the SAME lesson re-learned on a later attempt/run fingerprints
    identically and is skipped, while genuinely distinct lessons stay distinct."""
    t = _ATTEMPT_RE.sub("(attempt)", str(text or ""))
    return re.sub(r"\s+", " ", t).strip().casefold()


# --- classification of a raw learning string ----------------------------------------


def classify_kind(text: str) -> str:
    """Best-effort bucket for one ``task.learnings`` string (see engine._failure_learning,
    _apply_review_rejection, _apply_salvage, _apply_fallthrough, _settle_failed_session)."""
    low = text.lower()
    if low.startswith("review rejected"):
        return "review"
    if (
        "environment reset" in low
        or "provider was unavailable" in low
        or "infrastructure failure" in low
    ):
        return "infra"
    if "committed work before it failed" in low or "warm retry" in low:
        return "salvage"
    return "failure"


def is_capacity_notice(text: str) -> bool:
    """True if one learning is a provider capacity/rate-limit notice rather than a lesson.

    Deterministic and pure. Two deliberately asymmetric channels (see the marker tables):
    the provider CLI's first-person limit notice matches ANYWHERE in the text, while the
    broad status words ("rate-limited", "429", "overloaded") match only as the PREFIX of an
    engine-authored head line's error text. So a live limit notice quoted in a failure's
    output tail is caught, and a review finding *about* a rate limiter the task is building
    is not.
    """
    low = str(text or "").strip().lower()
    if not low:
        return False
    if any(m in low for m in _PROVIDER_LIMIT_NOTICE_MARKERS):
        return True
    head = low.split("\n", 1)[0]
    m = _LEARNING_HEAD_RE.match(head)
    if not m:
        return False  # no engine-authored prefix — never guess from a task's own prose
    return head[m.end():].lstrip("\"'").startswith(_HEAD_LIMIT_PREFIXES)


# --- resolved review findings (#393) -------------------------------------------------
#
# #384 asked whether a ``review`` entry describing a defect that was subsequently FIXED
# should age out, and deliberately left it alone: nothing in the KB recorded resolution, so
# a TTL, an "N runs later" decay, or a recency weight in ``_score`` would all have been a
# guess about relevance rather than a fact about the finding.
#
# It turns out resolution IS a fact available at write time. A rejection learning is not
# harvested when the reviewer rejects — it is harvested at task FINALIZE, and every
# ``Engine._harvest_task_learnings`` call site fires only once the task is terminal. So the
# task's outcome is already known when the row is written: a ``review`` learning harvested
# from a task that reached COMPLETED describes a defect that the SAME task's fix cycle
# closed on its way to completing. ``harvest_from_task`` stamps ``task_outcome``, and no
# tombstone-append convention over the append-only JSONL is needed.
#
# Two deliberate narrowings:
#
# - Only ``review`` counts. A ``failure``/``infra``/``salvage`` lesson (a flaky harness, a
#   tricky module, an environment quirk) generalizes past the instance that produced it and
#   stays true after the task completes; a rejection about a specific line does not.
# - An absent ``task_outcome`` is UNRESOLVED, never resolved. Every row written before this
#   change lacks the field, so unknown must not be read as fixed — no legacy row is silently
#   dropped from recall by the mere addition of the stamp.
#
# Recency was rejected as the mechanism: it is a proxy for relevance, whereas resolution is
# the thing the proxy was standing in for, and ``relevant_learnings`` already orders
# equal-signal entries by ``ts``.
_COMPLETED_OUTCOME = TaskState.COMPLETED.value


def resolved_defect(entry: dict) -> bool:
    """True if one KB entry describes a defect that has since been FIXED (#393).

    Pure and total. True only for a ``review`` entry stamped with a COMPLETED
    ``task_outcome`` — the reviewer's finding was blocking, and the task nonetheless reached
    completion, so the fix cycle that followed the rejection closed it. Any other kind, any
    non-completed outcome, and any entry missing the stamp (every row predating #393) is
    False, so 'unknown' reads as still-live rather than as resolved.
    """
    if not isinstance(entry, dict) or entry.get("kind") != "review":
        return False
    return str(entry.get("task_outcome") or "") == _COMPLETED_OUTCOME


def _terminal_outcome(task: object) -> str | None:
    """The terminal-state VALUE of a finished task, for the ``task_outcome`` stamp.

    Duck-typed and tolerant in the same style as ``_task_files``: a task doc without a
    usable ``state`` yields None and the field is simply omitted, which ``resolved_defect``
    reads as unresolved."""
    state = getattr(task, "state", None)
    value = getattr(state, "value", state)
    if not isinstance(value, str) or not value.strip():
        return None
    return value if value in _TASK_STATE_VALUES else None


def mentioned_files(text: str, files: list[str]) -> list[str]:
    """The subset of ``files`` a learning's text actually names — its FILE LOCUS (#384).

    A learning used to inherit its task's whole ``files_changed`` list, and ``_score`` lets
    any file overlap strictly dominate every other tier, so a contentless failure stamped
    with ten paths outranked every real lesson for any later task touching that package. An
    entry that cannot say which file it is about should compete on kind/stage/tokens
    instead, so it gets no files at all.

    Matched by full path or by basename with word-ish boundaries (``cli.py:172``,
    ``tests/test_x.py::test_y``), against the task's OWN changed files only — never an
    arbitrary path-shaped token in the prose.
    """
    low = str(text or "").lower()
    if not low.strip():
        return []
    out: list[str] = []
    for raw in files:
        path = str(raw).strip()
        if not path:
            continue
        lp = path.lower()
        base = lp.rstrip("/").rsplit("/", 1)[-1]
        if lp in low or (
            base and re.search(rf"(?:^|[^\w./-]){re.escape(base)}(?![\w-])", low)
        ):
            out.append(path)
    return out


def extract_stage(text: str) -> str | None:
    """The stage a learning is about, from its ``<stage> (attempt N):`` prefix (or a
    ``review rejected`` note). None when the string carries no stage marker."""
    m = _STAGE_PREFIX_RE.match(text)
    if m and m.group(1).lower() in _STAGE_VALUES:
        return m.group(1).lower()
    if text.lower().startswith("review rejected"):
        return Stage.REVIEW.value
    return None


def extract_failure_kind(text: str) -> str | None:
    """The classifier kind a learning mentions, from a ``[unit]``/``[e2e]``/``[infra]`` tag
    or an infrastructure note. None when none is present."""
    m = _FAILURE_KIND_TAG_RE.search(text)
    if m:
        return m.group(1)
    if "infrastructure failure" in text.lower():
        return FailureKind.INFRA.value
    return None


def _task_files(task: object) -> list[str]:
    """The files this task touched, from the folded ``files_changed`` context (present once
    IMPLEMENT has run). Bounded and stringified; [] when unknown."""
    ctx = getattr(task, "context", None) or {}
    files = ctx.get("files_changed")
    if not isinstance(files, list):
        return []
    return [str(f) for f in files[:40] if str(f).strip()]


# --- read / append ------------------------------------------------------------------


def read_records(path: str | Path) -> list[dict]:
    """Every JSON object in the file, in file order — lessons AND amendments (#480).

    Tolerant: a corrupt/blank line is skipped (the KB is an audit-grade append log — one bad
    line must never sink recall/harvest). This is the RAW view; ``read_entries`` is the
    folded lesson view almost every caller wants."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _is_amendment(record: dict) -> bool:
    """True for an amendment record (#480). Keyed on the kind AND a usable ``amends`` ref,
    so a malformed row is ignored rather than silently amending nothing."""
    return record.get("kind") == AMENDMENT_KIND and bool(str(record.get("amends") or "").strip())


def _apply_amendments(records: list[dict]) -> list[dict]:
    """Fold amendment records onto the lesson rows they name — pure, deterministic (#480).

    Two passes rather than one, so an amendment is applied regardless of where it sits
    relative to its target: pass one collects the lessons (a row with ``text`` that is not
    itself an amendment), pass two applies each amendment in FILE ORDER, so a later
    amendment overrides an earlier one for the same field. An amendment naming an unknown
    id is a no-op — it stays in the raw log as the record of a decision, but invents no
    lesson.

    Entries are copied, never mutated in place, and an unamended lesson comes back
    byte-identical to what was written."""
    entries = [dict(r) for r in records if not _is_amendment(r) and r.get("text")]
    by_id = {str(e.get("id")): e for e in entries if e.get("id")}
    for record in records:
        if not _is_amendment(record):
            continue
        target = by_id.get(str(record.get("amends")).strip())
        if target is None:
            continue
        if record.get("retired"):
            target["retired"] = True
            reason = str(record.get("reason") or "").strip()
            if reason:
                target["retired_reason"] = reason
        outcome = record.get("task_outcome")
        if isinstance(outcome, str) and outcome in _TASK_STATE_VALUES:
            target["task_outcome"] = outcome
    return entries


def read_entries(path: str | Path, *, include_retired: bool = False) -> list[dict]:
    """Read every KB LESSON in file order, with amendments folded in (#480).

    Retired rows are excluded by default: recall, the meta-authoring detector, and
    ``kb show`` all want the live pool. Pass ``include_retired=True`` for the audit view
    (``kb show --include-retired``) and for dedupe seeding, where a retired row must still
    suppress a re-append — see ``append_learnings``.

    Tolerant of a corrupt/blank line, as an audit-grade append log must be."""
    entries = _apply_amendments(read_records(path))
    if include_retired:
        return entries
    return [e for e in entries if not e.get("retired")]


def append_learnings(path: str | Path, entries: list[dict], *, dedupe: bool = True) -> list[dict]:
    """Append ``entries`` to the JSONL KB, returning the entries actually written.

    Each input dict supplies ``text`` (required) + optional ``run_id, task_id, kind, files,
    failure_kind, stage, target, task_outcome, ts, id``; missing ``id``/``ts`` are minted
    here, and ``target``/``task_outcome`` are written only when present so a row that knows
    neither stays clean. With
    ``dedupe`` enabled, ordinary learnings use a global normalized-text fingerprint, while
    ``process`` observations include ``run_id`` and normalized target identity in that key.
    The latter keeps replay within one run idempotent without erasing either cross-run
    repetition or same-run observations about different artifacts.
    """
    path = Path(path)

    def dedupe_key(
        entry: dict,
    ) -> tuple[str, str | None, str | None, str | None] | tuple[str]:
        """Process observations must recur across runs to be useful detector fuel.

        Ordinary task learnings retain their historical global text dedupe. Process
        observations dedupe only within a run and target, making task-finalize replay
        idempotent without erasing a same-worded observation about another artifact or the
        same complaint when a later run independently repeats it.
        """
        fp = _fingerprint(entry.get("text", ""))
        if entry.get("kind") == "process":
            target = entry.get("target")
            kind: str | None = None
            ref: str | None = None
            if isinstance(target, dict):
                kind = re.sub(r"\s+", " ", str(target.get("kind") or "")).strip().casefold()
                ref = re.sub(r"\s+", " ", str(target.get("ref") or "")).strip().casefold()
            return (fp, entry.get("run_id"), kind or None, ref or None)
        return (fp,)

    # include_retired: a RETIRED row must still suppress a re-append (#480). Retiring is a
    # human judgement that a lesson is stale; letting the next harvest of the same text
    # resurrect it would make the prune undo itself on the very next run.
    seen = (
        {dedupe_key(e) for e in read_entries(path, include_retired=True)} if dedupe else set()
    )
    written: list[dict] = []
    lines: list[str] = []
    for raw in entries:
        # Normalize the kind FIRST: it selects the text cap (#401), so an unrecognized kind
        # must fall back to "manual"'s default bound rather than miss the table both ways.
        kind = raw.get("kind") or "manual"
        kind = kind if kind in VALID_KINDS else "manual"
        text = bound_text(raw.get("text", ""), kind)
        if not text:
            continue
        raw_with_text = {**raw, "text": text}
        key = dedupe_key(raw_with_text)
        if dedupe and key in seen:
            continue
        seen.add(key)
        entry = {
            "id": raw.get("id") or _new_id(),
            "ts": raw.get("ts") or _now(),
            "run_id": raw.get("run_id"),
            "task_id": raw.get("task_id"),
            "kind": kind,
            "text": text,
            "files": [str(f) for f in (raw.get("files") or [])],
            "failure_kind": raw.get("failure_kind"),
            "stage": raw.get("stage"),
        }
        if raw.get("target") is not None:
            entry["target"] = raw["target"]
        if raw.get("task_outcome") is not None:
            entry["task_outcome"] = str(raw["task_outcome"])
        lines.append(json.dumps(entry, separators=(",", ":"), ensure_ascii=False))
        written.append(entry)
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return written


def harvest_from_task(path: str | Path, task: object, run_id: str, *, now: str | None = None) -> list[dict]:
    """Distil one finished task's ``learnings`` into the KB (dedupe-guarded). Returns the
    entries written — empty for a task that learned nothing (a clean first-pass task), so
    the engine can skip the harvest event for it.

    Two #384 filters keep the KB's recall pool honest:

    - A provider capacity/rate-limit notice is NOT a learning (``is_capacity_notice``) and is
      dropped here. It is an infra event, already durable in the run log, and its wall-clock
      reset time is meaningless by the next run.
    - Each entry is tagged with only the task files its own text NAMES (``mentioned_files``),
      not the task's whole ``files_changed`` list. File overlap strictly dominates recall, so
      an inherited path list let an entry with no file locus outrank every real lesson.

    Every row is also stamped with the task's terminal state as ``task_outcome`` (#393).
    This function only ever runs at task FINALIZE, so the outcome is a fact by the time the
    row is written — which is what lets ``resolved_defect`` tell a still-live rejection from
    one the same task's fix cycle already closed, with no update path on the append-only log.
    """
    learnings = list(getattr(task, "learnings", None) or [])
    if not learnings:
        return []
    files = _task_files(task)
    task_id = getattr(task, "task_id", None)
    outcome = _terminal_outcome(task)
    entries = [
        {
            "run_id": run_id,
            "task_id": task_id,
            "kind": classify_kind(text),
            "text": text,
            "files": mentioned_files(text, files),
            "failure_kind": extract_failure_kind(text),
            "stage": extract_stage(text),
            "task_outcome": outcome,
            "ts": now,
        }
        for text in learnings
        if not is_capacity_notice(text)
    ]
    return append_learnings(path, entries)


def harvest_process_retrospective(
    path: str | Path, task: object, run_id: str, *, now: str | None = None
) -> list[dict]:
    """Persist one REVIEW process retrospective as detector-only KB evidence.

    Interactive lanes may bypass JSON-schema validation, so malformed values are treated
    as absent. A malformed optional target is dropped while the useful lesson is retained.
    Returns the entries actually written, or an empty list for absent, invalid, or replayed
    evidence. Process entries feed recurrence detection and are excluded from task recall.
    """
    stages = getattr(task, "stages", None) or {}
    review = stages.get(Stage.REVIEW)
    output = getattr(review, "output", None) if review is not None else None
    retrospective = output.get("retrospective") if isinstance(output, dict) else None
    if not isinstance(retrospective, dict):
        return []
    title = retrospective.get("title")
    if not isinstance(title, str) or not title.strip():
        return []
    detail = retrospective.get("detail")
    text = title.strip()
    if isinstance(detail, str) and detail.strip():
        text = f"{text}: {detail.strip()}"

    target = retrospective.get("target")
    clean_target: dict[str, str] | None = None
    if isinstance(target, dict):
        kind, ref = target.get("kind"), target.get("ref")
        if kind in VALID_PROCESS_TARGET_KINDS and isinstance(ref, str) and ref.strip():
            clean_target = {"kind": kind, "ref": ref.strip()}

    return append_learnings(
        path,
        [{
            "run_id": run_id,
            "task_id": getattr(task, "task_id", None),
            "kind": "process",
            "text": text,
            "files": [],
            "failure_kind": None,
            "stage": Stage.REVIEW.value,
            "target": clean_target,
            "ts": now,
        }],
    )


# --- manual maintenance: prune + outcome backfill (#480) -----------------------------
#
# The KB is an append-only, audit-grade log, so "prune" cannot mean "rewrite the file".
# Deleting a row would destroy the evidence that the row ever recalled into a run — which is
# exactly what a later audit of a bad decision needs — and would leave no trace of WHO
# decided it was stale or why. So a prune APPENDS: a retirement amendment naming the entry's
# id, folded at read time by ``_apply_amendments``. The original line and the decision to
# demote it both survive.
#
# An in-place rewrite was not merely undesirable, it was unavailable: ``append_learnings``
# dedupes on a global normalized-text fingerprint, so re-appending a corrected copy of a row
# is silently suppressed. Amending by reference is the only shape that works with the log's
# existing invariants.
#
# The backfill exists because #393's ``task_outcome`` stamp only reaches rows written after
# it merged. Every older row lacks the stamp and therefore reads (correctly, per
# ``resolved_defect``'s narrowing) as still-live — so the resolved-finding demotion reached
# none of the accumulated data. The outcome is recoverable after the fact: the task doc in
# the run log records the terminal state the harvest would have stamped.


def append_amendments(path: str | Path, amendments: list[dict]) -> list[dict]:
    """Append maintenance amendments to the KB, returning the records actually written.

    Each input supplies ``amends`` (the target entry id, required) plus any of
    ``retired`` (bool), ``task_outcome`` (a ``TaskState`` value), and ``reason``. An
    amendment carrying no actual change, or no target, is skipped rather than written — an
    audit log should not accumulate rows that assert nothing.

    Unlike ``append_learnings`` there is no dedupe: re-retiring an already-retired row is
    idempotent in effect, and the second record is itself evidence (a human looked again).
    """
    path = Path(path)
    written: list[dict] = []
    lines: list[str] = []
    for raw in amendments:
        target = str(raw.get("amends") or "").strip()
        if not target:
            continue
        retired = bool(raw.get("retired"))
        outcome = raw.get("task_outcome")
        outcome = str(outcome) if outcome is not None else None
        if outcome is not None and outcome not in _TASK_STATE_VALUES:
            outcome = None  # never stamp a state the schema does not know
        if not retired and outcome is None:
            continue  # asserts nothing
        record: dict = {
            "id": raw.get("id") or _new_id(),
            "ts": raw.get("ts") or _now(),
            "kind": AMENDMENT_KIND,
            "amends": target,
        }
        if retired:
            record["retired"] = True
        if outcome is not None:
            record["task_outcome"] = outcome
        reason = str(raw.get("reason") or "").strip()
        if reason:
            record["reason"] = bound_text(reason)
        lines.append(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
        written.append(record)
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return written


def select_entries(
    entries: list[dict],
    *,
    ids: list[str] | None = None,
    kinds: list[str] | None = None,
    run_id: str | None = None,
    before: str | None = None,
    resolved: bool = False,
    unstamped: bool = False,
) -> list[dict]:
    """The entries a prune would retire, by deterministic selectors. Pure and total.

    Selectors AND together (``--kind review --before 2026-07-01`` is 'review rows older
    than July'). An EMPTY selector set selects NOTHING, not everything: the destructive
    reading of a bare ``kb prune`` must never be 'retire the whole KB'.

    ``before`` compares against the entry's ISO-8601 ``ts`` lexicographically, which is a
    correct chronological compare for that format and lets a bare date (``2026-07-01``) work
    as a prefix bound. ``resolved`` selects the #393 resolved-defect rows; ``unstamped``
    selects ``review`` rows carrying no ``task_outcome`` at all — the legacy population the
    backfill could not resolve.
    """
    if not any([ids, kinds, run_id, before, resolved, unstamped]):
        return []
    id_set = {str(i) for i in (ids or [])}
    kind_set = {str(k) for k in (kinds or [])}
    out: list[dict] = []
    for entry in entries:
        if id_set and str(entry.get("id") or "") not in id_set:
            continue
        if kind_set and str(entry.get("kind") or "") not in kind_set:
            continue
        if run_id and str(entry.get("run_id") or "") != run_id:
            continue
        if before and not str(entry.get("ts") or "") < before:
            continue
        if resolved and not resolved_defect(entry):
            continue
        if unstamped and (entry.get("kind") != "review" or entry.get("task_outcome")):
            continue
        out.append(entry)
    return out


def outcome_from_run_logs(runs_root: str | Path, entry: dict) -> str | None:
    """The terminal ``task_outcome`` for one legacy entry, recovered from the run log (#480).

    Reads the task doc the entry's ``run_id``/``task_id`` name
    (``<runs-root>/<run>/status-<run>-<task>.json``, falling back to the flat
    ``<runs-root>/status-<run>-<task>.json`` layout) and returns its state.

    Returns None — leaving the row unstamped, which ``resolved_defect`` reads as still-live —
    for every uncertain case: an entry with no run/task ids, a run dir the human has since
    pruned, an unreadable or unparseable doc, and a state that is not a KNOWN TERMINAL one.
    That last narrowing matters: a doc left mid-flight at ``running`` says nothing about
    whether the finding was fixed, and #393's rule is that unknown must never read as
    resolved.
    """
    run_id = str(entry.get("run_id") or "").strip()
    task_id = str(entry.get("task_id") or "").strip()
    if not run_id or not task_id:
        return None
    # These ids reach the filesystem, so refuse any that could climb out of the runs root.
    if any(bad in part for part in (run_id, task_id) for bad in ("/", "\\", "..")):
        return None
    root = Path(runs_root)
    name = f"status-{run_id}-{task_id}.json"
    for candidate in (root / run_id / name, root / name):
        try:
            doc = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        state = doc.get("state")
        if isinstance(state, str) and state in _TERMINAL_STATE_VALUES:
            return state
        return None  # found the doc; it simply is not terminal
    return None


# --- deterministic recall -----------------------------------------------------------


def _dirname(p: str) -> str:
    p = p.strip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _file_match(a: str, b: str) -> bool:
    """Structural path relatedness: same file, a path-prefix of the other, or same
    directory (the same tricky module). Empty dirnames (root files) don't match wholesale."""
    a, b = a.strip("/"), b.strip("/")
    if not a or not b:
        return False
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    da, db = _dirname(a), _dirname(b)
    return bool(da) and da == db


def _score(entry: dict, query: dict) -> tuple[int, int, int, int]:
    """Deterministic relevance tiers, most→least significant: file-overlap, failure-kind,
    stage, title-token overlap. Lexicographic tuple ordering makes each tier strictly
    dominate the next (any file overlap beats any kind/stage/token match), which is exactly
    'file-overlap > same failure_kind > same stage > title-token overlap'.

    These are the SIGNAL tiers only, and deliberately so: ``relevant_learnings`` uses
    ``sum(...) == 0`` as its relevance floor, so anything that is not evidence of a match
    (the #393 resolution demotion) must be spliced into the sort key there rather than
    added here, or every entry would clear the floor and nothing would be filtered."""
    efiles = [str(f) for f in (entry.get("files") or [])]
    qfiles = [str(f) for f in (query.get("files") or [])]
    file_overlap = sum(1 for q in qfiles if any(_file_match(q, e) for e in efiles))
    qkind = query.get("failure_kind")
    kind_match = 1 if qkind and entry.get("failure_kind") == qkind else 0
    qstage = query.get("stage")
    stage_match = 1 if qstage and entry.get("stage") == qstage else 0
    qtok = set(query.get("title_tokens") or [])
    token_overlap = len(qtok & set(tokenize(entry.get("text", "")))) if qtok else 0
    return (file_overlap, kind_match, stage_match, token_overlap)


def relevant_learnings(path: str | Path, query: dict, *, limit: int = 5) -> list[str]:
    """The ``limit`` most-relevant prior-learning TEXTS for a task, by deterministic score.

    ``query``: ``{files, stage, failure_kind, title_tokens}``. Ordering is file-overlap >
    NOT-resolved > failure-kind > stage > title-token overlap, recency (ts) as the tiebreak.
    Only entries with at least ONE positive signal are returned — a task matching nothing
    gets nothing (the fold is advisory 'may or may not apply', never random noise). Texts
    are already bounded at write time. ``process`` entries are excluded unconditionally
    because they are harness-maintainer evidence, not advice for a product task;
    capacity/rate-limit notices are excluded for the same reason they are no longer
    harvested (#384) — the filter is applied at READ time too because the KB is
    append-only, so rows written before the harvest filter existed would otherwise keep
    winning slots forever.

    A RETIRED row (#480) never reaches the scoring at all: ``read_entries`` drops it, which
    is the whole point of the manual prune — an entry a human judged stale should stop
    competing for slots, while staying in the file for audit.

    A RESOLVED review finding (#393, ``resolved_defect``) is DEMOTED rather than excluded,
    unlike those two. A capacity notice and a process observation teach a product task
    nothing at all, so they take no slot; a rejection that was fixed is at worst stale about
    its own code but still names a real hazard in a file, so it earns a low slot instead of
    losing outright. Slotting the bit directly below file-overlap gives it exactly that
    force: among entries touching the same files, every still-live lesson outranks every
    resolved one, but a resolved finding about the file actually in play still beats a
    stranger matching on a stage or a token."""
    scored: list[tuple[tuple[int, int, int, int, int], str, dict]] = []
    for entry in read_entries(path):
        if entry.get("kind") == "process":
            continue  # detector fuel, never advice for an unrelated product task
        if is_capacity_notice(str(entry.get("text") or "")):
            continue  # an infra event, not a lesson — and legacy rows carry inherited files
        s = _score(entry, query)
        if sum(s) == 0:
            continue  # no signal at all — not relevant
        live = 0 if resolved_defect(entry) else 1
        key = (s[0], live, s[1], s[2], s[3])
        scored.append((key, str(entry.get("ts") or ""), entry))
    # score desc, then recency desc; stable for exact ties (file order preserved).
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [entry["text"] for _, _, entry in scored[:limit]]


def resolve_kb_path(runs_root: str | Path, project: object | None = None) -> Path:
    """Resolve the KB file path. Precedence: a duck-typed project override
    (``ProjectConfig.learnings_kb_path`` — attribute or zero-arg callable), then the
    ``ORCHESTRATOR_LEARNINGS_KB_PATH`` env override, then the default
    ``<runs-root>/learnings-kb.jsonl`` (lives with the run logs, per-project durable)."""
    override = getattr(project, "learnings_kb_path", None) if project is not None else None
    if override:
        return Path(override() if callable(override) else override)
    env = os.environ.get("ORCHESTRATOR_LEARNINGS_KB_PATH")
    if env:
        return Path(env)
    return Path(runs_root) / "learnings-kb.jsonl"
