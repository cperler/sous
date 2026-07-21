"""Capacity policy tests — engine-owned dispatch limit; ceiling is a cap."""

from __future__ import annotations

import random

import pytest

from orchestrator.capacity import CapacityPolicy, DispatchBand

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
    # huge reset -> clamp to max_sleep (3600).
    assert P.sleep_seconds(reset_epoch=10_000_000, now_epoch=0, jitter_s=0) == 3600


def test_sleep_cap_includes_jitter() -> None:
    # Regression: jitter must NOT push the total past max_sleep_s (was 3600+300=3900).
    assert P.sleep_seconds(reset_epoch=10_000_000, now_epoch=0, jitter_s=300) == 3600


def test_jitter_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        P.sleep_seconds(reset_epoch=2000, now_epoch=1000, jitter_s=999)


# --- cheap-dispatch band table (#12) -----------------------------------------

def test_dispatch_band_normal_below_threshold() -> None:
    assert P.dispatch_band(0) is DispatchBand.NORMAL
    assert P.dispatch_band(69) is DispatchBand.NORMAL  # just under the high band


def test_dispatch_band_downgrade_edge() -> None:
    # 70 (inclusive) up to but excluding the 90 per-call gate -> DOWNGRADE.
    assert P.dispatch_band(70) is DispatchBand.DOWNGRADE
    assert P.dispatch_band(85) is DispatchBand.DOWNGRADE  # spans the throttle-to-1 range too
    assert P.dispatch_band(89) is DispatchBand.DOWNGRADE


def test_dispatch_band_wait_at_per_call_gate() -> None:
    # >= 90: WAIT — no dispatch (at_capacity blocks it; band just names the reason).
    assert P.dispatch_band(90) is DispatchBand.WAIT
    assert P.dispatch_band(95) is DispatchBand.WAIT


def test_dispatch_band_and_limit_are_orthogonal() -> None:
    # The high band overlaps BOTH dispatch_limit regimes: full ceiling (70–80) and
    # throttle-to-1 (80–90). Model choice and concurrency cap are separate decisions.
    assert P.dispatch_band(75) is DispatchBand.DOWNGRADE and P.dispatch_limit(75, 16) == 16
    assert P.dispatch_band(85) is DispatchBand.DOWNGRADE and P.dispatch_limit(85, 16) == 1


def test_downgrade_threshold_is_tunable() -> None:
    lax = CapacityPolicy(downgrade_threshold=50.0)
    assert lax.dispatch_band(50) is DispatchBand.DOWNGRADE
    assert lax.dispatch_band(49) is DispatchBand.NORMAL


# --- effort-aware adaptive band (#155) ---------------------------------------

def test_effort_threshold_at_zero_rate_is_base() -> None:
    # A group that never retries -> the flat base threshold, no adjustment.
    assert P.effort_downgrade_threshold(0.0) == P.downgrade_threshold


def test_effort_threshold_monotonic_in_rate() -> None:
    # Higher observed retry/failure rate -> higher threshold (smaller, less-eager band).
    rates = [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]
    vals = [P.effort_downgrade_threshold(r) for r in rates]
    assert vals == sorted(vals)
    assert vals[-1] > vals[0]  # a fully-retrying group is strictly less eager to downshift


def test_effort_threshold_clamped_below_per_call() -> None:
    # Even at rate 1.0 (and a steep slope), the edge stays strictly below the per-call gate
    # so the DOWNGRADE band shrinks but never collapses into WAIT.
    steep = CapacityPolicy(adaptive_band_slope=999.0)
    edge = steep.effort_downgrade_threshold(1.0)
    assert edge < steep.per_call_threshold
    assert edge == steep.per_call_threshold - steep.adaptive_band_margin


def test_effort_threshold_rate_clamped_to_unit_interval() -> None:
    # Out-of-range rates are clamped, not extrapolated.
    assert P.effort_downgrade_threshold(-1.0) == P.effort_downgrade_threshold(0.0)
    assert P.effort_downgrade_threshold(5.0) == P.effort_downgrade_threshold(1.0)


def test_effort_threshold_disabled_is_flat() -> None:
    off = CapacityPolicy(adaptive_band=False)
    assert off.effort_downgrade_threshold(1.0) == off.downgrade_threshold
    # A non-positive slope disables it too (no divide-by-anything, just a no-op).
    flat = CapacityPolicy(adaptive_band_slope=0.0)
    assert flat.effort_downgrade_threshold(1.0) == flat.downgrade_threshold


def test_dispatch_band_override_shrinks_band_at_edge() -> None:
    # A raised per-decision threshold flips a util that WOULD downshift at the flat edge
    # back to NORMAL, and leaves clearly-in-band util still DOWNGRADE.
    high_rate_edge = P.effort_downgrade_threshold(1.0)  # 89 with defaults
    assert P.dispatch_band(75) is DispatchBand.DOWNGRADE  # flat edge: in band
    assert P.dispatch_band(75, high_rate_edge) is DispatchBand.NORMAL  # raised edge: out
    assert P.dispatch_band(high_rate_edge, high_rate_edge) is DispatchBand.DOWNGRADE
    # The override never overrides the per-call WAIT gate.
    assert P.dispatch_band(95, 50.0) is DispatchBand.WAIT


def test_dispatch_band_override_none_is_flat() -> None:
    # Passing no override is identical to the flat single-arg call.
    for util in (0, 69, 70, 85, 89, 90, 95):
        assert P.dispatch_band(util, None) is P.dispatch_band(util)


# --- seedable, bounded jitter (#4) -------------------------------------------

def test_jitter_within_bounds_seeded() -> None:
    # An injected rng makes the draw deterministic; every draw stays in [min, max].
    rng = random.Random(1234)
    draws = [P.jitter(rng) for _ in range(1000)]
    assert all(P.min_jitter_s <= d <= P.max_jitter_s for d in draws)
    assert min(draws) >= P.min_jitter_s and max(draws) <= P.max_jitter_s


def test_jitter_deterministic_under_seed() -> None:
    # Same seed -> same sequence: reproducible in tests (mirrors the injected sleeper).
    assert [P.jitter(random.Random(7)) for _ in range(5)] == \
           [P.jitter(random.Random(7)) for _ in range(5)]


def test_jitter_feeds_sleep_seconds_within_cap() -> None:
    # The jitter() draw is always an acceptable sleep_seconds(jitter_s=...) argument.
    rng = random.Random(99)
    for _ in range(200):
        j = P.jitter(rng)
        total = P.sleep_seconds(reset_epoch=2000, now_epoch=1000, jitter_s=j)
        assert total <= P.max_sleep_s


def test_jitter_custom_min_floor() -> None:
    pol = CapacityPolicy(min_jitter_s=100, max_jitter_s=100)
    assert pol.jitter(random.Random(0)) == 100  # degenerate window -> exact value
