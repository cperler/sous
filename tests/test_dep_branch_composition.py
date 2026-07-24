"""#216: the engine resolves a batch task's COMPLETED DAG-dependency branches and injects
them as ``dep_branches`` into the deterministic INTAKE WorkItem context, so the setup
runner can compose the dependency's code into the dependent's worktree BEFORE its per-PR
gate runs. A DAG edge only ORDERS execution; without this a sibling's shared type/signature
change escapes the dependent's gate (the #161/#194 ModelId regression this closes)."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionLane, Stage, TaskState
from orchestrator.status_store import StatusStore


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def _complete_with_branch(eng: Engine, run: str, task: str, branch: str) -> None:
    def _mut(t) -> None:
        t.state = TaskState.COMPLETED
        t.context["branch"] = branch

    eng.store.update_task(run, task, _mut)


def test_intake_injects_completed_dep_branches(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "A")
    eng.add_task("r1", "B", depends_on=["A"])
    _complete_with_branch(eng, "r1", "A", "task/A")

    w = eng.next_work("r1", "B")

    assert w is not None and w.stage is Stage.INTAKE
    assert w.context["dep_branches"] == ["task/A"]


def test_intake_composes_deps_in_graph_order(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "A")
    eng.add_task("r1", "B")
    eng.add_task("r1", "C", depends_on=["A", "B"])
    _complete_with_branch(eng, "r1", "A", "task/A")
    _complete_with_branch(eng, "r1", "B", "task/B")

    w = eng.next_work("r1", "C")

    assert w is not None and w.context["dep_branches"] == ["task/A", "task/B"]


def test_no_deps_omits_dep_branches(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "A")

    w = eng.next_work("r1", "A")

    assert w is not None and w.stage is Stage.INTAKE
    assert (w.context or {}).get("dep_branches") is None


def test_incomplete_dep_is_not_composed(tmp_path, project) -> None:
    """A dep that is not yet COMPLETED (or has no resolvable branch) is skipped — the
    dependent degrades to base-only rather than merging half-built dep code."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "A")
    eng.add_task("r1", "B", depends_on=["A"])
    # A is left non-terminal (no branch on its context plane yet).

    w = eng.next_work("r1", "B")

    assert w is not None and (w.context or {}).get("dep_branches") is None


def test_dep_branches_only_at_intake_not_later_stages(tmp_path, project) -> None:
    """dep_branches rides the INTAKE WorkItem only — later deterministic stages
    (test/deliver) never re-compose. Proven via the engine helper: it injects the key
    for INTAKE and omits it for a later stage on the same dependent."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "A")
    eng.add_task("r1", "B", depends_on=["A"])
    _complete_with_branch(eng, "r1", "A", "task/A")
    run = eng.store.load_run("r1")
    task_b = eng.store.load_task("r1", "B")

    intake_ctx = eng._deterministic_context(task_b, stage=Stage.INTAKE, run=run)
    deliver_ctx = eng._deterministic_context(task_b, stage=Stage.DELIVER, run=run)

    assert intake_ctx["dep_branches"] == ["task/A"]
    assert "dep_branches" not in deliver_ctx
