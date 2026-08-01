"""SCOPE child-task decomposition and the attributed simplification pass (#60)."""

from __future__ import annotations

import threading
import time

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
        # Widens the external-create window so a concurrency test races on the guarantee
        # rather than on timing (see _race_reconciliation).
        self.create_delay = 0.0
        self._guard = threading.Lock()

    def create_task(self, title: str, body: str, labels=None) -> str:
        time.sleep(self.create_delay)
        with self._guard:
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


class NoLookupCreatingSource(CreatingSource):
    """A creating source with NO usable ``list_tasks`` hook.

    ``_find_decomposition_child`` needs one to spot an already-filed child, so this source
    has nothing but the durable mapping standing between two reconcilers and a duplicate
    issue — the weakest configuration the engine has to be correct in.
    """

    list_tasks = None  # type: ignore[assignment]


def _stage_unapplied_scope(tmp_path, project, source: CreatingSource, output: dict) -> None:
    """Durably record a SCOPE result carrying ``output`` while suppressing the saga.

    Leaves exactly the on-disk shape #354 lives in — a COMPLETED scope stage with a
    ``subtasks`` payload, no filed children, no mapping — which is what every scheduler
    process that touches the run then tries to reconcile.
    """
    project._task_source = source
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "parent", pipeline=[Stage.SCOPE, Stage.IMPLEMENT])
    scope = eng.next_work("r1", "parent")
    original = eng._apply_scope_decomposition
    eng._apply_scope_decomposition = lambda *_args, **_kwargs: eng.store.load_task(
        "r1", "parent"
    )
    try:
        eng.record("r1", make_result(scope, structured_output=output))
    finally:
        eng._apply_scope_decomposition = original
    assert eng.store.load_task("r1", "parent").decomposition_mapping == {}


def _race_reconciliation(tmp_path, project, output: dict, workers: int = 2) -> Engine:
    """Reconcile one parent's decomposition from ``workers`` independent engines at once.

    Each engine gets its own ``StatusStore`` over the same run directory: the cross-process
    shape of #354 without the fork, since the only thing that can serialize them is a lock
    on disk. Every worker loads the parent BEFORE the barrier, so they all enter holding the
    same pre-saga snapshot, and ``create_delay`` keeps whichever one wins inside its
    lookup→create window while the others arrive. Returns one engine for assertions.
    """
    engines = [_engine(tmp_path, project) for _ in range(workers)]
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    guard = threading.Lock()

    def _reconcile(eng: Engine) -> None:
        parent = eng.store.load_task("r1", "parent")
        barrier.wait()
        try:
            eng._apply_scope_decomposition("r1", parent, output)
        except BaseException as exc:  # noqa: BLE001 - any failure here is a real defect
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=_reconcile, args=(eng,)) for eng in engines]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if errors:
        raise errors[0]
    return engines[0]


def _assert_decomposed_once(eng: Engine, source: CreatingSource, local_ids: list[str]) -> None:
    parent = eng.store.load_task("r1", "parent")
    assert source.create_calls == len(local_ids), "a child was filed more than once"
    assert len(source.created) == len(local_ids)
    assert sorted(parent.decomposition_mapping) == sorted(local_ids)
    refs = set(parent.decomposition_mapping.values())
    assert len(refs) == len(local_ids), "two local ids collapsed onto one ref"
    # Every filed issue is mapped: no orphan real-world side effect left behind.
    assert refs == set(source.created)
    assert set(parent.decomposition_children) == refs
    assert eng.registered_task_ids("r1") == {"parent", *refs}
    decomposed = [
        ev for ev in eng.store.read_events("r1")
        if ev.get("type") == "task_decomposed" and ev.get("task_id") == "parent"
    ]
    assert len(decomposed) == 1, "the losing reconciler re-announced the decomposition"


def test_concurrent_reconciliation_files_each_child_once(tmp_path, project) -> None:
    """Two reconcilers, one parent, one issue per subtask (#354).

    Without a per-parent lock spanning lookup→create, both read a mapping that lacks local
    id X and both file an external issue for it; the mapping records the winner and the
    loser's issue is real, orphaned and referenced by nothing.
    """
    source = CreatingSource()
    source.create_delay = 0.05
    output = _scope_output()
    _stage_unapplied_scope(tmp_path, project, source, output)

    eng = _race_reconciliation(tmp_path, project, output)

    _assert_decomposed_once(eng, source, ["api", "docs", "client"])
    # The child DAG is the one the plan asked for, not an interleaving of two sagas.
    mapping = eng.store.load_task("r1", "parent").decomposition_mapping
    graph = eng.store.load_run("r1").dependency_graph
    assert graph["parent"] == [mapping["docs"], mapping["client"]]
    assert graph[mapping["client"]] == [mapping["api"]]


def test_concurrent_reconciliation_without_list_tasks_files_each_child_once(
    tmp_path, project
) -> None:
    """The same race on a source that cannot look a filed child up by marker (#354).

    The marker lookup is best-effort — it needs ``list_tasks``, and it only narrows the
    window even where it exists. With it gone, serialization plus the re-read of the
    parent's mapping inside the lock is the ONLY thing preventing duplicate filings.
    """
    source = NoLookupCreatingSource()
    source.create_delay = 0.05
    output = _scope_output()
    _stage_unapplied_scope(tmp_path, project, source, output)

    eng = _race_reconciliation(tmp_path, project, output, workers=3)

    _assert_decomposed_once(eng, source, ["api", "docs", "client"])


def test_reconciliation_does_not_file_for_a_parent_held_while_it_waited(
    tmp_path, project
) -> None:
    """A saga that waited on the lock re-checks the human gate before filing (#354).

    Serializing the saga means a late entrant now runs AFTER the winner finished — possibly
    after the winner's filing failure parked the parent for an operator. Re-reading the
    parent under the lock is what makes that observable, so honour it: a parent at the human
    gate files nothing, exactly as it would have on the ``record``/``dispatchable`` paths.
    """
    source = CreatingSource()
    output = _scope_output()
    _stage_unapplied_scope(tmp_path, project, source, output)
    eng = _engine(tmp_path, project)
    stale = eng.store.load_task("r1", "parent")
    eng._hold_decomposition("r1", "parent", "the winning reconciler could not file")

    assert eng._apply_scope_decomposition("r1", stale, output).state is TaskState.BLOCKED_ON_HUMAN
    assert source.create_calls == 0
    assert eng.store.load_task("r1", "parent").decomposition_children == []


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
