"""The pure half of the OPTIMIZE benchmark gate (#520): read a benchmark's numbers and
decide whether the stage earned its commit.

#519 shipped the OPTIMIZE stage with MODEL-REPORTED ``measurements``, which is a speed
claim nobody checked — exactly the shape CLAUDE.md says wants a check the harness runs
rather than firmer prose. This module is that check's decision layer: the engine runs the
project adapter's declared benchmark argv before and after the stage's commit, hands the two
stdout blobs here, and keeps the commit only when this module says there was a win.

Everything here is pure (no wall-clock, no subprocess, no event sink), per the fold
convention: it RETURNS what it could not judge and why, and ``Engine`` emits the events.

The output contract a benchmark command must satisfy: its LAST non-empty stdout line is a
JSON object mapping a metric name to either a bare number or
``{"value": <number>, "unit": "ms", "lower_is_better": true}``. ``lower_is_better``
defaults to true because the common metric is a duration; a throughput metric (ops/s) must
say ``false`` or a speedup reads as a regression. Anything else is unreadable and the gate
degrades to advisory — an unparseable benchmark must never read as green.

The win rule (``compare_benchmarks``): at least one comparable metric improves by more than
the tolerance AND no comparable metric regresses by more than it. Both halves matter — a
pass that halves one hot path while doubling another has not earned anything, and a pass
whose numbers all sit inside the noise band has not measured a win at all.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

# The minimum relative improvement that counts as a win. 5% is the documented default: below
# it a single benchmark run on a shared machine cannot distinguish a real gain from load
# noise, so keeping a commit on a 1% "win" would make the gate a rubber stamp. A project
# whose benchmark is quieter (or noisier) than that overrides it with ``benchmark_tolerance``.
DEFAULT_BENCHMARK_TOLERANCE = 0.05


@dataclass(frozen=True)
class Metric:
    """One measured number, with the direction that makes it better."""

    value: float
    unit: str = ""
    lower_is_better: bool = True


def parse_benchmark_output(stdout: str) -> tuple[dict[str, Metric] | None, str | None]:
    """Read a benchmark command's stdout into metrics, or explain why it could not be read.

    Returns ``(metrics, None)`` on success and ``(None, reason)`` otherwise — never raises,
    because an adapter's benchmark printing garbage must degrade the gate to advisory rather
    than break ``record()``. Only the last non-empty line is parsed, so a command is free to
    print progress noise (a pytest-benchmark table, a build log) above its result line.
    """
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None, "benchmark printed no output"
    try:
        payload = json.loads(lines[-1])
    except (ValueError, TypeError) as exc:
        return None, f"last output line is not JSON ({exc})"
    if not isinstance(payload, dict):
        return None, f"top-level JSON is {type(payload).__name__}, not an object of metrics"
    if not payload:
        return None, "benchmark reported an empty metric object"

    metrics: dict[str, Metric] = {}
    for name, raw in payload.items():
        metric, reason = _coerce_metric(raw)
        if metric is None:
            return None, f"metric {name!r}: {reason}"
        metrics[str(name)] = metric
    return metrics, None


def _coerce_metric(raw: object) -> tuple[Metric | None, str | None]:
    """One metric value in either accepted shape. A malformed metric fails the whole parse:
    a partially-read benchmark would silently judge a subset of what was measured."""
    if isinstance(raw, dict):
        value: object = raw.get("value")
        unit = str(raw.get("unit") or "")
        lower = raw.get("lower_is_better", True)
        if not isinstance(lower, bool):
            return None, f"lower_is_better must be a bool, got {type(lower).__name__}"
    else:
        value, unit, lower = raw, "", True
    # bool is an int in Python; a True/False "measurement" is a category error, not a number.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"value must be a number, got {type(value).__name__}"
    if not math.isfinite(float(value)):
        return None, f"value is not finite ({value})"
    return Metric(value=float(value), unit=unit, lower_is_better=lower), None


def compare_benchmarks(
    before: dict[str, Metric],
    after: dict[str, Metric],
    *,
    tolerance: float = DEFAULT_BENCHMARK_TOLERANCE,
) -> dict:
    """Judge a before/after pair against ``tolerance``.

    Returns ``{kept, reason, tolerance, metrics, notices}``. Each row in ``metrics`` carries
    ``name/before/after/unit/lower_is_better/delta_fraction/direction/judged``, where
    ``delta_fraction`` is signed so that POSITIVE always means better regardless of the
    metric's direction, and ``judged`` says whether the row counted toward the verdict.

    Reasons: ``win``, ``regressed`` (something got materially worse — reported in preference
    to ``no_win``, since it is the more serious finding), ``no_win`` (nothing moved beyond
    the noise band), ``no_metrics_compared`` (the two runs share no judgeable metric, so
    there is no measurement to stand on).

    A metric is EXCLUDED from the verdict when its unit changed between runs: comparing
    milliseconds to seconds is not a comparison. It is still reported, with a notice.
    """
    notices: list[dict[str, str]] = []
    for name in sorted(set(before) - set(after)):
        notices.append({
            "notice": "metric_missing_after",
            "detail": f"{name} was measured before the pass and not after",
        })
    for name in sorted(set(after) - set(before)):
        notices.append({
            "notice": "metric_missing_before",
            "detail": f"{name} was measured after the pass and not before",
        })

    rows: list[dict] = []
    for name in sorted(set(before) & set(after)):
        b, a = before[name], after[name]
        # The BASELINE defines what the metric means; a direction that flips mid-pass is a
        # benchmark bug, and silently adopting the new one could turn a regression into a win.
        lower_is_better = b.lower_is_better
        if a.lower_is_better != b.lower_is_better:
            notices.append({
                "notice": "metric_direction_changed",
                "detail": f"{name}: lower_is_better flipped between runs; "
                          f"using the baseline's ({lower_is_better})",
            })
        judged = True
        if a.unit != b.unit:
            judged = False
            notices.append({
                "notice": "metric_unit_changed",
                "detail": f"{name}: unit changed {b.unit or '(none)'} -> {a.unit or '(none)'}; "
                          "not comparable, excluded from the verdict",
            })
        delta = _delta_fraction(b.value, a.value, lower_is_better=lower_is_better)
        rows.append({
            "name": name,
            "before": b.value,
            "after": a.value,
            "unit": b.unit if a.unit == b.unit else f"{b.unit or '?'}->{a.unit or '?'}",
            "lower_is_better": lower_is_better,
            "delta_fraction": delta,
            "direction": _direction(
                delta, tolerance,
                before=b.value, after=a.value, lower_is_better=lower_is_better,
            ),
            "judged": judged,
        })

    judged_rows = [r for r in rows if r["judged"]]
    if not judged_rows:
        reason = "no_metrics_compared"
    elif any(r["direction"] == "regressed" for r in judged_rows):
        reason = "regressed"
    elif any(r["direction"] == "improved" for r in judged_rows):
        reason = "win"
    else:
        reason = "no_win"
    return {
        "kept": reason == "win",
        "reason": reason,
        "tolerance": tolerance,
        "metrics": rows,
        "notices": notices,
    }


def _delta_fraction(before: float, after: float, *, lower_is_better: bool) -> float | None:
    """Relative change, signed so positive is better. ``None`` when the baseline is zero and
    the fraction is therefore undefined (the caller treats any move off zero as unbounded)."""
    if before == 0:
        return None
    raw = (before - after) / abs(before)
    return raw if lower_is_better else -raw


def _direction(
    delta: float | None, tolerance: float, *,
    before: float, after: float, lower_is_better: bool,
) -> str:
    """``improved``/``regressed``/``flat`` for one row.

    A ``None`` delta is a zero baseline: there is no ratio to compare against the tolerance,
    so any move off zero counts as an unbounded change in its own direction (0 ms -> 3 ms is
    a regression however small the absolute number), and an unchanged zero is flat. Calling
    it flat instead would let a pass that made a free operation cost something through.
    """
    if delta is None:
        if after == before:
            return "flat"
        return "improved" if (after < before) == lower_is_better else "regressed"
    if delta > tolerance:
        return "improved"
    if delta < -tolerance:
        return "regressed"
    return "flat"


def format_metric_rows(rows: list[dict]) -> list[str]:
    """One human line per metric row, shared by the engine's events and the completion note
    so the numbers a human reads on GitHub cannot disagree with the numbers in the run log."""
    lines: list[str] = []
    for row in rows:
        unit = f" {row['unit']}" if str(row.get("unit") or "").strip() else ""
        delta = row.get("delta_fraction")
        change = "baseline 0 (no ratio)" if delta is None else f"{float(delta) * 100:+.1f}%"
        suffix = "" if row.get("judged", True) else " [not comparable]"
        lines.append(
            f"{row.get('name', '(unnamed)')}: {row.get('before', '?')}{unit} -> "
            f"{row.get('after', '?')}{unit} ({change}, {row.get('direction', '?')}){suffix}"
        )
    return lines
