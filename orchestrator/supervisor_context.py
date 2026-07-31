"""Claude Code supervisor-context sensing for the interactive lane (#259).

Claude Code exposes its context-window counters only to the configured ``statusLine``
command.  ``capture_statusline_context`` is the narrow bridge: the existing
``orchestrator statusline`` command records that input in a small, cwd-keyed cache, and
the interactive ``next`` command reads the fresh snapshot before it commits a dispatch.
No prompt or model output is stored here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_MAX_AGE_S = 30.0
DEFAULT_MIN_REMAINING_PCT = 20.0
PROMPT_BYTES_PER_TOKEN = 3  # deliberately conservative for mixed prose/JSON prompts


@dataclass(frozen=True)
class SupervisorContext:
    """One Claude Code context observation used to gate an interactive dispatch.

    ``available=False`` represents a missing, malformed, or stale observation and is
    deliberately fail-closed: callers must park rather than risk leasing work to a
    supervisor that may run out of context mid-stage.
    """

    available: bool
    observed_at: float | None = None
    session_id: str | None = None
    cwd: str | None = None
    context_window_size: int | None = None
    used_percentage: float | None = None
    remaining_percentage: float | None = None
    reason: str | None = None

    @property
    def remaining_tokens(self) -> int | None:
        """Return the floored remaining-window estimate, if the snapshot is complete."""
        if self.context_window_size is None or self.remaining_percentage is None:
            return None
        return max(0, math.floor(self.context_window_size * self.remaining_percentage / 100))

    def projected(self, prompt: str, *, min_remaining_pct: float) -> dict:
        """Calculate whether ``prompt`` fits while preserving the configured reserve.

        The estimate conservatively converts UTF-8 prompt bytes to tokens, so the
        returned ``should_park`` is suitable for a pre-lease safety gate. ``ValueError``
        is raised when ``min_remaining_pct`` is not a finite percentage in ``[0, 100]``.
        """
        if not math.isfinite(min_remaining_pct) or not 0 <= min_remaining_pct <= 100:
            raise ValueError("min_remaining_pct must be a finite percentage from 0 to 100")
        prompt_chars = len(prompt)
        prompt_bytes = len(prompt.encode("utf-8"))
        projected_tokens = math.ceil(prompt_bytes / PROMPT_BYTES_PER_TOKEN)
        window = self.context_window_size
        remaining = self.remaining_tokens
        reserve = math.ceil(window * min_remaining_pct / 100) if window is not None else None
        required = reserve + projected_tokens if reserve is not None else None
        should_park = (
            not self.available
            or remaining is None
            or required is None
            or remaining < required
        )
        return {
            "available": self.available,
            "session_id": self.session_id,
            "observed_at": self.observed_at,
            "context_window_size": window,
            "used_percentage": self.used_percentage,
            "remaining_percentage": self.remaining_percentage,
            "remaining_tokens": remaining,
            "prompt_chars": prompt_chars,
            "prompt_bytes": prompt_bytes,
            "projected_prompt_tokens": projected_tokens,
            "min_remaining_percentage": min_remaining_pct,
            "reserve_tokens": reserve,
            "required_remaining_tokens": required,
            "should_park": should_park,
            "sensor_reason": self.reason,
        }


def _cache_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    configured = os.environ.get("ORCHESTRATOR_SUPERVISOR_CONTEXT_DIR")
    return (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "orchestrator-supervisor-context"
    )


def _cwd_key(cwd: str | Path) -> str:
    resolved = str(Path(cwd).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _snapshot_from_cache(data: object) -> SupervisorContext | None:
    """Validate one persisted sensor row before constructing its dataclass.

    ``SupervisorContext`` is intentionally a plain dataclass, so its constructor does
    not enforce runtime field types. Cache data crosses a process boundary and must be
    checked explicitly: every value used by freshness or prompt projection is finite and
    correctly typed before callers can perform arithmetic with it.
    """
    if not isinstance(data, dict) or data.get("available") is not True:
        return None
    allowed = {
        "available",
        "observed_at",
        "session_id",
        "cwd",
        "context_window_size",
        "used_percentage",
        "remaining_percentage",
        "reason",
    }
    if not set(data).issubset(allowed):
        return None

    observed_at = _finite_number(data.get("observed_at"))
    size = data.get("context_window_size")
    used = _finite_number(data.get("used_percentage"))
    remaining = _finite_number(data.get("remaining_percentage"))
    session_id = data.get("session_id")
    cwd = data.get("cwd")
    reason = data.get("reason")
    if (
        observed_at is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or used is None
        or not 0 <= used <= 100
        or remaining is None
        or not 0 <= remaining <= 100
        or session_id is not None
        and not isinstance(session_id, str)
        or not isinstance(cwd, str)
        or not cwd
        or reason is not None
        and not isinstance(reason, str)
    ):
        return None
    return SupervisorContext(
        available=True,
        observed_at=observed_at,
        session_id=session_id,
        cwd=cwd,
        context_window_size=size,
        used_percentage=used,
        remaining_percentage=remaining,
        reason=reason,
    )


def capture_statusline_context(
    payload: dict, *, cache_root: Path | None = None, now: float | None = None
) -> Path | None:
    """Persist a valid Claude Code status-line context snapshot atomically.

    ``payload`` must provide a working directory and a complete ``context_window``
    observation. Invalid or incomplete status-line data returns ``None`` without
    creating a cache entry; a successful write returns the cwd-keyed cache path. The
    cache deliberately stores counters only, never prompts or model output.
    """
    context = payload.get("context_window")
    if not isinstance(context, dict):
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        workspace = payload.get("workspace")
        cwd = workspace.get("current_dir") if isinstance(workspace, dict) else None
    if not isinstance(cwd, str) or not cwd:
        return None

    size_raw = context.get("context_window_size")
    size = int(size_raw) if isinstance(size_raw, int) and not isinstance(size_raw, bool) else None
    used = _finite_number(context.get("used_percentage"))
    remaining = _finite_number(context.get("remaining_percentage"))
    if remaining is None and used is not None:
        remaining = max(0.0, 100.0 - used)
    if used is None and remaining is not None:
        used = max(0.0, 100.0 - remaining)
    if (
        size is None
        or size <= 0
        or remaining is None
        or not 0 <= remaining <= 100
        or used is not None and not 0 <= used <= 100
    ):
        return None

    root = _cache_root(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{_cwd_key(cwd)}.json"
    row = SupervisorContext(
        available=True,
        observed_at=now if now is not None else time.time(),
        session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
        cwd=str(Path(cwd).resolve()),
        context_window_size=size,
        used_percentage=used,
        remaining_percentage=remaining,
    )
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(row), sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_supervisor_context(
    cwd: str | Path,
    *,
    cache_root: Path | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: float | None = None,
) -> SupervisorContext:
    """Read ``cwd``'s snapshot, failing closed when it is unavailable or too old.

    ``max_age_s`` bounds how long a status-line observation may authorize a dispatch.
    Missing, unreadable, syntactically corrupt, schema-invalid, and stale cache data
    produces an unavailable snapshot with a reason instead of raising. Cached counters
    must have finite numeric values and valid ranges before they can authorize a dispatch,
    allowing callers to fail closed at a stage boundary.
    """
    target = _cache_root(cache_root) / f"{_cwd_key(cwd)}.json"
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return SupervisorContext(available=False, reason="supervisor context sensor unavailable")
    snapshot = _snapshot_from_cache(data)
    if snapshot is None:
        return SupervisorContext(available=False, reason="supervisor context sensor unavailable")
    current = now if now is not None else time.time()
    if snapshot.observed_at is None or current - snapshot.observed_at > max_age_s:
        return SupervisorContext(
            available=False,
            observed_at=snapshot.observed_at,
            session_id=snapshot.session_id,
            cwd=snapshot.cwd,
            context_window_size=snapshot.context_window_size,
            used_percentage=snapshot.used_percentage,
            remaining_percentage=snapshot.remaining_percentage,
            reason="supervisor context sensor is stale",
        )
    return snapshot
