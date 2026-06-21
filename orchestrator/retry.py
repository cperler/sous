"""Retry-with-learnings + structured circuit breaker (target.md §3).

Ports the as-built retry-with-learnings (learnings APPENDED, newest last) and the
identical-signature circuit breaker — but **fix-forward**: the signature is
*structured* (a hash over the stage + the normalized failure set), not the as-built
brittle ``<stage>:<first-100-chars-of-a-raw-log-line>`` which timestamps and paths
made falsely-distinct so the breaker rarely tripped.
"""

from __future__ import annotations

import hashlib
import re

from .schemas.enums import Stage

_NORMALIZE = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),  # timestamps
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),  # pointers
    (re.compile(r"/\S+"), "<path>"),  # absolute paths
    (re.compile(r"\b\d+\b"), "<n>"),  # bare numbers
    (re.compile(r"\s+"), " "),  # collapse whitespace
]


def _normalize(text: str) -> str:
    out = text.strip()
    for pat, repl in _NORMALIZE:
        out = pat.sub(repl, out)
    return out.strip()


def error_signature(
    stage: Stage,
    *,
    failures: list[str] | None = None,
    error: str | None = None,
) -> str:
    """Stable signature of a failure for the circuit breaker.

    Prefers the *set of failing test names* (deterministic across reordering) when
    available — this is the good as-built signal (``check_failure_signature_plateau``).
    Otherwise hashes the stage + a normalized error string so cosmetic variation
    (timestamps/paths/numbers) does not make identical failures look distinct.
    """

    if failures:
        body = "test-set:" + "\n".join(sorted(set(failures)))
    else:
        body = "error:" + _normalize(error or "")
    blob = f"{stage.value}\x1f{body}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CircuitBreaker:
    """Trips when the same structured signature recurs ``threshold`` times in a row."""

    def __init__(self, threshold: int = 2) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self._last: str | None = None
        self._streak = 0

    @property
    def streak(self) -> int:
        return self._streak

    def observe(self, signature: str) -> bool:
        """Record a signature; return True if the breaker has tripped."""
        if signature == self._last:
            self._streak += 1
        else:
            self._last = signature
            self._streak = 1
        return self.tripped

    @property
    def tripped(self) -> bool:
        return self._streak >= self.threshold

    def reset(self) -> None:
        self._last = None
        self._streak = 0


class RetryState:
    """Per-task attempt tracking + appended learnings."""

    def __init__(self, max_attempts: int = 3, breaker_threshold: int = 2) -> None:
        self.max_attempts = max_attempts
        self.attempt = 0
        self.learnings: list[str] = []  # APPEND order, newest last (ports as-built)
        self.signatures: list[str] = []
        self.breaker = CircuitBreaker(breaker_threshold)

    def record_failure(self, signature: str, learning: str) -> None:
        self.attempt += 1
        self.signatures.append(signature)
        self.learnings.append(learning)
        self.breaker.observe(signature)

    def should_retry(self) -> bool:
        """Retry only if attempts remain AND the breaker has not tripped."""
        return self.attempt < self.max_attempts and not self.breaker.tripped

    def learnings_text(self) -> str:
        """Accumulated learnings to prepend to the next attempt's prompt."""
        return "\n\n".join(
            f"## Attempt {i + 1}\n{txt}" for i, txt in enumerate(self.learnings)
        )
