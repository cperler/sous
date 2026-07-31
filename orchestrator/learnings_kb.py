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
     text (bounded ~500 chars), files (list), failure_kind (classifier kind|null),
     stage (stage value|null), target ({kind, ref}|null)}

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

from .schemas.enums import FailureKind, Stage

# One entry's ``text`` ceiling — mirrors the context-plane per-item bound so a KB hit
# folded into a prompt is already the right size.
MAX_TEXT = 500

VALID_KINDS = frozenset({"failure", "review", "infra", "salvage", "manual", "process"})

VALID_PROCESS_TARGET_KINDS = frozenset(
    {"stage-template", "agent", "skill", "stage-schema", "kit"}
)

_STAGE_VALUES = frozenset(s.value for s in Stage)
_FAILURE_KIND_VALUES = frozenset(k.value for k in FailureKind)

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return f"lk-{uuid.uuid4().hex[:12]}"


def bound_text(text: str) -> str:
    """Bound one entry's text to ``MAX_TEXT`` chars (mirrors the context-plane item cap)."""
    text = str(text or "").strip()
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - len(" … [truncated]")] + " … [truncated]"


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


def read_entries(path: str | Path) -> list[dict]:
    """Read every KB entry in file order. Tolerant: a corrupt/blank line is skipped (the
    KB is an audit-grade append log — one bad line must never sink recall/harvest)."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("text"):
            out.append(entry)
    return out


def append_learnings(path: str | Path, entries: list[dict], *, dedupe: bool = True) -> list[dict]:
    """Append ``entries`` to the JSONL KB, returning the entries actually written.

    Each input dict supplies ``text`` (required) + optional ``run_id, task_id, kind, files,
    failure_kind, stage, target, ts, id``; missing ``id``/``ts`` are minted here. With
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

    seen = {dedupe_key(e) for e in read_entries(path)} if dedupe else set()
    written: list[dict] = []
    lines: list[str] = []
    for raw in entries:
        text = bound_text(raw.get("text", ""))
        if not text:
            continue
        raw_with_text = {**raw, "text": text}
        key = dedupe_key(raw_with_text)
        if dedupe and key in seen:
            continue
        seen.add(key)
        kind = raw.get("kind") or "manual"
        entry = {
            "id": raw.get("id") or _new_id(),
            "ts": raw.get("ts") or _now(),
            "run_id": raw.get("run_id"),
            "task_id": raw.get("task_id"),
            "kind": kind if kind in VALID_KINDS else "manual",
            "text": text,
            "files": [str(f) for f in (raw.get("files") or [])],
            "failure_kind": raw.get("failure_kind"),
            "stage": raw.get("stage"),
        }
        if raw.get("target") is not None:
            entry["target"] = raw["target"]
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
    the engine can skip the harvest event for it. The task's ``files_changed`` context (if
    folded) tags every entry, so later file-overlap recall can find them."""
    learnings = list(getattr(task, "learnings", None) or [])
    if not learnings:
        return []
    files = _task_files(task)
    task_id = getattr(task, "task_id", None)
    entries = [
        {
            "run_id": run_id,
            "task_id": task_id,
            "kind": classify_kind(text),
            "text": text,
            "files": files,
            "failure_kind": extract_failure_kind(text),
            "stage": extract_stage(text),
            "ts": now,
        }
        for text in learnings
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
    'file-overlap > same failure_kind > same stage > title-token overlap'."""
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
    failure-kind > stage > title-token overlap, recency (ts) as the tiebreak. Only entries
    with at least ONE positive signal are returned — a task matching nothing gets nothing
    (the fold is advisory 'may or may not apply', never random noise). Texts are already
    bounded at write time. ``process`` entries are excluded unconditionally because they
    are harness-maintainer evidence, not advice for a product task."""
    scored: list[tuple[tuple[int, int, int, int], str, dict]] = []
    for entry in read_entries(path):
        if entry.get("kind") == "process":
            continue  # detector fuel, never advice for an unrelated product task
        s = _score(entry, query)
        if sum(s) == 0:
            continue  # no signal at all — not relevant
        scored.append((s, str(entry.get("ts") or ""), entry))
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
