"""Capacity policy tests — engine-owned dispatch limit; ceiling is a cap."""

from __future__ import annotations

import pytest

from orchestrator.capacity import CapacityPolicy

P = CapacityPolicy()


def test_dispatch_limit_full_below_admission() -> None:
    assert P.dispatch_limit(util_pct=10, ceiling=16) == 16


def test_dispatch_limit_throttles_above_admission() -> None:
    # >=80% admission: throttle to 1 to drain in-flight work.
    assert P.dispatch_limit(util_pct=85, ceiling=16) == 1


def test_dispatch_limit_zero_at_capacity() -> None:
    assert P.dispatch_limit(util_pct=95, ceiling=16) == 0


def test_dispatch_limit_never_exceeds_ceiling() -> None:
    # The engine limit is binding; the Workflow ceiling caps it.
    assert P.dispatch_limit(util_pct=0, ceiling=3) == 3
    assert P.dispatch_limit(util_pct=50, ceiling=1) == 1


def test_at_capacity_gate() -> None:
    assert not P.at_capacity(89)
    assert P.at_capacity(90)


def test_sleep_math_clamped_and_jittered() -> None:
    # reset 1000s out, +60 buffer = 1060, within [60, 3600]; +30 jitter = 1090.
    assert P.sleep_seconds(reset_epoch=2000, now_epoch=1000, jitter_s=30) == 1090


def test_sleep_min_clamp() -> None:
    # reset already passed -> negative base -> clamp to min_sleep (60), +0 jitter.
    assert P.sleep_seconds(reset_epoch=100, now_epoch=1000, jitter_s=0) == 60


def test_sleep_max_clamp() -> None:
    # huge reset -> clamp to max_sleep (3600) before jitter.
    assert P.sleep_seconds(reset_epoch=10_000_000, now_epoch=0, jitter_s=0) == 3600


def test_jitter_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        P.sleep_seconds(reset_epoch=2000, now_epoch=1000, jitter_s=999)
