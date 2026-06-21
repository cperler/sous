"""Capacity policy (target.md §3, fix from §6/§2).

Unifies the as-built two-gate model (ralph launch throttle >=80% admission +
per-task >=90% per-call) into one policy. **The engine computes the capacity-safe
dispatch limit and that is the binding concurrency policy** — the execution
adapter's own concurrency cap (e.g. Workflow's ``agent()`` cap) is merely a ceiling;
interactive mode must never dispatch beyond the engine's limit even if the shim could.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityPolicy:
    admission_threshold: float = 80.0  # >= this util: throttle new dispatch (drain)
    per_call_threshold: float = 90.0  # >= this util: at capacity, wait
    min_sleep_s: int = 60
    max_sleep_s: int = 3600  # cap on a single wait
    buffer_s: int = 60  # added to a computed reset wait
    max_jitter_s: int = 300

    def at_capacity(self, util_pct: float) -> bool:
        """Per-call gate: a single stage should wait."""
        return util_pct >= self.per_call_threshold

    def dispatch_limit(self, util_pct: float, ceiling: int) -> int:
        """Capacity-derived concurrency — the BINDING limit (ceiling is just a cap).

        - util >= per_call (90): 0 — wait, no new dispatch.
        - util >= admission (80): throttle to 1 to let in-flight work drain.
        - else: the full ceiling.
        Never exceeds ``ceiling`` (the adapter/Workflow cap).
        """

        if ceiling < 0:
            raise ValueError("ceiling must be >= 0")
        if util_pct >= self.per_call_threshold:
            allowed = 0
        elif util_pct >= self.admission_threshold:
            allowed = 1
        else:
            allowed = ceiling
        return min(allowed, ceiling)

    def sleep_seconds(
        self, *, reset_epoch: float, now_epoch: float, jitter_s: int = 0
    ) -> int:
        """Wait until the usage window resets, clamped, plus injected jitter.

        Ports the as-built math: ``reset - now + buffer``, clamped to
        ``[min_sleep, max_sleep]``, then ``+ jitter`` (0..max_jitter). Jitter is
        injected (not random) so the policy is deterministic and testable.
        """

        if not (0 <= jitter_s <= self.max_jitter_s):
            raise ValueError(f"jitter_s must be in [0, {self.max_jitter_s}]")
        base = (reset_epoch - now_epoch) + self.buffer_s
        clamped = max(self.min_sleep_s, min(int(base), self.max_sleep_s))
        return clamped + jitter_s


DEFAULT_CAPACITY = CapacityPolicy()
