"""SCOPE child-task decomposition and the attributed simplification pass (#60)."""

from __future__ import annotations

from adapters.project.base import TaskSpec
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    ImplementationBudget,
    QualityTier,
    ResultStatus,
    Stage,
    TaskState,
)
from orchestrator.status_store import StatusStore
from tests.conftest import FakeTaskSource, make_result


class CreatingSource(FakeTaskSource):
    def __init__(self) -> None:
        super().__init__()
        self.created: dict[str, TaskSpec] = {}
        self.create_calls = 0

    def create_task(self, title: str, body: str, labels=None) -> str:
        self.create_calls += 1
        ref = f"child-{len(self.created) + 1}"
        self.created[ref] = TaskSpec(task_id=ref, title=title, body=body)
        return ref

    def resolve(self, task_id: str) -> TaskSpec:
        return self.created.get(task_id) or super().resolve(task_id)

    def list_tasks(self, label=None, limit=50) -> list[TaskSpec]:
        return [*self.created.values(), *super().list_tasks(label=label, limit=limit)][:limit]


class FailOnceCreatingSource(CreatingSource):
    def __init__(self) -> None:
        super().__init__()
        self.fail_on_call = 2

    def create_task(self, title: str, body: str, labels=None) -> str:
        if self.create_calls + 1 == self.fail_on_call:
            self.create_calls += 1
            self.fail_on_call = 0
            raise RuntimeError("temporary filing failure")
        return super().create_task(title, body, labels)


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "cost.jsonl"), project)


def _scope_output() -> dict:
    return {
        "feasible": True,
        "plan": ["split work"],
        "subtasks": [
            {
                "id": "api",
                "description": "Build the API seam",
                "agent": "backend",
                "quality_tier": "full",
                "implementation_budget": "short",
            },
            {
                "id": "docs",
                "description": "Document the seam",
                "quality_tier": "light",
            },
            {
                "id": "client",
                "description": "Consume the API seam",
                "quality_tier": "none",
                "depends_on": ["api"],
            },
        ],
    }


def _decompose(tmp_path, project) -> tuple[Engine, CreatingSource]:
    source = CreatingSource()
    project._task_source = source
    project.agent_for = lambda stage, role=None: f"{role}-agent" if role else None
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    scope = eng.next_work("r1", "parent")
    result = eng.record("r1", make_result(scope, structured_output=_scope_output()))
    assert result["outcome"] == "task_decomposed"
    return eng, source


def _finish(eng: Engine, task_id: str) -> list[Stage]:
    stages: list[Stage] = []
    while (work := eng.next_work("r1", task_id)) is not None:
        stages.append(work.stage)
        eng.record("r1", make_result(work))
    return stages


def test_scope_emits_children_with_controls_and_existing_dag(tmp_path, project) -> None:
    eng, source = _decompose(tmp_path, project)
    parent = eng.store.load_task("r1", "parent")
    assert parent.state is TaskState.BLOCKED
    assert eng.next_work("r1", "parent") is None
    assert parent.decomposition_mapping == {
        "api": "child-1", "docs": "child-2", "client": "child-3"
    }
    assert eng.store.load_run("r1").dependency_graph == {
        "parent": ["child-2", "child-3"],
        "child-1": [],
        "child-2": [],
        "child-3": ["child-1"],
    }

    api = eng.store.load_task("r1", "child-1")
    docs = eng.store.load_task("r1", "child-2")
    client = eng.store.load_task("r1", "child-3")
    assert api.agent_role == "backend"
    assert api.quality_tier is QualityTier.FULL
    assert api.implementation_budget is ImplementationBudget.SHORT
    assert Stage.SIMPLIFY in api.pipeline
    assert Stage.SIMPLIFY not in docs.pipeline and Stage.REVIEW in docs.pipeline
    assert Stage.SIMPLIFY not in client.pipeline and Stage.REVIEW not in client.pipeline
    child_status = eng.status("r1")["tasks"]["child-1"]
    assert child_status["agent_role"] == "backend"
    assert child_status["quality_tier"] == "full"
    assert child_status["implementation_budget"] == "short"

    # The named implementation agent and short timeout survive onto the WorkItem.
    eng.record("r1", make_result(eng.next_work("r1", "child-1")))  # intake
    implement = eng.next_work("r1", "child-1")
    assert implement.stage is Stage.IMPLEMENT
    assert implement.agent == "backend-agent" and implement.timeout_s == 900
    eng.record("r1", make_result(implement))
    simplify = eng.next_work("r1", "child-1")
    assert simplify.stage is Stage.SIMPLIFY
    assert simplify.agent == "simplify-agent"
    eng.record("r1", make_result(simplify))

    _finish(eng, "child-1")
    _finish(eng, "child-2")
    _finish(eng, "child-3")
    assert eng.store.load_task("r1", "parent").state is TaskState.COMPLETED
    assert ("parent", None) in source.completed


def test_failed_child_cascades_only_its_dependents(tmp_path, project) -> None:
    eng, _source = _decompose(tmp_path, project)
    eng.record("r1", make_result(eng.next_work("r1", "child-1")))  # intake
    while eng.store.load_task("r1", "child-1").state is not TaskState.FAILED:
        work = eng.next_work("r1", "child-1")
        assert work is not None
        eng.record(
            "r1", make_result(work, status=ResultStatus.FAILURE, error="implementation broke")
        )

    assert eng.store.load_task("r1", "child-1").state is TaskState.FAILED
    assert eng.store.load_task("r1", "child-3").state is TaskState.CASCADE_BLOCKED
    assert eng.store.load_task("r1", "parent").state is TaskState.CASCADE_BLOCKED
    assert eng.store.load_task("r1", "child-2").state is TaskState.PENDING
    assert "child-2" in eng.dispatchable("r1")


def test_invalid_child_dag_is_a_retryable_scope_failure(tmp_path, project) -> None:
    source = CreatingSource()
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    work = eng.next_work("r1", "parent")
    output = {
        "feasible": True,
        "plan": ["bad graph"],
        "subtasks": [
            {"id": "a", "description": "A", "depends_on": ["missing"]},
        ],
    }
    result = eng.record("r1", make_result(work, structured_output=output))
    assert result["outcome"] == "stage_failed_will_retry"
    assert not source.created
    assert "unknown task" in (eng.store.load_task("r1", "parent").last_error or "")


def test_source_without_create_hook_holds_parent_without_children(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    work = eng.next_work("r1", "parent")
    output = {
        "feasible": True,
        "plan": ["split"],
        "subtasks": [{"id": "a", "description": "A"}],
    }
    result = eng.record("r1", make_result(work, structured_output=output))
    assert result["outcome"] == "scope_decomposition_held"
    assert eng.store.load_task("r1", "parent").state is TaskState.BLOCKED_ON_HUMAN
    assert eng.store.load_run("r1").dependency_graph == {"parent": []}


def test_crash_window_reuses_source_task_by_marker(tmp_path, project) -> None:
    source = CreatingSource()
    source.created["child-existing"] = TaskSpec(
        task_id="child-existing",
        title="existing",
        body="Decomposition-key: parent/a\ncreated before the status mapping was saved",
    )
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    scope = eng.next_work("r1", "parent")
    # Simulate the durable SCOPE result surviving while the process died after the external
    # create and before _apply_scope_decomposition persisted its mapping.
    output = {
        "feasible": True,
        "plan": ["split"],
        "subtasks": [{"id": "a", "description": "A"}],
    }
    original = eng._apply_scope_decomposition
    eng._apply_scope_decomposition = lambda *_args, **_kwargs: eng.store.load_task(
        "r1", "parent"
    )
    eng.record("r1", make_result(scope, structured_output=output))
    eng._apply_scope_decomposition = original

    eng.dispatchable("r1")  # reconciliation resumes the filing saga
    parent = eng.store.load_task("r1", "parent")
    assert parent.decomposition_mapping == {"a": "child-existing"}
    assert source.create_calls == 0


def test_direct_next_resumes_approved_partial_decomposition(tmp_path, project) -> None:
    source = FailOnceCreatingSource()
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])

    result = eng.record(
        "r1",
        make_result(eng.next_work("r1", "parent"), structured_output=_scope_output()),
    )
    assert result["outcome"] == "scope_decomposition_held"
    parent = eng.store.load_task("r1", "parent")
    assert parent.state is TaskState.BLOCKED_ON_HUMAN
    assert parent.decomposition_mapping == {"api": "child-1"}

    eng.approve("r1", "parent", approved_by="operator")
    assert eng.next_work("r1", "parent") is None

    parent = eng.store.load_task("r1", "parent")
    assert parent.state is TaskState.BLOCKED
    assert parent.decomposition_children == ["child-1", "child-2", "child-3"]
    assert eng.registered_task_ids("r1") == {"parent", "child-1", "child-2", "child-3"}


def test_infeasible_scope_holds_instead_of_filing_children(tmp_path, project) -> None:
    """feasible=false wins over a subtasks payload in the same SCOPE result (#60 review).

    Decomposition files real external issues. Doing that for a parent the same transaction
    just parked at the human gate spends side effects on a verdict the human has not seen.
    """
    source = CreatingSource()
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    scope = eng.next_work("r1", "parent")

    output = {**_scope_output(), "feasible": False, "blocked_reason": "needs a decision"}
    result = eng.record("r1", make_result(scope, structured_output=output))

    assert result["outcome"] == "scope_not_feasible_held"
    parent = eng.store.load_task("r1", "parent")
    assert parent.state is TaskState.BLOCKED_ON_HUMAN
    assert parent.decomposition_children == []
    assert parent.decomposition_mapping == {}
    assert source.create_calls == 0, "filed children before the human released the hold"

    # The hold stays quiescent through an eligibility pass, then decomposes on approval.
    eng.dispatchable("r1")
    assert source.create_calls == 0
    eng.approve("r1", "parent", approved_by="tester")
    eng.dispatchable("r1")
    assert source.create_calls == 3
    assert eng.store.load_task("r1", "parent").decomposition_children


def test_marker_lookup_does_not_collide_on_an_id_prefix(tmp_path, project) -> None:
    """The body marker matches a WHOLE line, so local id ``a`` never reuses ``ab``'s
    child (#60 review). A substring test collapsed both subtasks onto one ref and let the
    umbrella finish having executed only one of them."""
    source = CreatingSource()
    source.created["child-ab"] = TaskSpec(
        task_id="child-ab",
        title="existing ab",
        body="Decomposition-key: parent/ab\nfiled before the mapping was saved",
    )
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    scope = eng.next_work("r1", "parent")
    output = {
        "feasible": True,
        "plan": ["split"],
        "subtasks": [
            {"id": "ab", "description": "AB"},
            {"id": "a", "description": "A"},
        ],
    }
    eng.record("r1", make_result(scope, structured_output=output))

    parent = eng.store.load_task("r1", "parent")
    assert parent.decomposition_mapping["ab"] == "child-ab"
    assert parent.decomposition_mapping["a"] != "child-ab"
    assert len(set(parent.decomposition_mapping.values())) == 2
