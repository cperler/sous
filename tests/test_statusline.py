"""Statusline formatter (#61): a raw utilization line for the Claude Code status bar,
built over the same usage cache the `util` sensor feeds. Best-effort — a probe miss or a
malformed reset timestamp yields a quiet line, never an error."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator import usage_probe
from orchestrator.cli import main
from orchestrator.usage_probe import Usage, format_statusline

_NOW = datetime(2026, 7, 3, 15, 0, 0, tzinfo=UTC)


def test_format_statusline_probe_hit_shows_both_pcts_and_countdowns() -> None:
    usage = Usage(
        five_hour_pct=87.4,
        seven_day_pct=41.0,
        five_hour_resets_at="2026-07-03T18:00:00+00:00",  # +3h
        seven_day_resets_at="2026-07-08T00:00:00+00:00",  # +4d9h
    )
    line = format_statusline(usage, now=_NOW)
    assert line == "⧗ 5h 87% (resets 3h0m) · 7d 41% (resets 4d9h)"


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
