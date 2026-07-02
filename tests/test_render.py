"""Markdown renderers + engine writes cost-summary.md / index.md / per-stage .md."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import (
    render_cost_report,
    render_cost_summary,
    render_stage,
    render_task_index,
)
from orchestrator.schemas.status import Task
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def test_render_cost_summary_markdown() -> None:
    summary = {
        "total_invocations": 2,
        "total_cost_usd": 1.5,
        "by_model": {"claude-opus-4-8": {"invocations": 2, "input_tokens": 100, "output_tokens": 50, "cost_usd": 1.5}},
    }
    md = render_cost_summary("r1", summary)
    assert "# Cost summary — r1" in md
    assert "$1.5000" in md and "claude-opus-4-8" in md and "| Model |" in md


def test_render_stage_renders_structured_output_as_readable_markdown() -> None:
    payload = {
        "stage": "scope", "task_id": "#9", "attempt": 0, "status": "success", "outcome": "stage_completed",
        "model": "claude-opus-4-8", "lane_used": {"execution_mode": "interactive", "provider": "claude"},
        "cost_usd": 0.66, "structured_output": {"feasible": True, "plan": ["hoist it"]},
        "raw_output": "did the thing", "error": None, "completed_at": "t",
    }
    md = render_stage(payload)
    assert "# scope — #9" in md and "interactive:claude" in md and "$0.6600" in md
    # structured output is readable bullets, NOT an embedded JSON blob
    assert "```json" not in md and '"feasible"' not in md
    assert "- **feasible:** yes" in md
    assert "- **plan:**" in md and "  - hoist it" in md
    assert "## Commentary" in md and "did the thing" in md


def test_render_task_index_lists_six_stages() -> None:
    t = Task(task_id="#9", run_id="r1", created_at="x", updated_at="x", title="Demo")
    md = render_task_index(t)
    assert "# Task #9 — Demo" in md
    for stage in ("intake", "scope", "implement", "test", "deliver", "review"):
        assert stage in md


def test_render_cost_report_markdown() -> None:
    analysis = {
        "total_cost_usd": 6.75,
        "by_stage": {"intake": {"invocations": 1, "input_tokens": 0, "output_tokens": 0,
                                "cache_read_tokens": 0, "cache_write_tokens": 1_000_000, "cost_usd": 6.25}},
        "by_task": {"t1": {"invocations": 2, "input_tokens": 0, "output_tokens": 0,
                           "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000, "cost_usd": 6.75}},
        "session_reuse": {
            "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
            "fresh_input_tokens": 0, "output_tokens": 0, "cache_hit_ratio": 0.5,
            "cache_read_savings_usd": 4.5, "cache_write_premium_usd": 1.25,
            "net_win_usd": 3.25, "uncached_cost_usd": 10.0, "win_pct": 32.5,
            "unpriced_models": [],
        },
    }
    md = render_cost_report("r1", analysis)
    assert "# Cost report — r1" in md
    assert "$3.2500" in md and "32.5%" in md  # the headline win
    assert "50.0%" in md  # cache hit ratio
    assert "## By stage" in md and "## By task" in md
    assert "`intake`" in md


def test_render_cost_report_defensive_on_empty() -> None:
    # zeroed analysis (no rows) must render without crashing
    md = render_cost_report("r1", {"total_cost_usd": 0.0, "by_stage": {}, "by_task": {},
                                   "session_reuse": {}})
    assert "# Cost report — r1" in md and "$0.0000" in md


def test_engine_writes_markdown_artifacts(tmp_path, project) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))

    assert (tmp_path / "cost-summary.md").exists()
    assert "# Cost summary" in (tmp_path / "cost-summary.md").read_text()
    report = tmp_path / "cost-report.md"
    assert report.exists()
    report_text = report.read_text()
    assert "Session-reuse win" in report_text and "## By stage" in report_text
    stage_dir = tmp_path / "stages" / "t1"
    assert (stage_dir / "index.md").exists()
    md_files = sorted(p.name for p in stage_dir.glob("*.md") if p.name != "index.md")
    assert md_files == [
        "01-intake.md", "02-scope.md", "03-implement.md",
        "04-test.md", "05-deliver.md", "06-review.md",
    ]
    # the per-stage md carries the embedded structured substance
    assert "pr_url" in (stage_dir / "05-deliver.md").read_text()
