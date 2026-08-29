"""Aggregate-cost honesty on the surfaces #319 did not reach (#331).

#319 taught ``cost-summary.md`` / ``cost-report.md`` / ``--budget-usd`` that an unmetered
call has UNKNOWN cost — it lands in the ledger at ``cost_usd: 0.0``, so summing it in
silently understates spend while looking exact. Four surfaces kept summing it anyway:

* the multi-run text board (``orchestrator dashboard`` / ``watch``) — per-run cost cell AND
  the board-wide ``spend:`` header;
* the web skin's JS templates (per-run cell + header);
* ``render_by_effort`` (cost-by-effort.md), the #96 effort-tuning evidence table — where a
  silently-cheap-looking stage would actively mislead a defaults decision.

These pin the shared convention: all metered → the plain figure; some unmetered → ``≥$X``
(a floor); all unmetered → ``n/a (unmetered)`` (unknown, NOT $0).
"""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import dashboard_snapshot, render_dashboard
from orchestrator.engine import Engine
from orchestrator.render import aggregate_cost_cell, render_by_effort
from orchestrator.status_store import StatusStore
from orchestrator.web_dashboard import INDEX_HTML
from tests.conftest import FakeProject

# --- builders (mirror test_dashboard.py) ------------------------------------------------


def _engine(run_root, **kw) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    return Engine(
        StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), FakeProject(), **kw
    )


def _run_with_rows(tmp_path, run_id: str, rows: list[dict]):
    """A real run store whose ledger holds exactly ``rows``."""
    rr = tmp_path / run_id
    eng = _engine(rr)
    eng.create_run(run_id)
    eng.add_task(run_id, "t1")
    (rr / "stage-costs.jsonl").write_text(
        "".join(json.dumps({"model": "m", "run_id": run_id, **r}) + "\n" for r in rows)
    )
    return rr


def _snapshot(root, **kw):
    kw.setdefault("engine_factory", lambda run_root, project_ref=None: _engine(run_root))
    kw.setdefault("clock", lambda: 1_000_000.0)
    return dashboard_snapshot(root, **kw)


# --- the shared cell convention ---------------------------------------------------------


def test_aggregate_cost_cell_three_cases() -> None:
    assert aggregate_cost_cell(1.25, 0, 4) == "$1.2500"
    assert aggregate_cost_cell(1.25, 1, 4) == "≥$1.2500"
    assert aggregate_cost_cell(0.0, 4, 4) == "n/a (unmetered)"
    # No cost data at all is a different thing from unmetered.
    assert aggregate_cost_cell(None, 0, 0) == "—"


def test_aggregate_cost_cell_unmetered_without_invocation_count_is_a_floor() -> None:
    # Defensive: a caller that knows the unmetered count but not the total must still get a
    # qualified figure, never a bare one.
    assert aggregate_cost_cell(2.0, 3, 0) == "≥$2.0000"


# --- by_effort: the ledger carries the flag through the grouping ------------------------


def test_by_effort_groups_carry_the_unmetered_count(tmp_path) -> None:
    path = tmp_path / "stage-costs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        {"stage": "implement", "effort": "high", "model": "m", "cost_usd": 2.0, "metered": True},
        {"stage": "implement", "effort": "high", "model": "m", "cost_usd": 0.0, "metered": False},
        {"stage": "deliver", "effort": "low", "model": "m", "cost_usd": 0.5},  # flag absent
    ]))
    groups = {(g["stage"], g["effort"]): g for g in CostLedger(path).by_effort()}
    assert groups[("implement", "high")]["unmetered"] == 1
    assert groups[("implement", "high")]["invocations"] == 2
    # A row predating the flag counts as metered (``is False``, not falsy) — same rule as
    # summary()/analysis(), so old ledgers don't retroactively grow unmetered calls.
    assert groups[("deliver", "low")]["unmetered"] == 0


def test_render_by_effort_qualifies_partly_and_wholly_unmetered_groups() -> None:
    agg = [
        {"stage": "implement", "effort": "high", "model": "opus", "invocations": 3,
         "cost_usd": 5.0, "unmetered": 1, "avg_duration_s": 15.0,
         "retry_rate": 0.0, "failure_rate": 0.0},
        {"stage": "review", "effort": "medium", "model": "sonnet", "invocations": 2,
         "cost_usd": 0.0, "unmetered": 2, "avg_duration_s": 3.0,
         "retry_rate": 0.0, "failure_rate": 0.0},
        {"stage": "deliver", "effort": "low", "model": "sonnet", "invocations": 1,
         "cost_usd": 0.75, "unmetered": 0, "avg_duration_s": 4.0,
         "retry_rate": 0.0, "failure_rate": 0.0},
    ]
    md = render_by_effort("r1", agg)
    assert "≥$5.0000" in md  # partly unmetered -> a floor
    assert "n/a (unmetered)" in md  # wholly unmetered -> unknown, NOT $0.0000
    assert "$0.0000" not in md
    assert "| $0.7500 |" in md  # fully metered group keeps the plain figure
    assert "3 call(s) in this table are unmetered" in md


def test_render_by_effort_clean_run_has_no_caveat() -> None:
    agg = [
        {"stage": "implement", "effort": "high", "model": "opus", "invocations": 2,
         "cost_usd": 5.0, "unmetered": 0, "avg_duration_s": 15.0,
         "retry_rate": 0.0, "failure_rate": 0.0},
    ]
    md = render_by_effort("r1", agg)
    assert "| $5.0000 |" in md
    assert "unmetered" not in md and "≥$" not in md


# --- the multi-run board ----------------------------------------------------------------


def test_snapshot_row_and_header_carry_unmetered_counts(tmp_path) -> None:
    _run_with_rows(tmp_path, "r1", [
        {"cost_usd": 1.5, "metered": True},
        {"cost_usd": 0.0, "metered": False},
    ])
    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["cost_usd"] == 1.5
    assert row["unmetered_calls"] == 1 and row["total_invocations"] == 2
    assert snap["header"]["unmetered_calls"] == 1
    assert snap["header"]["total_invocations"] == 2


def test_board_qualifies_a_partly_unmetered_run(tmp_path) -> None:
    _run_with_rows(tmp_path, "r1", [
        {"cost_usd": 1.5, "metered": True},
        {"cost_usd": 0.0, "metered": False},
    ])
    out = render_dashboard(_snapshot(tmp_path))
    assert "≥$1.5000" in out  # the per-run cell
    assert "spend: ≥$1.5000 (1 unmetered call(s) of unknown cost excluded)" in out
    assert "$1.5000 across" not in out  # never the bare, confident figure


def test_board_says_n_a_when_every_call_is_unmetered(tmp_path) -> None:
    _run_with_rows(tmp_path, "r1", [
        {"cost_usd": 0.0, "metered": False},
        {"cost_usd": 0.0, "metered": False},
    ])
    out = render_dashboard(_snapshot(tmp_path))
    assert "n/a (unmetered)" in out  # the per-run cell — not "$0.0000"
    assert "spend: n/a — all 2 call(s) unmetered" in out
    assert "$0.0000" not in out


def test_board_keeps_the_plain_figure_when_everything_is_metered(tmp_path) -> None:
    _run_with_rows(tmp_path, "r1", [{"cost_usd": 2.25, "metered": True}])
    out = render_dashboard(_snapshot(tmp_path))
    assert "$2.2500" in out and "≥$" not in out and "unmetered" not in out


def test_unreadable_run_contributes_no_counts(tmp_path) -> None:
    # An unreadable run has no cost data at all — a different claim from "unmetered" (calls
    # happened, cost unknown). Its None counts must not poison the header's sums, and the
    # header must not start claiming unmetered calls that were never observed.
    rr = tmp_path / "r1"
    _engine(rr).create_run("r1")
    (rr / "status-r1.json").write_text("{not json")
    snap = _snapshot(tmp_path)
    assert snap["runs"][0]["unmetered_calls"] is None
    assert snap["header"]["unmetered_calls"] == 0
    out = render_dashboard(snap)
    assert "spend: $0.0000 across 1 run(s)" in out
    assert "<unreadable status>" in out


# --- the web skin -----------------------------------------------------------------------


def test_web_page_qualifies_cost_the_same_way() -> None:
    # The JS is a twin of aggregate_cost_cell / the header rule; pin both so the two skins
    # can't drift apart silently.
    assert "function costCell(" in INDEX_HTML
    assert "n/a (unmetered)" in INDEX_HTML
    assert '"≥$" + cost.toFixed(4)' in INDEX_HTML
    assert "row.unmetered_calls" in INDEX_HTML
    assert "hd.unmetered_calls" in INDEX_HTML
    assert "all " + '" + unmetered + "' + " call(s) unmetered" in INDEX_HTML
    # The old unconditional header figure is gone.
    assert '"  ·  spend $" + (hd.total_spend_usd || 0).toFixed(4)' not in INDEX_HTML
