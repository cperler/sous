"""Cross-run review-panel yield and cost reporting (#286).

The report is deliberately observational.  A panel replaces the single-reviewer path, so
the artifacts contain no same-diff counterfactual and cannot say what a single reviewer
would have caught.  What they *can* answer is whether the panel is producing useful-looking
signals, how often verification refutes them, and what those calls cost.

Review stage records are the source of truth for review counts and panel yield.  Cost rows
are regrouped by ``work_item_id`` and retain their raw ``phase`` discriminator; no aggregate
ledger row exists for a panel.  Every reader is best-effort across runs: a damaged artifact
is counted in ``coverage`` rather than making all other runs disappear from the report.
"""

from __future__ import annotations

import json
import math
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOW_PANEL_SAMPLE = 5


@dataclass(frozen=True)
class _RunRoot:
    run_id: str
    path: Path
    mtime: float


@dataclass(frozen=True)
class _ReviewRecord:
    run_id: str
    path: Path
    task_id: str
    work_item_id: str
    kind: str
    status: str
    panel_summary: dict[str, Any] | None


def _run_root(child: Path) -> _RunRoot | None:
    """Return one run location, or ``None`` for an unrelated directory."""
    candidates = sorted(child.glob("status-*.json"))
    if not candidates and not (child / "stages").is_dir() and not (
        child / "stage-costs.jsonl"
    ).is_file():
        return None

    run_id = child.name
    mtime = child.stat().st_mtime
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("document_type") == "run":
            run_id = str(payload.get("run_id") or child.name)
            with suppress(OSError):  # disappeared between read and stat
                mtime = candidate.stat().st_mtime
            break
    return _RunRoot(run_id=run_id, path=child, mtime=mtime)


def _discover_runs(root: Path) -> list[_RunRoot]:
    if not root.is_dir():
        return []
    runs: list[_RunRoot] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            location = _run_root(child)
        except OSError:
            continue
        if location is not None:
            runs.append(location)
    return sorted(runs, key=lambda run: (-run.mtime, run.run_id))


def _as_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _as_cost(value: object) -> tuple[float, bool]:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value), True
    return 0.0, False


def _read_reviews(run: _RunRoot, coverage: dict[str, int]) -> list[_ReviewRecord]:
    reviews: list[_ReviewRecord] = []
    for path in sorted(run.path.glob("stages/*/*-review.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            coverage["unreadable_review_records"] += 1
            continue
        if not isinstance(payload, dict):
            coverage["unreadable_review_records"] += 1
            continue
        raw_work_item_id = payload.get("work_item_id")
        if not isinstance(raw_work_item_id, str) or not raw_work_item_id:
            coverage["review_records_without_work_item_id"] += 1
            work_item_id = f"stage:{path.relative_to(run.path)}"
        else:
            work_item_id = raw_work_item_id
        raw_summary = payload.get("panel_summary")
        summary = raw_summary if isinstance(raw_summary, dict) else None
        if "panel_summary" in payload and summary is None:
            coverage["malformed_panel_summaries"] += 1
        reviews.append(
            _ReviewRecord(
                run_id=run.run_id,
                path=path,
                task_id=str(payload.get("task_id") or path.parent.name),
                work_item_id=work_item_id,
                kind="panel" if "sub_results" in payload else "single",
                status=str(payload.get("status") or "unknown"),
                panel_summary=summary,
            )
        )
    return reviews


def _read_ledger(run: _RunRoot, coverage: dict[str, int]) -> list[dict[str, Any]]:
    path = run.path / "stage-costs.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        coverage["unreadable_ledgers"] += 1
        return []

    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            coverage["malformed_ledger_rows"] += 1
            continue
        if not isinstance(row, dict):
            coverage["malformed_ledger_rows"] += 1
            continue
        if row.get("stage") != "review":
            continue
        # A legacy shared ledger may contain several runs.  Rows without a run_id predate
        # that isolation and remain usable; an explicitly different run must not inflate it.
        row_run_id = row.get("run_id")
        if row_run_id is not None and str(row_run_id) != run.run_id:
            coverage["foreign_ledger_rows"] += 1
            continue
        rows.append(row)
    return rows


def _phase_name(row: dict[str, Any]) -> str:
    if "phase" not in row:
        return "single"
    phase = row.get("phase")
    return phase if isinstance(phase, str) and phase else "unknown-panel"


def _phase_role(row: dict[str, Any]) -> str:
    phase = _phase_name(row)
    if phase.startswith("find:"):
        return "finder"
    if phase.startswith("verify:"):
        return "verifier"
    return "single" if phase == "single" else "other-panel"


def _new_call_bucket() -> dict[str, Any]:
    return {
        "invocations": 0,
        "total_cost_usd": 0.0,
        "unmetered_calls": 0,
        "unpriced_calls": 0,
        "schema_retry_calls": 0,
        "schema_retries": 0,
        "schema_retry_rate": 0.0,
        "cost_is_floor": False,
    }


def _add_call(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    cost, valid_cost = _as_cost(row.get("cost_usd"))
    retries = _as_count(row.get("schema_retries", 0))
    bucket["invocations"] += 1
    bucket["total_cost_usd"] += cost
    if row.get("metered") is False:
        bucket["unmetered_calls"] += 1
    if row.get("priced") is False or not valid_cost:
        bucket["unpriced_calls"] += 1
    if retries:
        bucket["schema_retry_calls"] += 1
        bucket["schema_retries"] += retries


def _finish_call_bucket(bucket: dict[str, Any]) -> None:
    calls = bucket["invocations"]
    bucket["total_cost_usd"] = round(bucket["total_cost_usd"], 6)
    bucket["schema_retry_rate"] = round(bucket["schema_retry_calls"] / calls, 4) if calls else 0.0
    bucket["cost_is_floor"] = bool(bucket["unmetered_calls"] or bucket["unpriced_calls"])


def _aggregate_yield(reviews: list[_ReviewRecord], coverage: dict[str, int]) -> dict[str, Any]:
    by_lens: dict[str, dict[str, int]] = {}
    findings = agreed = confirmed = refuted = inconclusive = 0
    cap_hits = cap_dropped = summarized = 0

    for review in reviews:
        summary = review.panel_summary
        if review.kind == "panel" and summary is None:
            coverage["panel_reviews_without_summary"] += 1
            continue
        if review.kind != "panel":
            if summary is not None:
                coverage["single_reviews_with_panel_summary"] += 1
            continue
        if summary is None:  # narrowed above; retained for the type checker
            continue
        summarized += 1
        findings += _as_count(summary.get("findings"))
        agreed += _as_count(summary.get("agreed"))
        inconclusive += _as_count(summary.get("inconclusive"))
        cap_dropped += _as_count(summary.get("cap_dropped"))
        if summary.get("cap_hit") is True:
            cap_hits += 1

        verdicts = summary.get("verdicts")
        if isinstance(verdicts, dict):
            confirmed += _as_count(verdicts.get("confirmed"))
            refuted += _as_count(verdicts.get("refuted"))
        lenses = summary.get("lenses")
        if not isinstance(lenses, dict):
            continue
        for lens, raw in lenses.items():
            if not isinstance(raw, dict):
                continue
            bucket = by_lens.setdefault(
                str(lens), {"reviews": 0, "findings": 0, "unique": 0, "shared": 0}
            )
            bucket["reviews"] += 1
            bucket["findings"] += _as_count(raw.get("total"))
            bucket["unique"] += _as_count(raw.get("unique"))
            bucket["shared"] += _as_count(raw.get("shared"))

    verdict_total = confirmed + refuted
    panel_reviews = sum(review.kind == "panel" for review in reviews)
    return {
        "panel_reviews": panel_reviews,
        "summarized_panel_reviews": summarized,
        "findings": findings,
        "agreed": agreed,
        "agreement_rate": round(agreed / findings, 4) if findings else 0.0,
        "by_lens": {lens: by_lens[lens] for lens in sorted(by_lens)},
        "verifiers": {
            "confirmed": confirmed,
            "refuted": refuted,
            "inconclusive": inconclusive,
            "refute_rate": round(refuted / verdict_total, 4) if verdict_total else 0.0,
        },
        "verifier_cap": {
            "hits": cap_hits,
            "eligible_reviews": summarized,
            "hit_rate": round(cap_hits / summarized, 4) if summarized else 0.0,
            "findings_dropped": cap_dropped,
        },
    }


def _review_cost(
    review: _ReviewRecord | None,
    run_id: str,
    work_item_id: str,
    rows: list[dict[str, Any]],
    ledger_kind: str | None,
) -> dict[str, Any]:
    total = 0.0
    invalid_costs = 0
    retries = 0
    for row in rows:
        cost, valid = _as_cost(row.get("cost_usd"))
        total += cost
        invalid_costs += not valid
        retries += _as_count(row.get("schema_retries", 0))
    unmetered = sum(row.get("metered") is False for row in rows)
    unpriced = sum(row.get("priced") is False for row in rows) + invalid_costs
    stage_kind = review.kind if review is not None else None
    return {
        "run_id": run_id,
        "task_id": review.task_id if review is not None else str(rows[0].get("task_id") or "unknown"),
        "work_item_id": work_item_id,
        "kind": stage_kind or ledger_kind or "unknown",
        "status": review.status if review is not None else str(rows[0].get("status") or "unknown"),
        "stage_marker": stage_kind,
        "ledger_marker": ledger_kind,
        "invocations": len(rows),
        "cost_usd": round(total, 6) if rows else None,
        "unmetered_calls": unmetered,
        "unpriced_calls": unpriced,
        "schema_retries": retries,
        "cost_is_floor": not rows or bool(unmetered or unpriced),
    }


def _aggregate_review_costs(per_review: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for kind in ("panel", "single", "unknown"):
        selected = [review for review in per_review if review["kind"] == kind]
        with_rows = [review for review in selected if review["cost_usd"] is not None]
        total = sum(review["cost_usd"] for review in with_rows)
        result[kind] = {
            "reviews": len(selected),
            "reviews_with_cost_rows": len(with_rows),
            "reviews_missing_cost_rows": len(selected) - len(with_rows),
            "invocations": sum(review["invocations"] for review in selected),
            "total_cost_usd": round(total, 6),
            "avg_cost_usd_per_recorded_review": round(total / len(with_rows), 6)
            if with_rows
            else None,
            "unmetered_reviews": sum(bool(review["unmetered_calls"]) for review in selected),
            "unpriced_reviews": sum(bool(review["unpriced_calls"]) for review in selected),
            "cost_is_floor": any(review["cost_is_floor"] for review in selected),
        }
    return result


def build_panel_report(root: str | Path, *, limit: int = 20) -> dict[str, Any]:
    """Aggregate review-panel telemetry from the newest stores under ``root``.

    ``root`` is the shared runs directory, whose child stores are ordered by their run
    document's mtime.  ``limit`` must be positive and bounds that newest-first sample.
    The returned JSON-ready mapping separates stage-record yield from ledger cost: stage
    ``sub_results`` marks a panel review, while ledger ``phase`` marks panel sub-calls.
    Marker disagreements, unreadable artifacts, missing cost rows, and unmetered or
    unpriced calls are retained in ``coverage`` rather than discarded; affected cost
    totals are explicitly floors.  The report is observational and intentionally has no
    single-reviewer counterfactual.

    Raises:
        ValueError: If ``limit`` is less than one.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    all_runs = _discover_runs(Path(root))
    selected_runs = all_runs[:limit]
    coverage = {
        "unreadable_review_records": 0,
        "review_records_without_work_item_id": 0,
        "malformed_panel_summaries": 0,
        "panel_reviews_without_summary": 0,
        "single_reviews_with_panel_summary": 0,
        "unreadable_ledgers": 0,
        "malformed_ledger_rows": 0,
        "foreign_ledger_rows": 0,
        "ledger_rows_without_work_item_id": 0,
        "ledger_only_reviews": 0,
        "marker_disagreements": 0,
        "invalid_cost_rows": 0,
    }
    reviews: list[_ReviewRecord] = []
    per_review: list[dict[str, Any]] = []
    by_phase: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}

    for run in selected_runs:
        run_reviews = _read_reviews(run, coverage)
        reviews.extend(run_reviews)
        ledger_rows = _read_ledger(run, coverage)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in ledger_rows:
            if not _as_cost(row.get("cost_usd"))[1]:
                coverage["invalid_cost_rows"] += 1
            phase = _phase_name(row)
            _add_call(by_phase.setdefault(phase, _new_call_bucket()), row)
            role = _phase_role(row)
            _add_call(by_role.setdefault(role, _new_call_bucket()), row)
            work_item_id = row.get("work_item_id")
            if not isinstance(work_item_id, str) or not work_item_id:
                coverage["ledger_rows_without_work_item_id"] += 1
                continue
            grouped.setdefault(work_item_id, []).append(row)

        for review in run_reviews:
            rows = grouped.pop(review.work_item_id, [])
            ledger_kind = None
            if rows:
                ledger_kind = "panel" if any("phase" in row for row in rows) else "single"
                if ledger_kind != review.kind:
                    coverage["marker_disagreements"] += 1
            per_review.append(
                _review_cost(review, run.run_id, review.work_item_id, rows, ledger_kind)
            )
        for work_item_id, rows in grouped.items():
            ledger_kind = "panel" if any("phase" in row for row in rows) else "single"
            coverage["ledger_only_reviews"] += 1
            per_review.append(
                _review_cost(None, run.run_id, work_item_id, rows, ledger_kind)
            )

    for bucket in [*by_phase.values(), *by_role.values()]:
        _finish_call_bucket(bucket)

    panel_count = sum(review.kind == "panel" for review in reviews)
    single_count = sum(review.kind == "single" for review in reviews)
    notes = [
        "Observational only: panels replace single-reviewer reviews, so this report has no "
        "same-diff counterfactual and cannot claim what a single reviewer would have caught.",
        "Ground truth about panel-approved changes that later needed fixes is external to these artifacts.",
    ]
    if panel_count < LOW_PANEL_SAMPLE:
        notes.append(
            f"Low sample: only {panel_count} panel review(s) appear in the selected runs; "
            "rates and averages are not yet stable."
        )
    if any(review["cost_is_floor"] for review in per_review):
        notes.append(
            "At least one review has missing, unmetered, or unpriced calls; affected cost totals are floors."
        )

    return {
        "runs": {
            "discovered": len(all_runs),
            "included": len(selected_runs),
            "limit": limit,
            "run_ids": [run.run_id for run in selected_runs],
        },
        "reviews": {
            "total": len(reviews),
            "panel": panel_count,
            "single": single_count,
        },
        "yield": _aggregate_yield(reviews, coverage),
        "cost": {
            "by_review_kind": _aggregate_review_costs(per_review),
            "by_role": {role: by_role[role] for role in sorted(by_role)},
            "by_phase": {phase: by_phase[phase] for phase in sorted(by_phase)},
            "per_review": per_review,
        },
        "coverage": coverage,
        "notes": notes,
    }


def _money(value: float | None, floor: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"${value:.4f}{'+' if floor else ''}"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_panel_report(report: dict[str, Any]) -> str:
    """Render a ``build_panel_report`` result as terminal-friendly Markdown.

    The renderer preserves the aggregation's honesty contract: it includes the selected
    sample size and observational limitation, calls out low panel sample sizes, and marks
    incomplete cost totals with ``+`` rather than presenting unknown spend as free.  It
    returns text only and does not read or modify run artifacts.
    """
    runs = report["runs"]
    reviews = report["reviews"]
    yield_data = report["yield"]
    cost = report["cost"]
    coverage = report["coverage"]
    lines = [
        "# Review panel report",
        "",
        f"Runs: {runs['included']} of {runs['discovered']} newest (limit {runs['limit']}) · "
        f"reviews: {reviews['total']} ({reviews['panel']} panel, {reviews['single']} single)",
        "",
    ]
    for note in report["notes"]:
        lines.append(f"> {note}")
    lines += [
        "",
        "## Yield",
        "",
        f"Panel summaries: {yield_data['summarized_panel_reviews']} of "
        f"{yield_data['panel_reviews']} panel reviews · distinct findings: "
        f"{yield_data['findings']} · shared across lenses: {yield_data['agreed']} "
        f"({_pct(yield_data['agreement_rate'])})",
        "",
        "| Lens | Reviews | Findings | Unique | Shared |",
        "|---|---:|---:|---:|---:|",
    ]
    if yield_data["by_lens"]:
        for lens, bucket in yield_data["by_lens"].items():
            lines.append(
                f"| {lens} | {bucket['reviews']} | {bucket['findings']} | "
                f"{bucket['unique']} | {bucket['shared']} |"
            )
    else:
        lines.append("| _(no summarized panel findings)_ | 0 | 0 | 0 | 0 |")
    verifiers = yield_data["verifiers"]
    cap = yield_data["verifier_cap"]
    lines += [
        "",
        f"Verifier verdicts: {verifiers['refuted']} refuted / "
        f"{verifiers['confirmed'] + verifiers['refuted']} conclusive "
        f"({_pct(verifiers['refute_rate'])}); {verifiers['inconclusive']} inconclusive.",
        f"Verifier cap: {cap['hits']} hit(s) / {cap['eligible_reviews']} summarized panels "
        f"({_pct(cap['hit_rate'])}); {cap['findings_dropped']} finding(s) left unverified.",
        "",
        "## Cost",
        "",
        "A trailing `+` means the displayed spend is a floor because some cost is unknown.",
        "",
        "| Review path | Reviews | With rows | Calls | Total | Average / recorded review |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for kind in ("panel", "single", "unknown"):
        bucket = cost["by_review_kind"][kind]
        if kind == "unknown" and not bucket["reviews"]:
            continue
        floor = bucket["cost_is_floor"]
        lines.append(
            f"| {kind} | {bucket['reviews']} | {bucket['reviews_with_cost_rows']} | "
            f"{bucket['invocations']} | {_money(bucket['total_cost_usd'], floor)} | "
            f"{_money(bucket['avg_cost_usd_per_recorded_review'], floor)} |"
        )
    lines += [
        "",
        "| Call role | Calls | Spend | Schema-retry calls | Retry rate | Retry turns |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for role in ("finder", "verifier", "single", "other-panel"):
        bucket = cost["by_role"].get(role)
        if bucket is None:
            continue
        lines.append(
            f"| {role} | {bucket['invocations']} | "
            f"{_money(bucket['total_cost_usd'], bucket['cost_is_floor'])} | "
            f"{bucket['schema_retry_calls']} | {_pct(bucket['schema_retry_rate'])} | "
            f"{bucket['schema_retries']} |"
        )
    lines += [
        "",
        "### Schema retries by phase",
        "",
        "The rate is the share of model calls in that phase that needed at least one corrective turn.",
        "",
        "| Phase | Calls | Retry calls | Retry rate | Retry turns |",
        "|---|---:|---:|---:|---:|",
    ]
    if cost["by_phase"]:
        for phase, bucket in cost["by_phase"].items():
            lines.append(
                f"| {phase} | {bucket['invocations']} | {bucket['schema_retry_calls']} | "
                f"{_pct(bucket['schema_retry_rate'])} | {bucket['schema_retries']} |"
            )
    else:
        lines.append("| _(none)_ | 0 | 0 | 0.0% | 0 |")
    lines += [
        "",
        "### Per review",
        "",
        "| Run | Task | Path | Calls | Spend | Schema retries |",
        "|---|---|---|---:|---:|---:|",
    ]
    if cost["per_review"]:
        for review in cost["per_review"]:
            lines.append(
                f"| {review['run_id']} | {review['task_id']} | {review['kind']} | "
                f"{review['invocations']} | "
                f"{_money(review['cost_usd'], review['cost_is_floor'])} | "
                f"{review['schema_retries']} |"
            )
    else:
        lines.append("| _(none)_ |  |  | 0 | n/a | 0 |")

    problems = {key: value for key, value in coverage.items() if value}
    lines += ["", "## Data coverage", ""]
    if problems:
        lines.extend(f"- `{key}`: {value}" for key, value in sorted(problems.items()))
    else:
        lines.append("No artifact gaps or marker disagreements detected.")
    lines.append("")
    return "\n".join(lines)
