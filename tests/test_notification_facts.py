"""The pure builders behind the notification payload's derived blocks (#524)."""

from __future__ import annotations

from orchestrator.notification_facts import (
    ISSUE_EXCERPT_MAX_CHARS,
    PR_MAX_FILES,
    REVIEW_MAX_FINDINGS,
    cost_rollup,
    issue_facts,
    pr_facts,
    review_facts,
    run_counts,
    run_duration_s,
    stage_facts,
)
from orchestrator.schemas.enums import ExecutionMode, Stage, StageStatus, TaskState
from orchestrator.schemas.status import Run, Task, TaskRef


def _task() -> Task:
    return Task(task_id="#9", run_id="r1", created_at="2026-09-30T00:00:00+00:00",
                updated_at="x", title="Demo")


def test_issue_excerpt_prefers_the_problem_section_and_drops_the_discussion() -> None:
    body = (
        "<!-- template hint -->\n# Title line\n\nIntro prose.\n\n## Problem\nIt repeats.\n\n"
        "## Acceptance\n- no repeats\n\n## Discussion\n### Comment by bob\nlgtm"
    )
    facts = issue_facts(body)
    assert facts == {"excerpt": "It repeats.", "acceptance": "- no repeats"}


def test_issue_excerpt_falls_back_to_the_opening_prose_and_is_bounded() -> None:
    assert issue_facts("Just do the thing.")["excerpt"] == "Just do the thing."
    assert issue_facts("") == {"excerpt": None, "acceptance": None}
    long = issue_facts("word " * 1000)["excerpt"]
    assert long is not None
    assert len(long) <= ISSUE_EXCERPT_MAX_CHARS + 2 and long.endswith(" …")


def test_stage_facts_lists_only_stages_that_ran_with_attribution() -> None:
    task = _task()
    task.pipeline = (Stage.INTAKE, Stage.SCOPE, Stage.IMPLEMENT)
    rec = task.stages[Stage.SCOPE]
    rec.status = StageStatus.COMPLETED
    rec.lane = ExecutionMode.HEADLESS
    rec.model = "claude-opus-5-5"
    rec.input_tokens, rec.output_tokens, rec.cost_usd = 100, 20, 0.5
    rec.started_at = "2026-09-30T00:00:00+00:00"
    rec.completed_at = "2026-09-30T00:01:30+00:00"
    facts = stage_facts(task)
    assert [f["stage"] for f in facts] == ["scope"]
    assert facts[0] | {"error": None} == {
        "stage": "scope", "status": "completed", "attempt": 0, "model": "claude-opus-5-5",
        "effort": None, "lane": "headless", "input_tokens": 100, "output_tokens": 20,
        "cost_usd": 0.5, "metered": True, "duration_s": 90.0, "error": None,
    }


def test_review_facts_bound_and_count_the_overflow() -> None:
    task = _task()
    task.review_cycles = 2
    output = {
        "approved": False,
        "issues": [{"severity": "high", "description": f"bug {i}"} for i in range(12)],
        "non_blocking": [{"title": f"nit {i}", "disposition": "file"} for i in range(11)]
        + ["not a dict"],
    }
    facts = review_facts(task, output)
    assert facts["approved"] is False and facts["cycles"] == 2
    assert len(facts["blocking"]) == REVIEW_MAX_FINDINGS and facts["blocking_omitted"] == 2
    assert facts["blocking"][0] == "high — bug 0"
    assert facts["non_blocking"][0] == {"title": "nit 0", "disposition": "file"}
    assert facts["non_blocking_omitted"] == 1


def test_pr_facts_bound_lists_and_keep_unknowns_unknown() -> None:
    files = [{"path": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(45)]
    facts = pr_facts({"url": "u", "files": files, "commits": []})
    assert len(facts["files"]) == PR_MAX_FILES and facts["files_omitted"] == 5
    # Nothing the source did not say is invented: no diffstat, no CI verdict.
    assert facts["additions"] is None and facts["checks"] is None


def test_cost_rollup_counts_unmetered_calls() -> None:
    rows = [{"cost_usd": 1.0, "metered": True}, {"cost_usd": 0.0, "metered": False}]
    assert cost_rollup(rows) == {"usd": 1.0, "invocations": 2, "unmetered_calls": 1}


def test_run_counts_and_duration() -> None:
    run = Run(run_id="r1", created_at="2026-09-30T00:00:00+00:00", updated_at="x",
              task_refs=[TaskRef(task_id=t, status_file=f"{t}.json", state=state)
                         for t, state in (("a", TaskState.COMPLETED), ("b", TaskState.FAILED),
                                          ("c", TaskState.COMPLETED))])
    assert run_counts(run) == {"completed": 2, "failed": 1, "total": 3}
    assert run_duration_s(run.created_at, "2026-09-30T01:00:00+00:00") == 3600.0
    assert run_duration_s("garbage", "2026-09-30T01:00:00+00:00") is None
