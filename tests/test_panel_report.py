"""Cross-run panel efficacy reporting (#286)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.panel_report import build_panel_report, render_panel_report


def _run(root: Path, run_id: str, mtime: float) -> Path:
    run = root / run_id
    run.mkdir()
    status = run / f"status-{run_id}.json"
    status.write_text(json.dumps({
        "document_type": "run", "run_id": run_id, "state": "completed",
    }))
    os.utime(status, (mtime, mtime))
    return run


def _review(
    run: Path,
    *,
    task: str,
    work_item_id: str,
    sub_results: bool,
    panel_summary: dict | None = None,
) -> None:
    stages = run / "stages" / task
    stages.mkdir(parents=True, exist_ok=True)
    payload = {
        "work_item_id": work_item_id,
        "stage": "review",
        "task_id": task,
        "attempt": 0,
        "status": "success",
    }
    if sub_results:
        payload["sub_results"] = {"findings_by_lens": {}, "verdicts": []}
    if panel_summary is not None:
        payload["panel_summary"] = panel_summary
    (stages / "06-review.json").write_text(json.dumps(payload))


def _ledger(run: Path, rows: list[dict], *, trailing: str = "") -> None:
    text = "".join(json.dumps(row) + "\n" for row in rows) + trailing
    (run / "stage-costs.jsonl").write_text(text)


def _row(
    run_id: str,
    work_item_id: str,
    cost: float,
    *,
    phase: str | None = None,
    schema_retries: int = 0,
    metered: bool = True,
) -> dict:
    row = {
        "run_id": run_id,
        "task_id": work_item_id,
        "work_item_id": work_item_id,
        "stage": "review",
        "status": "success",
        "cost_usd": cost,
        "priced": True,
        "metered": metered,
        "schema_retries": schema_retries,
    }
    if phase is not None:
        row["phase"] = phase
    return row


def test_aggregates_panel_yield_and_cost_against_plain_reviews(tmp_path: Path) -> None:
    panel = _run(tmp_path, "panel-run", 200)
    single = _run(tmp_path, "single-run", 100)
    summary = {
        "lenses": {
            "code": {"total": 2, "unique": 1, "shared": 1},
            "tests": {"total": 1, "unique": 0, "shared": 1},
        },
        "findings": 2,
        "agreed": 1,
        "verdicts": {"confirmed": 1, "refuted": 1},
        "inconclusive": 1,
        "cap_hit": True,
        "cap_dropped": 3,
    }
    _review(panel, task="t-panel", work_item_id="wi-panel", sub_results=True,
            panel_summary=summary)
    _review(single, task="t-single", work_item_id="wi-single", sub_results=False)
    _ledger(panel, [
        _row("panel-run", "wi-panel", 1.0, phase="find:code", schema_retries=1),
        _row("panel-run", "wi-panel", 2.0, phase="find:tests"),
        _row("panel-run", "wi-panel", 3.0, phase="verify:0"),
    ])
    _ledger(single, [_row("single-run", "wi-single", 0.5)])

    report = build_panel_report(tmp_path)

    assert report["runs"]["run_ids"] == ["panel-run", "single-run"]
    assert report["reviews"] == {"total": 2, "panel": 1, "single": 1}
    yield_data = report["yield"]
    assert yield_data["by_lens"] == {
        "code": {"reviews": 1, "findings": 2, "unique": 1, "shared": 1},
        "tests": {"reviews": 1, "findings": 1, "unique": 0, "shared": 1},
    }
    assert yield_data["agreement_rate"] == 0.5
    assert yield_data["verifiers"] == {
        "confirmed": 1, "refuted": 1, "inconclusive": 1, "refute_rate": 0.5,
    }
    assert yield_data["verifier_cap"] == {
        "hits": 1, "eligible_reviews": 1, "hit_rate": 1.0, "findings_dropped": 3,
    }

    costs = report["cost"]
    assert costs["by_review_kind"]["panel"]["total_cost_usd"] == 6.0
    assert costs["by_review_kind"]["panel"]["avg_cost_usd_per_recorded_review"] == 6.0
    assert costs["by_review_kind"]["single"]["total_cost_usd"] == 0.5
    assert costs["by_role"]["finder"]["total_cost_usd"] == 3.0
    assert costs["by_role"]["verifier"]["total_cost_usd"] == 3.0
    assert costs["by_phase"]["find:code"]["schema_retry_rate"] == 1.0
    assert costs["by_phase"]["find:code"]["schema_retries"] == 1
    assert report["coverage"]["marker_disagreements"] == 0


def test_limit_selects_newest_run_before_aggregation(tmp_path: Path) -> None:
    older = _run(tmp_path, "older", 100)
    newer = _run(tmp_path, "newer", 200)
    _review(older, task="old", work_item_id="old", sub_results=False)
    _review(newer, task="new", work_item_id="new", sub_results=True, panel_summary={})

    report = build_panel_report(tmp_path, limit=1)

    assert report["runs"] == {
        "discovered": 2, "included": 1, "limit": 1, "run_ids": ["newer"],
    }
    assert report["reviews"] == {"total": 1, "panel": 1, "single": 0}
    with pytest.raises(ValueError, match="at least 1"):
        build_panel_report(tmp_path, limit=0)


def test_partial_artifacts_are_reported_without_hiding_valid_data(tmp_path: Path) -> None:
    run = _run(tmp_path, "r1", 100)
    _review(run, task="good", work_item_id="good", sub_results=False)
    broken = run / "stages" / "broken"
    broken.mkdir(parents=True)
    (broken / "06-review.json").write_text("{torn")
    _ledger(
        run,
        [
            _row("r1", "good", 0.0, metered=False),
            _row("another-run", "foreign", 99.0),
        ],
        trailing="{torn\n",
    )

    report = build_panel_report(tmp_path)

    assert report["reviews"] == {"total": 1, "panel": 0, "single": 1}
    assert report["coverage"]["unreadable_review_records"] == 1
    assert report["coverage"]["malformed_ledger_rows"] == 1
    assert report["coverage"]["foreign_ledger_rows"] == 1
    single = report["cost"]["by_review_kind"]["single"]
    assert single["total_cost_usd"] == 0.0
    assert single["unmetered_reviews"] == 1
    assert single["cost_is_floor"] is True
    assert any("cost totals are floors" in note for note in report["notes"])


def test_marker_disagreement_and_ledger_only_review_are_visible(tmp_path: Path) -> None:
    run = _run(tmp_path, "r1", 100)
    _review(run, task="t1", work_item_id="stage-panel", sub_results=True,
            panel_summary={})
    _ledger(run, [
        _row("r1", "stage-panel", 1.0),  # plain ledger marker disagrees with stage marker
        _row("r1", "ledger-panel", 2.0, phase="verify:0"),
    ])

    report = build_panel_report(tmp_path)

    assert report["coverage"]["marker_disagreements"] == 1
    assert report["coverage"]["ledger_only_reviews"] == 1
    per_review = report["cost"]["per_review"]
    assert per_review[0]["stage_marker"] == "panel"
    assert per_review[0]["ledger_marker"] == "single"
    assert per_review[1]["kind"] == "panel"


def test_render_always_states_observational_limit_and_low_sample(tmp_path: Path) -> None:
    rendered = render_panel_report(build_panel_report(tmp_path))

    assert "# Review panel report" in rendered
    assert "Observational only" in rendered
    assert "no same-diff counterfactual" in rendered
    assert "Low sample: only 0 panel review(s)" in rendered
    assert "panel-approved changes that later needed fixes is external" in rendered
    assert "### Schema retries by phase" in rendered


def test_panel_report_cli_needs_only_runs_root(tmp_path: Path, capsys) -> None:
    run = _run(tmp_path, "r1", 100)
    _review(run, task="t1", work_item_id="wi", sub_results=False)

    rc = main(["--root", str(tmp_path), "panel-report", "--limit", "1"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "Runs: 1 of 1 newest (limit 1)" in output
    assert "reviews: 1 (0 panel, 1 single)" in output
