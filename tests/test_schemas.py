"""Contract tests for the frozen §4/§7 schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.schemas import (
    LANE_STAGES,
    STAGE_ORDER,
    ExecutionLane,
    ExecutionMode,
    LanePolicy,
    Provider,
    ResultStatus,
    Run,
    Stage,
    StageResult,
    StageStatus,
    Task,
    TaskRef,
    TaskState,
    WorkItem,
    compute_content_hash,
)
from orchestrator.schemas.work import LaneUsed

POLICY = LanePolicy(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE)


def _wi(attempt: int = 0, prompt: str = "do the thing") -> WorkItem:
    return WorkItem.create(
        id=f"w-{attempt}",
        run_id="r1",
        task_id="t1",
        stage=Stage.SCOPE,
        prompt=prompt,
        schema_ref="scope.json",
        model="claude-x",
        lane_policy=POLICY,
        created_at="2026-06-20T00:00:00Z",
        attempt=attempt,
    )


def test_content_hash_includes_attempt() -> None:
    # Same prompt, different attempt -> different hash (idempotency invariant, §4).
    a = _wi(attempt=0)
    b = _wi(attempt=1)
    assert a.content_hash != b.content_hash


def test_content_hash_is_deterministic() -> None:
    h1 = compute_content_hash(
        stage=Stage.SCOPE, prompt="p", schema_ref="s", model="m", lane_policy=POLICY, attempt=2
    )
    h2 = compute_content_hash(
        stage=Stage.SCOPE, prompt="p", schema_ref="s", model="m", lane_policy=POLICY, attempt=2
    )
    assert h1 == h2 and len(h1) == 64


def test_workitem_is_frozen() -> None:
    wi = _wi()
    with pytest.raises(ValidationError):
        wi.prompt = "mutated"  # type: ignore[misc]


def test_workitem_roundtrip() -> None:
    wi = _wi()
    assert WorkItem.model_validate_json(wi.model_dump_json()) == wi


def test_stageresult_ok_property() -> None:
    sr = StageResult(
        work_item_id="w-0",
        content_hash=_wi().content_hash,
        run_id="r1",
        task_id="t1",
        stage=Stage.SCOPE,
        model="claude-x",
        status=ResultStatus.SUCCESS,
        lane_used=LaneUsed(
            execution_mode=ExecutionMode.INTERACTIVE,
            provider=Provider.CLAUDE,
            invocation="agent(model=claude-x)",
        ),
        completed_at="2026-06-20T00:01:00Z",
    )
    assert sr.ok
    assert not sr.model_copy(update={"status": ResultStatus.TIMEOUT}).ok


def test_task_initializes_all_six_stages_with_started_at() -> None:
    t = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    assert list(t.stages.keys()) == list(Stage)
    # started_at ALWAYS present (null until running) — fixes the as-built omission.
    for rec in t.stages.values():
        dumped = rec.model_dump()
        assert "started_at" in dumped and dumped["started_at"] is None
        assert rec.status is StageStatus.PENDING


def test_lane_stage_sets() -> None:
    assert Stage.SIMPLIFY not in LANE_STAGES[ExecutionLane.FULL]
    assert set(LANE_STAGES[ExecutionLane.FULL]) == set(STAGE_ORDER) - {Stage.SIMPLIFY}
    assert Stage.SCOPE not in LANE_STAGES[ExecutionLane.LITE]
    assert Stage.TEST not in LANE_STAGES[ExecutionLane.MICRO]
    # intake/implement/deliver/review always run
    for lane in ExecutionLane:
        for s in (Stage.INTAKE, Stage.IMPLEMENT, Stage.DELIVER, Stage.REVIEW):
            assert s in LANE_STAGES[lane]


def test_run_progress_is_derived() -> None:
    run = Run(
        run_id="r1",
        created_at="x",
        updated_at="x",
        task_refs=[
            TaskRef(task_id="t1", status_file="f1", state=TaskState.COMPLETED),
            TaskRef(task_id="t2", status_file="f2", state=TaskState.RUNNING),
            TaskRef(task_id="t3", status_file="f3", state=TaskState.BLOCKED),
        ],
    )
    p = run.progress()
    assert p.total == 3 and p.completed == 1 and p.running == 1 and p.blocked == 1
