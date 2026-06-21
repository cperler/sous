"""Retry + structured circuit-breaker tests (fix-forward: robust signature)."""

from __future__ import annotations

from orchestrator.retry import CircuitBreaker, RetryState, error_signature
from orchestrator.schemas.enums import Stage


def test_signature_stable_across_cosmetic_variation() -> None:
    # The brittle as-built signature used a raw log line; timestamps/paths/numbers
    # made identical failures look distinct. The structured signature is stable.
    a = error_signature(Stage.TEST, error="2026-06-20T10:00:00Z FAILED at /tmp/x/abc.py:42")
    b = error_signature(Stage.TEST, error="2026-06-20T11:30:05Z FAILED at /var/y/abc.py:99")
    assert a == b


def test_signature_distinguishes_real_difference() -> None:
    a = error_signature(Stage.TEST, error="AssertionError in foo")
    b = error_signature(Stage.TEST, error="ImportError in bar")
    assert a != b


def test_signature_test_set_order_invariant() -> None:
    a = error_signature(Stage.TEST, failures=["t_b", "t_a"])
    b = error_signature(Stage.TEST, failures=["t_a", "t_b", "t_a"])
    assert a == b  # set + sort -> order/dup invariant


def test_circuit_breaker_trips_on_repeat() -> None:
    cb = CircuitBreaker(threshold=2)
    assert cb.observe("sig-x") is False  # streak 1
    assert cb.observe("sig-x") is True  # streak 2 -> tripped
    assert cb.tripped


def test_circuit_breaker_resets_on_change() -> None:
    cb = CircuitBreaker(threshold=2)
    cb.observe("a")
    cb.observe("b")  # different -> streak back to 1
    assert not cb.tripped
    assert cb.streak == 1


def test_retry_state_stops_on_breaker_before_max() -> None:
    rs = RetryState(max_attempts=5, breaker_threshold=2)
    sig = error_signature(Stage.IMPLEMENT, error="same boom")
    rs.record_failure(sig, "tried X, failed")
    assert rs.should_retry()  # attempt 1, breaker not tripped
    rs.record_failure(sig, "tried X again, failed")
    assert not rs.should_retry()  # breaker tripped at 2 identical, despite attempts left
    assert rs.attempt == 2


def test_retry_state_stops_on_max_attempts() -> None:
    rs = RetryState(max_attempts=2, breaker_threshold=5)
    rs.record_failure(error_signature(Stage.TEST, error="a"), "l1")
    rs.record_failure(error_signature(Stage.TEST, error="b"), "l2")
    assert not rs.should_retry()  # hit max_attempts


def test_learnings_appended_newest_last() -> None:
    rs = RetryState()
    rs.record_failure("s1", "first")
    rs.record_failure("s2", "second")
    text = rs.learnings_text()
    assert text.index("first") < text.index("second")
    assert "## Attempt 1" in text and "## Attempt 2" in text
