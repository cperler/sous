"""Deterministic review policy gates (#65): the adapter's review_findings hook merges
engine-side into a completed REVIEW — a blocking finding overrides the model's
approval (the old merge_e2e_policy_review_finding semantics), advisory findings
become tracked follow-ups, and a raising hook can never break record()."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance_to_review(eng, run="r1", task="t1"):
    eng.create_run(run)
    eng.add_task(run, task)
    for _ in range(5):
        eng.record(run, make_result(eng.next_work(run, task)))
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


class _PolicyProject(FakeProject):
    def __init__(self, findings) -> None:
        super().__init__()
        self._findings = findings
        self.seen_worktrees: list = []

    def review_findings(self, *, worktree=None):
        self.seen_worktrees.append(worktree)
        if isinstance(self._findings, Exception):
            raise self._findings
        return self._findings


def test_blocking_finding_overrides_model_approval(tmp_path) -> None:
    project = _PolicyProject([{
        "description": "e2e policy: frontend change with no spec change",
        "file": "frontend/src/x.tsx", "blocking": True,
    }])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "review_rejected_fix_cycle"  # the gate, not the model, decides
    task = eng.store.load_task("r1", "t1")
    assert "e2e policy" in task.learnings[-1]
    # the hook received the intake worktree from the context plane
    assert project.seen_worktrees and project.seen_worktrees[0] == "/wt/42"
    events = [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_merged"]
    assert events and events[0]["blocking"] == 1
    # severity defaulted to critical: a repeated policy finding must never
    # convergence-auto-approve past the gate
    rec_issues = task.context.get("issues") or []
    assert any("critical" in str(i) for i in rec_issues)


def test_advisory_finding_becomes_tracked_followup(tmp_path) -> None:
    project = _PolicyProject([{
        "description": "API contract: frontend api types touched — double-check the lambda models",
        "blocking": False,
    }])
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"  # advisory never blocks
    filed = project.task_source.followups
    assert any("API contract" in f["title"] for f in filed)  # filed at finalize
    review = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output
    assert review["non_blocking"][0]["disposition"] == "file"


def test_raising_hook_never_breaks_record(tmp_path) -> None:
    project = _PolicyProject(RuntimeError("policy hook exploded"))
    eng = _engine(tmp_path, project)
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"  # best-effort: review proceeds
    events = [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_failed"]
    assert events and "exploded" in events[0]["error"]


def test_no_hook_and_empty_findings_are_untouched(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)  # plain FakeProject: no review_findings hook
    w = _advance_to_review(eng)
    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))
    assert out["outcome"] == "task_completed"
