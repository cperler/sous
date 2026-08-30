"""Deterministic cost policy (#34): the engine-side controls that turn recorded cost
into a *lever*, with ZERO model judgment.

Two small, TABLE-driven pieces:

1. An **estimate table** (``ESTIMATE_USD`` / ``estimate_to_usd``): a rough size→USD map,
   grounded in real observed cost (dogfood 2026-07-02: a small headless issue metered
   $1.44 — #29). Used by the a-priori spec check (``spec plan/file --budget-usd``) and,
   optionally, to refine cost-aware lane routing. Deliberately ROUGH — advisory math,
   never billing.

2. A **cost router** (``CostRouter``): maps the run's REMAINING budget fraction (plus an
   optional per-task estimate) to a lane preset, so a run whose budget is thinning routes
   new, un-pinned tasks to cheaper pipelines and prefers the $0 deterministic TEST/DELIVER
   runners (#33). A named band table, not clever heuristics ("table-driven and small").

Pure and project-agnostic: no model call, no repo knowledge. Lives beside the engine so
both the engine (routing + budget thresholds) and the spec front door (a-priori estimate)
share one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas.enums import LANE_STAGES, ExecutionLane, Stage

# Soft-warning threshold: warn ONCE when metered spend reaches this fraction of the
# budget. The hard stop is at 1.0 (spend >= budget). Engine-overridable.
BUDGET_SOFT_FRACTION = 0.80

# Rough size -> USD. GROUNDED in real ledger observation: the 2026-07-02 dogfood metered
# $1.44 for a small headless issue (#29), so "small" ≈ $1.5. Medium/large are rough
# multiples for multi-file changes and review-churn-heavy tasks. ADVISORY only — this is
# a-priori sizing, never used to bill (the ledger prices actual calls authoritatively).
ESTIMATE_USD: dict[str, float] = {
    "small": 1.5,
    "medium": 4.0,
    "large": 10.0,
}
# Accepted aliases -> canonical size key (looked up case-insensitively).
_ESTIMATE_ALIASES: dict[str, str] = {
    "s": "small", "sm": "small", "xs": "small",
    "m": "medium", "med": "medium",
    "l": "large", "lg": "large", "xl": "large",
}


def estimate_to_usd(estimate: object) -> float | None:
    """Rough USD for a per-task ``estimate`` hint, or None if unrecognized/absent.

    Accepts a size word (small/medium/large + a few aliases, case-insensitive) OR a bare
    number (already USD — a caller who knows the figure). An unknown/empty value returns
    None so the caller counts it as *unestimated* rather than silently guessing $0."""
    if estimate is None:
        return None
    if isinstance(estimate, bool):  # guard: bool is an int subclass
        return None
    if isinstance(estimate, (int, float)):
        return float(estimate) if estimate >= 0 else None
    text = str(estimate).strip().lower()
    if not text:
        return None
    if text in ESTIMATE_USD:
        return ESTIMATE_USD[text]
    if text in _ESTIMATE_ALIASES:
        return ESTIMATE_USD[_ESTIMATE_ALIASES[text]]
    try:  # a bare numeric string ("2.5") is taken as USD directly
        val = float(text)
    except ValueError:
        return None
    return val if val >= 0 else None


# Remaining-budget-fraction bands -> lane preset. Read top-down: the FIRST band whose
# floor the remaining fraction meets wins. Plenty of budget -> FULL; once spend passes the
# soft threshold (remaining < 0.20) -> LITE; nearly exhausted (< 0.05) -> MICRO. A small
# table by design (#34: "table-driven and small ... not clever").
COST_ROUTING_BANDS: tuple[tuple[float, ExecutionLane], ...] = (
    (0.20, ExecutionLane.FULL),   # remaining >= 20% of budget: full pipeline
    (0.05, ExecutionLane.LITE),   # 5%–20% remaining: drop SCOPE (lite)
    (0.0, ExecutionLane.MICRO),   # < 5% remaining: cheapest preset (micro)
)

# Cheapest-last order, for the estimate nudge (one step cheaper).
_LANE_RANK: tuple[ExecutionLane, ...] = (
    ExecutionLane.FULL, ExecutionLane.LITE, ExecutionLane.MICRO,
)

# Stages that are $0 on the deterministic ENGINE lane (#33) — preferred as deterministic
# whenever routing downgrades a task to conserve budget.
# PUBLISH is not listed: its StageSpec is deterministic=True unconditionally, so it is
# already on the $0 ENGINE lane and there is nothing for cost pressure to route (#389).
_CHEAP_DETERMINISTIC: tuple[Stage, ...] = (Stage.TEST, Stage.DELIVER)


@dataclass(frozen=True)
class RouteDecision:
    """A cost-routing outcome: the chosen lane preset, the deterministic stages to run on
    the $0 ENGINE lane, and a self-describing ``reason`` (emitted as a ``lane_routed``
    event — routing is never silent)."""

    lane: ExecutionLane
    deterministic_stages: tuple[Stage, ...]
    reason: dict


@dataclass(frozen=True)
class CostRouter:
    """Deterministic budget-fraction -> lane-preset router. Injectable band table so a
    project can supply its own without touching the engine."""

    bands: tuple[tuple[float, ExecutionLane], ...] = COST_ROUTING_BANDS

    def route(self, remaining_fraction: float, estimate: object = None) -> RouteDecision:
        """Pick a lane preset from the REMAINING budget fraction, refined by an optional
        per-task estimate. No model judgment — pure table lookup + one bounded nudge."""
        lane = self.bands[-1][1]
        for floor, preset in self.bands:
            if remaining_fraction >= floor:
                lane = preset
                break
        # Estimate nudge: a LARGE task, when we're already below the top band, routes ONE
        # step cheaper — a big task shouldn't spend the last of a thinning budget on the
        # full pipeline. Deterministic, one step, bounded.
        est_usd = estimate_to_usd(estimate)
        nudged = False
        if (
            est_usd is not None
            and est_usd >= ESTIMATE_USD["large"]
            and lane is not ExecutionLane.FULL
        ):
            idx = _LANE_RANK.index(lane)
            if idx + 1 < len(_LANE_RANK):
                lane = _LANE_RANK[idx + 1]
                nudged = True
        # Below FULL, prefer $0 deterministic TEST/DELIVER for whatever the preset runs.
        det: tuple[Stage, ...] = ()
        if lane is not ExecutionLane.FULL:
            preset_stages = set(LANE_STAGES[lane])
            det = tuple(s for s in _CHEAP_DETERMINISTIC if s in preset_stages)
        return RouteDecision(
            lane=lane,
            deterministic_stages=det,
            reason={
                "remaining_fraction": round(remaining_fraction, 4),
                "estimate": None if estimate is None else str(estimate),
                "estimate_usd": est_usd,
                "estimate_nudge": nudged,
                "preset": lane.value,
                "deterministic_stages": [s.value for s in det],
            },
        )


DEFAULT_COST_ROUTER = CostRouter()
