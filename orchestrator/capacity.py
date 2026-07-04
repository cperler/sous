"""Capacity policy (target.md §3, fix from §6/§2).

Unifies the as-built two-gate model (ralph launch throttle >=80% admission +
per-task >=90% per-call) into one policy. **The engine computes the capacity-safe
dispatch limit and that is the binding concurrency policy** — the execution
adapter's own concurrency cap (e.g. Workflow's ``agent()`` cap) is merely a ceiling;
interactive mode must never dispatch beyond the engine's limit even if the shim could.

Two orthogonal capacity levers, kept DISTINCT (both are rate-limit headroom, not USD —
cost is ``cost_policy``):
  - a concurrency throttle (``dispatch_limit`` — how MANY tasks may dispatch), and
  - a cheap-dispatch band (``dispatch_band`` — on WHICH model a fresh dispatch runs).
The band table (#12) is the capacity sibling of ``cost_policy``'s budget-fraction table:
a small named map over CURRENT utilization -> dispatch behavior.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

# --- Wait/backoff tuning (#4) ------------------------------------------------
# Named, justified constants instead of magic numbers so the wait window is tunable and
# testable. The capacity policy waits out a saturated 5h/7d usage window; these bound
# that single wait.
MIN_SLEEP_S = 60      # never busy-spin: a sub-minute wait can't meaningfully drain a window
MAX_SLEEP_S = 3600    # a single wait never parks a run > 1h — re-probe hourly at worst
RESET_BUFFER_S = 60   # wait PAST the advertised reset so clock skew / a lazy server reset
#                       doesn't wake us straight back into the limit
# Thundering-herd jitter: N waiters that all woke on the same reset epoch would re-hit the
# API in lockstep and re-trip the limit together. A random spread desynchronizes them.
# Bounded [MIN_JITTER_S, MAX_JITTER_S] so jitter can never dominate the wait, and DRAWN
# FROM AN INJECTED rng (mirror the injected-sleeper pattern) so tests are deterministic
# under a seed while production still spreads. 0..5min: enough to separate a handful of
# concurrent workers, small next to the 1h cap.
MIN_JITTER_S = 0
MAX_JITTER_S = 300

# --- Cheap-dispatch band (#12) -----------------------------------------------
# Utilization at/above which a FRESH dispatch degrades to a cheaper model (but still
# dispatches) instead of running the role default. Sits BELOW the per-call gate (90) so
# the band is actually REACHABLE: at >=90 there is no dispatch at all, so a downgrade
# gated there would be dead (exactly why the first attempt was reverted — see #12). One
# step down the provider's fallback chain by default; both are tunable on the policy.
DOWNGRADE_THRESHOLD = 70.0
DOWNGRADE_STEPS = 1


class DispatchBand(StrEnum):
    """What CURRENT utilization says about a fresh dispatch's MODEL (not its count)."""

    NORMAL = "normal"        # < downgrade band: role-default model
    DOWNGRADE = "downgrade"  # high band: keep dispatching, but on a cheaper model
    WAIT = "wait"            # >= per-call gate: no new dispatch (see at_capacity)


@dataclass(frozen=True)
class CapacityPolicy:
    admission_threshold: float = 80.0  # >= this util: throttle new dispatch (drain)
    per_call_threshold: float = 90.0  # >= this util: at capacity, wait
    # Cheap-dispatch band (#12): [downgrade_threshold, per_call_threshold) downgrades a
    # fresh dispatch by downgrade_steps down the fallback chain. Must stay below the
    # per-call gate to be reachable.
    downgrade_threshold: float = DOWNGRADE_THRESHOLD
    downgrade_steps: int = DOWNGRADE_STEPS
    min_sleep_s: int = MIN_SLEEP_S
    max_sleep_s: int = MAX_SLEEP_S  # cap on a single wait
    buffer_s: int = RESET_BUFFER_S  # added to a computed reset wait
    min_jitter_s: int = MIN_JITTER_S
    max_jitter_s: int = MAX_JITTER_S

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

    def dispatch_band(self, util_pct: float) -> DispatchBand:
        """Cheap-dispatch band (#12): CURRENT util -> a fresh dispatch's model behavior.

        Named band table (the capacity sibling of ``cost_policy``'s budget-fraction map),
        ORTHOGONAL to ``dispatch_limit``: this picks the model, that picks how many tasks.
        - util >= per_call (90): WAIT — no dispatch (``at_capacity`` already blocks it).
        - util >= downgrade (70): DOWNGRADE — keep progressing on a cheaper model.
        - else: NORMAL — role default.
        The DOWNGRADE band deliberately spans BOTH the throttle-to-1 range (80–90) and the
        full-ceiling range (70–80): the model choice and the concurrency cap are separate
        decisions that compose.
        """

        if util_pct >= self.per_call_threshold:
            return DispatchBand.WAIT
        if util_pct >= self.downgrade_threshold:
            return DispatchBand.DOWNGRADE
        return DispatchBand.NORMAL

    def jitter(self, rng: random.Random) -> int:
        """A bounded, seed-deterministic thundering-herd jitter in
        ``[min_jitter_s, max_jitter_s]``.

        The rng is INJECTED (mirror the injected-sleeper pattern) so a test pins it with a
        seed while production passes a real ``random.Random`` — the spread is real but the
        bounds are provable. Feed the result to ``sleep_seconds(jitter_s=...)``.
        """

        return rng.randint(self.min_jitter_s, self.max_jitter_s)

    def sleep_seconds(
        self, *, reset_epoch: float, now_epoch: float, jitter_s: int = 0
    ) -> int:
        """Wait until the usage window resets, clamped, plus injected jitter.

        Ports the as-built math: ``reset - now + buffer``, clamped to
        ``[min_sleep, max_sleep]``, then ``+ jitter`` (0..max_jitter). Jitter is
        injected (not random) so the policy is deterministic and testable — draw it
        from ``jitter(rng)`` for a bounded, seedable spread.
        """

        if not (0 <= jitter_s <= self.max_jitter_s):
            raise ValueError(f"jitter_s must be in [0, {self.max_jitter_s}]")
        base = (reset_epoch - now_epoch) + self.buffer_s
        floored = max(self.min_sleep_s, int(base))
        # Cap INCLUDES jitter — max_sleep_s is a hard ceiling on the total wait.
        return min(floored + jitter_s, self.max_sleep_s)


DEFAULT_CAPACITY = CapacityPolicy()
