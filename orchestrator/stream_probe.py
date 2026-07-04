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
from collections.abc import Callable
from pathlib import Path

from .status_store import safe_task_dirname

# --- stream-file naming (single source of truth) -----------------------------------------


def stream_basename(stage_value: str, attempt: int) -> str:
    """The per-(stage, attempt) stem shared by the ``.stream.jsonl`` and ``.stderr.log``
    files. A retry gets its own stem so a prior attempt's evidence is never clobbered."""
    return f"{stage_value}-attempt{attempt}"


def stream_filename(stage_value: str, attempt: int) -> str:
    return stream_basename(stage_value, attempt) + ".stream.jsonl"


def stderr_filename(stage_value: str, attempt: int) -> str:
    return stream_basename(stage_value, attempt) + ".stderr.log"


def stages_dir(run_root: str | Path, task_id: str) -> Path:
    """``<run_root>/stages/<safe-task>/`` — the per-task log dir the StatusStore and the
    teeing adapter both write into (one sanitization, one location)."""
    return Path(run_root) / "stages" / safe_task_dirname(task_id)


def stream_relpath(task_id: str, stage_value: str, attempt: int) -> str:
    """Stream path RELATIVE to the run root (portable if the run dir moves)."""
    return str(Path("stages") / safe_task_dirname(task_id) / stream_filename(stage_value, attempt))


def stderr_relpath(task_id: str, stage_value: str, attempt: int) -> str:
    return str(Path("stages") / safe_task_dirname(task_id) / stderr_filename(stage_value, attempt))


def _attempt_of(path: Path) -> int:
    """The attempt number encoded in ``<stage>-attemptN.stream.jsonl`` (-1 if unparseable),
    so the newest attempt sorts last."""
    try:
        return int(path.name.split("-attempt", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return -1


def find_current_stream(
    run_root: str | Path, task_id: str, stage_value: str | None = None
) -> Path | None:
    """The stream file to probe/tail for a task: the highest-attempt file for ``stage_value``
    when given, else the most-recently-modified ``*.stream.jsonl`` under the task's stages dir
    (the "current, or last" stream). ``None`` when the dir/file doesn't exist — i.e. no
    provider stream (an interactive/ENGINE lane, or nothing dispatched yet)."""
    d = stages_dir(run_root, task_id)
    if not d.is_dir():
        return None
    if stage_value is not None:
        cands = sorted(d.glob(f"{stage_value}-attempt*.stream.jsonl"), key=_attempt_of)
        return cands[-1] if cands else None
    cands = list(d.glob("*.stream.jsonl"))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


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
