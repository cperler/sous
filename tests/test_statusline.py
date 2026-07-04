"""Statusline formatter (#61): a raw utilization line for the Claude Code status bar,
built over the same usage cache the `util` sensor feeds. Best-effort — a probe miss or a
malformed reset timestamp yields a quiet line, never an error."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator import usage_probe
from orchestrator.cli import main
from orchestrator.usage_probe import Usage, format_statusline, watch_statusline

_NOW = datetime(2026, 7, 3, 15, 0, 0, tzinfo=UTC)


def test_format_statusline_probe_hit_shows_both_pcts_and_countdowns() -> None:
    usage = Usage(
        five_hour_pct=87.4,
        seven_day_pct=41.0,
        five_hour_resets_at="2026-07-03T18:00:00+00:00",  # +3h
        seven_day_resets_at="2026-07-08T00:00:00+00:00",  # +4d9h
    )
    line = format_statusline(usage, now=_NOW)
    # Exactly +3h suppresses the zero-minute suffix (#77): "3h", not "3h0m".
    assert line == "⧗ 5h 87% (resets 3h) · 7d 41% (resets 4d9h)"


def test_reset_countdown_drops_zero_components() -> None:
    """#77: at hour granularity a zero-minute reset renders "3h" (not "3h0m"), and a
    nonzero-minute reset still renders "3h12m". At day granularity a zero-hour reset
    renders "4d" (not "4d0h"); minutes are never shown at day granularity by design."""
    from orchestrator.usage_probe import _reset_countdown

    now = datetime(2026, 7, 3, 15, 0, 0, tzinfo=UTC)
    # exactly 3 hours -> "3h"
    assert _reset_countdown("2026-07-03T18:00:00+00:00", now) == "3h"
    # 3h12m -> unchanged
    assert _reset_countdown("2026-07-03T18:12:00+00:00", now) == "3h12m"
    # exactly 4 days (zero hours) -> "4d", not "4d0h"
    assert _reset_countdown("2026-07-07T15:00:00+00:00", now) == "4d"
    # 4 days 9 hours -> "4d9h" (minutes never shown at day granularity)
    assert _reset_countdown("2026-07-08T00:00:00+00:00", now) == "4d9h"


def test_format_statusline_probe_miss_is_quiet() -> None:
    assert format_statusline(None) == ""


def test_format_statusline_handles_past_and_malformed_resets() -> None:
    usage = Usage(
        five_hour_pct=12.0,
        seven_day_pct=5.0,
        five_hour_resets_at="2026-07-03T09:00:00+00:00",  # already past -> no countdown
        seven_day_resets_at="not-a-timestamp",             # malformed -> no countdown
    )
    line = format_statusline(usage, now=_NOW)
    assert line == "⧗ 5h 12% · 7d 5%"


def test_format_statusline_accepts_z_suffix_timestamp() -> None:
    usage = Usage(10.0, 20.0, "2026-07-03T16:30:00Z", "")
    line = format_statusline(usage, now=_NOW)
    assert line == "⧗ 5h 10% (resets 1h30m) · 7d 20%"


def test_cli_statusline_prints_line_on_probe_hit(capsys, monkeypatch) -> None:
    usage = Usage(50.0, 25.0, "", "")
    monkeypatch.setattr(usage_probe, "read_usage", lambda *a, **k: usage)
    assert main(["statusline"]) == 0
    assert capsys.readouterr().out.strip() == "⧗ 5h 50% · 7d 25%"


def test_cli_statusline_is_quiet_and_zero_on_probe_miss(capsys, monkeypatch) -> None:
    monkeypatch.setattr(usage_probe, "read_usage", lambda *a, **k: None)
    assert main(["statusline"]) == 0
    assert capsys.readouterr().out.strip() == ""


# --- #79: --watch refresh loop -------------------------------------------------------------


def test_watch_statusline_reprints_each_iter_and_sleeps_between() -> None:
    """Clears + reprints once per iteration, sleeping ``interval`` between (but not after the
    last), so a max_iters=3 run yields 3 renders and 2 sleeps."""
    usage = Usage(50.0, 25.0, "", "")
    emitted: list[str] = []
    sleeps: list[float] = []
    watch_statusline(
        emit=emitted.append,
        sleeper=sleeps.append,
        clear=lambda: emitted.append("<clear>"),
        interval=45,
        max_iters=3,
        reader=lambda *a, **k: usage,
    )
    assert emitted == ["<clear>", "⧗ 5h 50% · 7d 25%"] * 3
    assert sleeps == [45, 45]  # no trailing sleep after the final render


def test_watch_statusline_shows_placeholder_on_probe_miss() -> None:
    emitted: list[str] = []
    watch_statusline(
        emit=emitted.append,
        sleeper=lambda _s: None,
        clear=lambda: None,
        max_iters=1,
        reader=lambda *a, **k: None,
    )
    assert emitted == ["⧗ usage unavailable"]


def test_watch_statusline_stops_cleanly_on_keyboardinterrupt() -> None:
    usage = Usage(10.0, 20.0, "", "")
    emitted: list[str] = []

    def _boom(_s: float) -> None:
        raise KeyboardInterrupt

    # Ctrl-C mid-sleep ends the loop without propagating; the first render still lands.
    watch_statusline(
        emit=emitted.append,
        sleeper=_boom,
        clear=lambda: None,
        reader=lambda *a, **k: usage,
    )
    assert emitted == ["⧗ 5h 10% · 7d 20%"]


def test_cli_statusline_watch_loops_via_injected_sleep(capsys, monkeypatch) -> None:
    """The CLI wiring drives the loop with real print/time.sleep; a sleeper that raises
    KeyboardInterrupt after the first render exercises the terminal path and exits 0."""
    usage = Usage(50.0, 25.0, "", "")
    monkeypatch.setattr(usage_probe, "read_usage", lambda *a, **k: usage)

    def _stop(_s: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(usage_probe.time, "sleep", _stop)
    assert main(["statusline", "--watch", "--interval", "5"]) == 0
    assert "⧗ 5h 50% · 7d 25%" in capsys.readouterr().out
