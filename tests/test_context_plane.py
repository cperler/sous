"""Task context plane — the fold (review Phase B #5; 2026-07-01 design note).

Every stage's whitelisted structured-output fields are folded into an engine-owned,
bounded, injective `task.context`. Correctness is derived from durable results only.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Stage
from orchestrator.state_machine import (
    _MAX_CONTEXT_BYTES,
    _MAX_ITEM_STR,
    _MAX_LIST,
    _MAX_STR,
    CONTEXT_KEYS,
    _absorb_outputs,
    _context_bytes,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_context_keys_are_injective_across_stages() -> None:
    seen: set[str] = set()
    for keys in CONTEXT_KEYS.values():
        for k in keys:
            assert k not in seen, f"context key {k!r} written by more than one stage"
            seen.add(k)


def test_full_run_folds_every_stage_into_context(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    ctx = eng.store.load_task("r1", "t1").context
    # scope's plan reaches the task context (was dropped on the floor before)
    assert ctx["plan"] == ["subtask-1"]
    # intake's worktree/branch, implement's summary, test's notes, deliver's pr all folded
    assert ctx["branch"] == "issue-42" and ctx["worktree"] == "/wt/42"
    assert ctx["summary"] == "done" and ctx["files_changed"] == ["a.py"]
    assert ctx["tests_meaningful"] is True
    assert ctx["validation_notes"] == "asserts the changed behavior"
    assert ctx["pr_url"].endswith("/1234") and ctx["pr_number"] == 1234
    # context survives on the persisted task doc (resume/replay safe)
    assert "plan" in eng.store.load_task("r1", "t1").context


def test_fold_is_tolerant_of_missing_keys() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    res = make_result_stub(Stage.SCOPE, {"feasible": True})  # no "plan" key
    _absorb_outputs(task, res)
    assert task.context == {}  # nothing to fold, no crash


def test_string_and_list_values_are_capped() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    big_summary = "x" * (_MAX_STR + 500)
    big_plan = [f"step-{i}" for i in range(_MAX_LIST + 20)]
    _absorb_outputs(task, make_result_stub(Stage.IMPLEMENT, {"summary": big_summary,
                                                             "files_changed": []}))
    _absorb_outputs(task, make_result_stub(Stage.SCOPE, {"plan": big_plan}))
    assert len(task.context["summary"]) <= _MAX_STR + len(" … [truncated]")
    assert task.context["summary"].endswith("[truncated]")
    assert len(task.context["plan"]) == _MAX_LIST + 1  # kept + the "… (N more)" marker
    assert task.context["plan"][-1].startswith("… (")


def test_context_ceiling_drops_lowest_priority_stages_first() -> None:
    from orchestrator.schemas.status import Task

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    # intake contributes a little; review contributes a maxed list (~40*500 ≈ 20KB) that
    # alone blows the 16KB ceiling, so the ceiling must drop review (lowest priority)
    # while keeping intake (highest priority).
    _absorb_outputs(task, make_result_stub(Stage.INTAKE, {"branch": "b", "worktree": "/wt"}))
    huge_issues = ["z" * _MAX_ITEM_STR for _ in range(_MAX_LIST)]
    _absorb_outputs(task, make_result_stub(Stage.REVIEW, {"issues": huge_issues}))
    assert _context_bytes(task.context) <= _MAX_CONTEXT_BYTES  # ceiling held
    assert "branch" in task.context and "worktree" in task.context  # earliest kept
    assert "issues" not in task.context  # review dropped first (lowest priority)


def _prompt_at(eng: Engine, stage: Stage, run="r1", task="t1") -> str:
    """Drive the task until `stage` is dispatched; return that WorkItem's prompt."""
    while (w := eng.next_work(run, task)) is not None:
        if w.stage is stage:
            return w.prompt
        eng.record(run, make_result(w))
    raise AssertionError(f"stage {stage} never dispatched")


def test_implement_prompt_contains_scope_plan(tmp_path, project) -> None:
    # review Phase B acceptance check: scope's plan reaches implement's prompt
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.IMPLEMENT)
    assert "subtask-1" in prompt  # the plan scope produced (_default_output SCOPE)
    assert "Context from earlier stages" in prompt


def test_review_prompt_contains_pr_url(tmp_path, project) -> None:
    # review Phase B acceptance check: review's prompt is given task.pr_url
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.REVIEW)
    assert "/1234" in prompt  # deliver's pr_url, folded into context


def test_prompt_orders_stable_parts_before_per_task_context(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    prompt = _prompt_at(eng, Stage.IMPLEMENT)
    # stable project commands + task spec precede the growing per-task context block
    i_cmds = prompt.index("## Project commands")
    i_task = prompt.index("## Task t1")
    i_ctx = prompt.index("## Context from earlier stages")
    assert i_cmds < i_task < i_ctx
    # the decorative-no-more ProjectConfig commands are actually in the prompt now
    assert "install" in prompt and "typecheck" in prompt


# --- helpers ---------------------------------------------------------------
def make_result_stub(stage: Stage, output: dict):
    from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
    from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage

    return StageResult(
        work_item_id="wi", content_hash="h", run_id="r", task_id="t", stage=stage,
        attempt=0, model="claude-opus-4-8", status=ResultStatus.SUCCESS,
        structured_output=output,
        lane_used=LaneUsed(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE,
                           invocation="x"),
        token_usage=TokenUsage(), completed_at="2026-07-01T00:00:00Z",
    )
