"""In-flight stream sensing (#66): a cheap, deterministic live snapshot of a headless
provider stream that is still being written.

A headless stage (``claude -p`` / ``codex exec``) can run 10-30 minutes. While it runs
the engine only knows the stage is RUNNING — not *what* the model is doing. The execution
adapters tee the provider's stdout to ``stages/<task>/<stage>-attempt<N>.stream.jsonl`` as
it arrives (see ``adapters.execution.transport``); this module turns that partially-written
file into a snapshot: how many events so far, when it last grew, the current activity (last
tool_use / command, bounded), and a raw tail.

Deterministic by contract: it PARSES the stream, it never interprets it. It tolerates a
partial/truncated trailing line (an in-progress write) by skipping any line that isn't yet
valid JSON, and a missing file returns ``None``.

This module also OWNS the stream-file naming (``stream_basename`` / ``stream_relpath`` /
``stream_filename``) so the teeing adapter, the probe, and the tail CLI all agree on where a
stage's stream lives — extracted here, in the ``orchestrator`` layer, so the ``adapters``
layer can import it without the engine ever importing ``adapters``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from .status_store import safe_task_dirname

# --- stream-file naming (single source of truth) -----------------------------------------


def safe_phase(phase: str) -> str:
    """A sub-call phase (``find:code``, ``verify:3``) as a filesystem-safe path segment
    (``find-code``, ``verify-3``) — colon-free like ``safe_task_dirname``, since a colon is
    hostile on Windows/SMB and awkward in a shell path. A deliberate, documented deviation
    from the design doc's literal ``<stage>-attempt<N>.<phase>.stream.jsonl`` spelling: the
    phase segment is the SANITIZED phase, and the unsanitized phase rides the ledger row /
    ``SubCall.phase`` where it is data, not a path."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", phase).strip("-") or "sub"


def stream_basename(
    stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    """The per-(stage, attempt[, sub-call phase][, schema-retry sub-call]) stem shared by the
    ``.stream.jsonl`` and ``.stderr.log`` files.

    A stage ATTEMPT gets its own stem (``<stage>-attempt<N>``) so a retry never clobbers a
    prior attempt's evidence. A multi-agent REVIEW panel's sub-call (#73) inserts its
    sanitized ``phase`` next (``<stage>-attempt<N>.<phase>``) so each finder/verifier of one
    dispatch owns its own stream. Within one (sub-)call, a schema-validate-and-retry sub-call
    (#70) gets a further ``.retry<K>`` suffix (K>=1) so each corrective sub-call's stream
    survives too — the first call (K=0) keeps the bare name, so the 99%-no-retry case is
    unchanged, and a phase-less (single-call) dispatch names files exactly as it always did."""
    stem = f"{stage_value}-attempt{attempt}"
    if phase:
        stem = f"{stem}.{safe_phase(phase)}"
    return f"{stem}.retry{retry}" if retry else stem


def stream_filename(
    stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    return stream_basename(stage_value, attempt, retry, phase=phase) + ".stream.jsonl"


def stderr_filename(
    stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    return stream_basename(stage_value, attempt, retry, phase=phase) + ".stderr.log"


def prompt_filename(stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None) -> str:
    """The dispatched prompt's file for one (stage, attempt) — ``<stem>.prompt.txt`` (#314).

    Shares ``stream_basename`` with the ``.stream.jsonl``/``.stderr.log`` tees deliberately:
    a dispatch's prompt then sits next to that same call's raw provider stream under one
    stem, so "what did this call cost, and what was it sent?" is one directory listing
    rather than a join. Named by (stage, attempt) rather than by the per-stage record's
    ``NN-`` sequence because that counter is only claimed under the record-time lock — a
    resume that supersedes a lease (#142) dispatches twice with no record in between, and
    both dispatches would predict the same ``NN``."""
    return stream_basename(stage_value, attempt, retry, phase=phase) + ".prompt.txt"


def stages_dir(run_root: str | Path, task_id: str) -> Path:
    """``<run_root>/stages/<safe-task>/`` — the per-task log dir the StatusStore and the
    teeing adapter both write into (one sanitization, one location)."""
    return Path(run_root) / "stages" / safe_task_dirname(task_id)


def stream_relpath(
    task_id: str, stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    """Stream path RELATIVE to the run root (portable if the run dir moves)."""
    return str(
        Path("stages") / safe_task_dirname(task_id)
        / stream_filename(stage_value, attempt, retry, phase=phase)
    )


def stderr_relpath(
    task_id: str, stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    return str(
        Path("stages") / safe_task_dirname(task_id)
        / stderr_filename(stage_value, attempt, retry, phase=phase)
    )


def prompt_relpath(
    task_id: str, stage_value: str, attempt: int, retry: int = 0, *, phase: str | None = None
) -> str:
    """Prompt path RELATIVE to the run root (portable if the run dir moves) — the spelling
    that rides ``stage_dispatched`` so a reader never has to reconstruct the convention."""
    return str(
        Path("stages") / safe_task_dirname(task_id)
        / prompt_filename(stage_value, attempt, retry, phase=phase)
    )


def _attempt_of(path: Path) -> tuple[int, int]:
    """The ``(attempt, retry)`` encoded in ``<stage>-attemptN[.retryK].stream.jsonl`` — retry
    defaults to 0 for a base (non-retry) file — so the newest sub-call sorts last: a higher
    attempt wins, and within one attempt a higher schema-retry suffix wins (#70). ``(-1, -1)``
    if the attempt is unparseable."""
    name = path.name
    try:
        attempt = int(name.split("-attempt", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return (-1, -1)
    retry = 0
    if ".retry" in name:
        try:
            retry = int(name.split(".retry", 1)[1].split(".", 1)[0])
        except (IndexError, ValueError):
            retry = 0
    return (attempt, retry)


def _stream_sort_key(path: Path) -> tuple[int, int, float]:
    """``(attempt, retry, mtime)`` — the ordering ``find_current_stream`` follows.

    The mtime is the TIEBREAK within one ``(attempt, retry)``: a multi-agent REVIEW panel
    (#73) writes one stream per sub-call (``<stage>-attempt<N>.<phase>...``), so several
    files now share a key that used to be unique per stage-attempt. Ordering the tie by
    mtime makes ``probe_current_stream`` / ``orchestrator tail`` follow the sub-call that is
    actually live rather than whichever name the glob happened to yield. An unstattable file
    (raced deletion) sorts oldest instead of raising."""
    attempt, retry = _attempt_of(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:  # pragma: no cover - defensive (raced deletion)
        mtime = 0.0
    return (attempt, retry, mtime)


def find_current_stream(
    run_root: str | Path, task_id: str, stage_value: str | None = None
) -> Path | None:
    """The stream file to probe/tail for a task: the highest-(attempt, schema-retry, mtime)
    file for ``stage_value`` when given — so a live schema-retry sub-call's ``.retry<K>``
    stream (#70) is preferred over the superseded base file, and within one attempt the
    most-recently-written review-panel sub-call stream (#73) wins — else the
    most-recently-modified ``*.stream.jsonl`` under the task's stages dir (the "current, or
    last" stream). ``None`` when the dir/file doesn't exist — i.e. no provider stream (an
    interactive/ENGINE lane, or nothing dispatched yet)."""
    d = stages_dir(run_root, task_id)
    if not d.is_dir():
        return None
    if stage_value is not None:
        # The glob also matches the ``.retry<K>`` and ``.<phase>`` sub-call files;
        # ``_stream_sort_key`` orders them after the base file (and breaks a phase tie by
        # mtime) so the newest live sub-call sorts last.
        cands = sorted(d.glob(f"{stage_value}-attempt*.stream.jsonl"), key=_stream_sort_key)
        return cands[-1] if cands else None
    cands = list(d.glob("*.stream.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


# --- final-text extraction (#93) ---------------------------------------------------------
# Since #66 the headless transports stream `--output-format stream-json` / codex `--json`, and
# the raw JSONL event stream was landing in `RawResult.raw_output` — which is what the human-
# facing per-stage .md Commentary, the failure-learning output tail, and schema-retry prompts
# all show. The full stream is already teed to `<stage>-attempt<N>.stream.jsonl`, so the stream
# in raw_output is pure noise. These helpers recover the model's READABLE final text from a
# stream so the transport can put THAT on raw_output, and the renderer can retro-extract it from
# any old-style stream payload. Owned here (orchestrator layer) so both `adapters.execution`
# and `orchestrator.render` reach it without render importing adapters. Deterministic parse;
# a partial/non-JSON line is skipped, mirroring the rest of this module.

# Last-resort tail when a stream carries no readable final text at all (a crashed/textless
# call). Bounded so raw_output can never balloon back to the whole stream.
_STREAM_TAIL_CHARS = 2000


def _iter_stream_objects(text: str) -> Iterator[dict]:
    """Yield each complete, parseable JSON object line of a provider stream (skipping banners
    and partial trailing writes) — the shared spine of the stream-shape helpers."""
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def _assistant_text(ev: dict) -> str | None:
    """The concatenated ``text`` blocks of a claude ``assistant`` event, or None if it has none
    (a tool-only turn)."""
    content = (ev.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    parts = [
        c["text"] for c in content
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
    ]
    joined = "\n".join(p for p in parts if p.strip())
    return joined if joined.strip() else None


def claude_final_text(stdout: str) -> str | None:
    """The model's readable final text from a claude ``stream-json`` stdout: the final
    ``result`` event's ``result`` prose, else the LAST assistant turn's text block(s). None
    when the stream carries neither (a crashed/textless call), leaving the caller to fall back
    to a bounded stream tail."""
    result_text: str | None = None
    assistant_text: str | None = None
    for ev in _iter_stream_objects(stdout):
        t = ev.get("type")
        if t == "result":
            r = ev.get("result")
            if isinstance(r, str) and r.strip():
                result_text = r
        elif t == "assistant":
            blocks = _assistant_text(ev)
            if blocks is not None:
                assistant_text = blocks
    return result_text or assistant_text


def codex_final_text(stdout: str) -> str | None:
    """The model's readable final text from a codex ``--json`` stdout: the LAST ``agent_message``
    item's text. Covers the newer ``item.completed``→``item`` shape and the older ``msg``-wrapped /
    top-level shapes; None when the stream carries no agent message (leaving a fallback to the
    prose ``--output-last-message`` content or a bounded tail)."""
    text: str | None = None
    for ev in _iter_stream_objects(stdout):
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else None
        for src in (item, msg, ev):
            if not isinstance(src, dict):
                continue
            if src.get("type") == "agent_message" or src.get("item_type") == "agent_message":
                for key in ("text", "message", "content"):
                    v = src.get(key)
                    if isinstance(v, str) and v.strip():
                        text = v
    return text


def stream_tail_note(stdout: str, relpath: str | None, *, max_chars: int = _STREAM_TAIL_CHARS) -> str:
    """The LAST-RESORT ``raw_output`` for a call that produced no readable final text (a hard
    failure / textless crash): a one-line note pointing at the retained full stream, then a
    bounded tail of the stream. Evidence lives in the stream file; this is only a breadcrumb."""
    tail = (stdout or "").strip()
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    pointer = relpath or "(the retained stream file)"
    note = f"[no final text — stream tail; full stream: {pointer}]"
    return f"{note}\n{tail}" if tail else note


def looks_like_event_stream(text: str, *, min_lines: int = 3) -> bool:
    """True when ``text`` looks like a raw provider JSONL event stream rather than model prose:
    at least ``min_lines`` non-empty lines with a MAJORITY parsing as JSON objects that carry a
    ``type`` key. The renderer's belt-and-suspenders guard against dumping a stream into the
    human-facing Commentary — including on a replay of an old-style (pre-#93) payload (#93)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < min_lines:
        return False
    typed = 0
    for ln in lines:
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and "type" in ev:
            typed += 1
    return typed * 2 > len(lines)


def readable_text_from_stream(text: str) -> str | None:
    """Extract the model's readable final text from a raw event-stream payload of unknown lane
    (claude or codex) — the renderer's extractor once ``looks_like_event_stream`` has flagged a
    payload. None when neither lane's shape yields prose."""
    return claude_final_text(text) or codex_final_text(text)


# --- activity extraction (parse, don't interpret) ----------------------------------------

# Bound every piece of extracted text so a snapshot can never balloon (a model can emit a
# multi-KB command / prompt as a tool arg).
_MAX_TOOL = 60
_MAX_DETAIL = 200

# Argument keys that make the most useful "what is it doing" detail, in priority order.
# Covers the claude tool_use inputs (Bash.command, Read.file_path, Grep.pattern, …) and the
# codex command-ish event fields.
_ARG_KEYS = (
    "command", "file_path", "path", "pattern", "cmd", "url", "query", "description", "prompt",
)


def _pick_detail(d: dict) -> str | None:
    if not isinstance(d, dict):
        return None
    for k in _ARG_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:_MAX_DETAIL]
        if isinstance(v, list) and v:
            return " ".join(str(x) for x in v)[:_MAX_DETAIL]
    return None


def _activity_from_event(ev: object) -> dict | None:
    """``{"tool": name, "detail": key-arg|None}`` for an event that represents the model
    doing something (a tool_use / command), else ``None``. Best-effort across the claude
    stream-json shape and the codex ``--json`` shapes; an unrecognized event just yields
    ``None`` (the probe keeps the last recognized activity, and events_seen still counts it),
    which is the documented codex degraded mode."""
    if not isinstance(ev, dict):
        return None
    # claude stream-json: an assistant turn may carry one or more tool_use blocks.
    if ev.get("type") == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        last_tool = None
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                last_tool = item
        if last_tool is not None:
            name = str(last_tool.get("name") or "tool")[:_MAX_TOOL]
            return {"tool": name, "detail": _pick_detail(last_tool.get("input") or {})}
        return None  # text-only turn — not a tool activity
    # codex (older): a `msg`-wrapped event carrying a typed sub-event.
    msg = ev.get("msg")
    if isinstance(msg, dict) and msg.get("type"):
        return {"tool": str(msg["type"])[:_MAX_TOOL], "detail": _pick_detail(msg)}
    # codex (newer `item.*` events): {"type":"item.completed","item":{"type":"command_execution",...}}
    item = ev.get("item")
    if isinstance(item, dict) and (item.get("type") or ev.get("type")):
        return {
            "tool": str(item.get("type") or ev.get("type"))[:_MAX_TOOL],
            "detail": _pick_detail(item),
        }
    return None


# --- the snapshot ------------------------------------------------------------------------


def probe_stream(
    path: str | Path, *, tail_lines: int = 5, max_line_chars: int = 500
) -> dict | None:
    """A cheap live snapshot of a (possibly still-being-written) stream file, or ``None`` if
    the file is missing.

    Returns ``{events_seen, last_event_at, current_activity, recent_tail}`` where:
      - ``events_seen``   — count of complete, parseable JSON event lines,
      - ``last_event_at`` — the file mtime (epoch seconds; a good "last grew at" proxy),
      - ``current_activity`` — ``{"tool", "detail"}`` of the latest recognized tool_use /
        command, or ``None`` (no activity recognized yet / codex degraded mode),
      - ``recent_tail``   — the last ``tail_lines`` raw lines, each bounded to
        ``max_line_chars`` (``[]`` when ``tail_lines`` is 0).

    A partial/truncated trailing line (an in-progress write) is tolerated: it simply fails
    ``json.loads`` and is skipped, so a probe mid-write never raises and never miscounts a
    half-written line as an event."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        mtime: float | None = p.stat().st_mtime
    except OSError:  # pragma: no cover - defensive (raced deletion / permissions)
        return None
    lines = text.splitlines()
    events_seen = 0
    activity: dict | None = None
    for line in lines:
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except json.JSONDecodeError:
            continue  # partial trailing write, or a non-JSON banner line — skip
        events_seen += 1
        found = _activity_from_event(ev)
        if found is not None:
            activity = found
    tail = [ln[:max_line_chars] for ln in lines[-tail_lines:]] if tail_lines else []
    return {
        "events_seen": events_seen,
        "last_event_at": mtime,
        "current_activity": activity,
        "recent_tail": tail,
    }


def probe_current_stream(
    run_root: str | Path,
    task_id: str,
    stage_value: str | None = None,
    *,
    tail_lines: int = 5,
    max_line_chars: int = 500,
) -> dict | None:
    """``probe_stream`` on the task's current/last stream file, or ``None`` when there is no
    provider stream to probe (interactive/ENGINE lane, or nothing dispatched yet)."""
    p = find_current_stream(run_root, task_id, stage_value)
    if p is None:
        return None
    return probe_stream(p, tail_lines=tail_lines, max_line_chars=max_line_chars)


# --- the tail CLI's read/follow helpers --------------------------------------------------


def read_tail(
    path: str | Path, *, lines: int = 20, max_line_chars: int = 2000
) -> list[str] | None:
    """The last ``lines`` raw lines of a stream file (each bounded), or ``None`` if missing.
    Higher caps than ``probe_stream``'s lean tail — this feeds the human ``tail`` command."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - defensive
        return None
    return [ln[:max_line_chars] for ln in text.splitlines()[-lines:]]


def follow_stream(
    path: str | Path,
    *,
    emit: Callable[[str], None],
    sleeper: Callable[[float], None],
    lines: int = 20,
    max_line_chars: int = 2000,
    poll_interval: float = 2.0,
    max_polls: int | None = None,
) -> None:
    """Print the current tail, then poll for growth and print each newly-appended line.

    ``sleeper`` is injected (``time.sleep`` in production, a stub in tests) so the follow loop
    is drivable without real sleeping — the same pattern the alerting ``watch`` loop uses.
    ``max_polls`` bounds the loop for tests (``None`` = run until interrupted). Re-reads the
    whole file each poll: simple and correct for a bounded, human-facing tail."""
    p = Path(path)

    def _all() -> list[str]:
        if not p.exists():
            return []
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - defensive
            return []

    current = _all()
    for ln in current[max(0, len(current) - lines):]:
        emit(ln[:max_line_chars])
    seen = len(current)
    polls = 0
    while max_polls is None or polls < max_polls:
        sleeper(poll_interval)
        polls += 1
        current = _all()
        if len(current) > seen:
            for ln in current[seen:]:
                emit(ln[:max_line_chars])
            seen = len(current)
