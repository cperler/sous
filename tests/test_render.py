"""Markdown renderers + engine writes cost-summary.md / index.md / per-stage .md."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import (
    render_by_effort,
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


def test_render_stage_prose_commentary_is_rendered_verbatim() -> None:
    # A normal raw_output (the model's final text since #93) is shown as-is — the guard must
    # not touch prose.
    payload = {
        "stage": "review", "task_id": "#9", "attempt": 0, "status": "success",
        "outcome": "stage_completed", "model": "m", "lane_used": {}, "cost_usd": 0.1,
        "raw_output": "I reviewed the change and it looks correct.\nNo blocking issues.",
        "error": None, "completed_at": "t",
    }
    md = render_stage(payload)
    assert "I reviewed the change and it looks correct." in md
    assert "Full provider event stream" not in md  # not a stream → no pointer


def test_render_stage_guards_against_a_raw_event_stream_payload() -> None:
    # #93 belt-and-suspenders: an OLD-style (pre-fix) or replayed payload whose raw_output is
    # still a raw JSONL event stream must NOT be dumped into Commentary — the renderer extracts
    # the readable text and points at the retained full stream instead.
    import json

    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "here is the analysis"}]}}),
        json.dumps({"type": "result", "result": "the readable final answer", "usage": {}}),
    ]) + "\n"
    payload = {
        "stage": "review", "task_id": "#9", "attempt": 0, "status": "success",
        "outcome": "stage_completed", "model": "m", "lane_used": {}, "cost_usd": 0.1,
        "raw_output": stream, "error": None, "completed_at": "t",
        "stream_files": {"stream": "stages/9/review-attempt0.stream.jsonl"},
    }
    md = render_stage(payload)
    assert "## Commentary" in md
    assert "the readable final answer" in md  # extracted, not the stream
    assert '"type":' not in md and '"type": ' not in md  # no raw JSONL leaked through
    assert "Full provider event stream: `stages/9/review-attempt0.stream.jsonl`" in md


def test_render_stage_stream_payload_without_extractable_text() -> None:
    import json

    stream = "\n".join(json.dumps({"type": "system", "n": i}) for i in range(4)) + "\n"
    payload = {
        "stage": "review", "task_id": "#9", "attempt": 0, "status": "failure",
        "outcome": "stage_failed", "model": "m", "lane_used": {}, "cost_usd": 0.1,
        "raw_output": stream, "error": "boom", "completed_at": "t",
        "stream_files": {"stream": "stages/9/review-attempt0.stream.jsonl"},
    }
    md = render_stage(payload)
    assert "no final text" in md and '"type":' not in md
    assert "Full provider event stream:" in md


def test_render_stage_titled_findings_are_readable_blocks() -> None:
    payload = {
        "stage": "review", "task_id": "#9", "attempt": 0, "status": "success",
        "outcome": "task_completed", "model": "claude-sonnet-4-6",
        "lane_used": {"execution_mode": "interactive", "provider": "claude"}, "cost_usd": 0.0,
        "structured_output": {
            "approved": True,
            "issues": [],
            "non_blocking": [
                {"title": "First finding", "detail": "why one"},
                {"title": "Second finding", "detail": "why two"},
            ],
            "improvement": {"title": "Do the thing", "detail": "because reasons"},
        },
        "raw_output": None, "error": None, "completed_at": "t",
    }
    md = render_stage(payload)
    lines = md.splitlines()
    # A titled dict leads with its title in bold, and its prose detail is a continuation
    # line (NOT a `- **detail:**` bullet).
    assert "  - **First finding**" in md
    assert "    why one" in md and "- **detail:**" not in md
    # The two findings are separated by a blank line so they don't blur together.
    i1 = lines.index("  - **First finding**")
    i2 = lines.index("  - **Second finding**")
    assert "" in lines[i1:i2], "expected a blank line between findings"
    # The improvement object is titled too.
    assert "  - **Do the thing**" in md and "    because reasons" in md


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


def test_render_by_effort_markdown() -> None:
    agg = [
        {"stage": "deliver", "effort": "low", "model": "claude-sonnet-4-6", "invocations": 3,
         "cost_usd": 0.75, "avg_duration_s": 4.2, "retry_rate": 0.33, "failure_rate": 0.0},
        {"stage": "implement", "effort": "high", "model": "claude-opus-4-8", "invocations": 2,
         "cost_usd": 5.0, "avg_duration_s": 15.0, "retry_rate": 0.5, "failure_rate": 0.5},
    ]
    md = render_by_effort("r1", agg)
    assert "# Cost by effort — r1" in md
    assert "| Stage | Effort | Model | Calls | Cost (USD) | Avg dur (s) | Retry rate | Failure rate |" in md
    assert "`deliver`" in md and "`low`" in md and "`claude-sonnet-4-6`" in md
    assert "$5.0000" in md and "15.0" in md
    assert "50%" in md  # retry/failure rates rendered as percentages


def test_render_by_effort_defensive_on_empty() -> None:
    md = render_by_effort("r1", [])
    assert "# Cost by effort — r1" in md


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
