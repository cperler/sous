"""Usage probe — the capacity policy's missing sensor (ports ``fetch-usage.sh``).

Reads the account's 5h/7d utilization from the Anthropic OAuth usage endpoint
(token from the macOS keychain entry Claude Code itself maintains) so the
``--util`` gates actually bind instead of running on a hand-typed default.
Engine-external by design: the CLI (``orchestrator util`` / ``--util auto``) and
the supervisor skills call this and pass the number in — the Engine class stays
free of network I/O.

Everything is best-effort: no keychain, no token, no network, or a malformed
payload all yield ``None`` — the caller falls back to util 0.0 (gates open),
which is exactly the pre-probe behavior, never an error.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_USAGE_URL = "https://api.anthropic.com/oauth/usage"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CACHE = Path.home() / ".cache" / "orchestrator" / "usage.json"
CACHE_TTL_S = 120  # the old fetch-usage cadence was minutes; 2 min keeps polls cheap


@dataclass(frozen=True)
class Usage:
    five_hour_pct: float
    seven_day_pct: float
    five_hour_resets_at: str = ""
    seven_day_resets_at: str = ""


def _keychain_token() -> str | None:
    """The Claude Code OAuth access token from the macOS keychain (best-effort)."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout).get("claudeAiOauth", {}).get("accessToken") or None
    except (ValueError, AttributeError):
        return None


def _http_get(url: str, headers: dict[str, str]) -> str | None:
    try:
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - fixed https URL
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a probe must never raise into the caller
        return None


def fetch_usage(*, token_provider=_keychain_token, http_get=_http_get) -> Usage | None:
    """One live probe of the usage endpoint. None on any failure."""
    token = token_provider()
    if not token:
        return None
    body = http_get(
        _USAGE_URL,
        {
            "accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "authorization": f"Bearer {token}",
            "user-agent": "orchestrator-usage-probe",
        },
    )
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    five_h = data.get("five_hour") or {}
    seven_d = data.get("seven_day") or {}
    if five_h.get("utilization") is None or seven_d.get("utilization") is None:
        return None
    return Usage(
        five_hour_pct=float(five_h["utilization"]),
        seven_day_pct=float(seven_d["utilization"]),
        five_hour_resets_at=str(five_h.get("resets_at") or ""),
        seven_day_resets_at=str(seven_d.get("resets_at") or ""),
    )


def read_usage(
    cache_path: Path = DEFAULT_CACHE,
    ttl_s: int = CACHE_TTL_S,
    *,
    fetch=fetch_usage,
    now=time.time,
) -> Usage | None:
    """Cached probe: serve a fresh-enough cache file, else fetch and rewrite it.
    The cache lives under the user's cache dir (not /tmp — the old script's cache
    was world-shared and swept on reboot)."""
    try:
        if cache_path.exists() and now() - cache_path.stat().st_mtime < ttl_s:
            return Usage(**json.loads(cache_path.read_text()))
    except Exception:  # noqa: BLE001 - a corrupt cache falls through to a live fetch
        pass
    usage = fetch()
    if usage is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(asdict(usage)))
        except OSError:
            pass  # cache write is a nicety, never a failure
    return usage


def _reset_countdown(resets_at: str, now: datetime) -> str:
    """Human 'resets in Xh Ym' from an ISO timestamp, or '' if absent/past/malformed.
    Best-effort like the rest of the probe — a bad string never raises into a caller."""
    if not resets_at:
        return ""
    try:
        target = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    remaining = int((target - now).total_seconds())
    if remaining <= 0:
        return ""
    days, rem = divmod(remaining, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def format_statusline(usage: Usage | None, *, now: datetime | None = None) -> str:
    """One compact status-bar line from a probe: 5h/7d utilization + reset countdown.
    Empty string when the probe is unavailable so the status bar shows nothing rather
    than an error (ports the old ``statusline-command.sh``). Plain text, not JSON —
    Claude Code's ``statusLine`` consumes a raw line."""
    if usage is None:
        return ""
    now = now or datetime.now(UTC)

    def _seg(label: str, pct: float, resets_at: str) -> str:
        countdown = _reset_countdown(resets_at, now)
        tail = f" (resets {countdown})" if countdown else ""
        return f"{label} {pct:.0f}%{tail}"

    five = _seg("5h", usage.five_hour_pct, usage.five_hour_resets_at)
    seven = _seg("7d", usage.seven_day_pct, usage.seven_day_resets_at)
    return f"⧗ {five} · {seven}"


def watch_statusline(
    *,
    emit: Callable[[str], None],
    sleeper: Callable[[float], None],
    clear: Callable[[], None] | None = None,
    interval: float = 30,
    max_iters: int | None = None,
    reader: Callable[..., Usage | None] | None = None,
) -> None:
    """Clear-screen + reprint the statusline every ``interval`` seconds until interrupted (#79).

    Intended for supervising a batch run from a standalone terminal: a 30-60s poll amortizes over
    the 2-min usage cache (``read_usage`` serves the cached value between probes) while keeping the
    display fresh.

    Mirrors ``dashboard.render_watch``'s injected-sleeper pattern so every dependency is
    drivable in tests without real I/O:

    - ``emit`` — output sink; ``print`` in production, a list-appending stub in tests.
    - ``sleeper`` — ``time.sleep`` in production; a stub (or one that raises ``KeyboardInterrupt``
      to stop the loop) in tests.
    - ``reader`` — resolved at call time (not bound as a default) so the CLI path is
      monkeypatchable; defaults to ``read_usage``.
    - ``clear`` — defaults to emitting an ANSI full-clear escape; supply a no-op in tests.
    - ``max_iters`` — bounds the loop for tests; ``None`` means run until interrupted.

    A ``KeyboardInterrupt`` from any injected callable (Ctrl-C in production, or a test sleeper
    that raises to stop) ends the loop cleanly — the exception is swallowed, not propagated. On a
    probe miss the line is a quiet placeholder rather than a blank screen, so the terminal still
    shows the poll is alive."""
    if reader is None:
        reader = read_usage
    if clear is None:
        clear = lambda: emit("\x1b[2J\x1b[H")  # noqa: E731 - tiny ANSI clear
    iters = 0
    try:
        while max_iters is None or iters < max_iters:
            clear()
            emit(format_statusline(reader()) or "⧗ usage unavailable")
            iters += 1
            if max_iters is not None and iters >= max_iters:
                break
            sleeper(interval)
    except KeyboardInterrupt:
        pass


def resolve_util(spec: str | float | None, *, reader=read_usage) -> tuple[float, dict]:
    """CLI ``--util`` resolution: a number passes through; ``auto`` (or empty) probes.
    Returns ``(util_pct, meta)`` — meta says where the number came from, so a probe
    miss is visible ('gates open at 0.0' is a stated fact, not a silent lie)."""
    if spec in (None, "", "auto"):
        usage = reader()
        if usage is None:
            return 0.0, {"util_source": "auto", "probe": "unavailable (gates open at 0.0)"}
        return usage.five_hour_pct, {"util_source": "auto", "probe": asdict(usage)}
    return float(spec), {"util_source": "explicit"}
