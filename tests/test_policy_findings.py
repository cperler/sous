"""Deterministic review policy gates (#65): the adapter's review_findings hook merges
engine-side into a completed REVIEW — a blocking finding overrides the model's
approval (the old merge_e2e_policy_review_finding semantics), advisory findings
become tracked follow-ups, and a raising hook can never break record()."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adapters.project.heysoo.config import HeysooConfig
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


# --- the heysoo reference gates, against a real repo -----------------------------

def _repo(tmp_path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-qb", "task/x"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(repo: Path, *paths: str) -> None:
    for p in paths:
        f = repo / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("content")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "change"], cwd=repo, check=True)


class _QuietTscHeysoo(HeysooConfig):
    """Typecheck stubbed green so the e2e-policy gate is isolated (and no npx)."""

    def typecheck_cmd(self) -> list[str]:
        return ["sh", "-c", "exit 0"]


def test_heysoo_e2e_policy_flags_frontend_change_without_spec(tmp_path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "frontend/src/widget.tsx")
    findings = _QuietTscHeysoo().review_findings(worktree=str(repo))
    assert len(findings) == 1
    assert "e2e policy" in findings[0]["description"] and findings[0]["blocking"]


def test_heysoo_e2e_policy_satisfied_by_spec_change(tmp_path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "frontend/src/widget.tsx", "tests/e2e/widget.spec.ts")
    assert _QuietTscHeysoo().review_findings(worktree=str(repo)) == []


def test_heysoo_backend_only_change_passes_e2e_policy(tmp_path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "lambda/suggest/handler.py")
    assert _QuietTscHeysoo().review_findings(worktree=str(repo)) == []


def test_heysoo_tsc_gate_flags_failing_typecheck(tmp_path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "lambda/suggest/handler.py")

    class _RedTsc(HeysooConfig):
        def typecheck_cmd(self) -> list[str]:
            return ["sh", "-c", "echo 'TS2322: type error'; exit 2"]

    findings = _RedTsc().review_findings(worktree=str(repo))
    assert len(findings) == 1
    f = findings[0]
    assert "TSC gate" in f["description"] and "TS2322" in f["description"]
    assert f["severity"] == "critical" and f["blocking"]


def test_heysoo_no_worktree_is_silent(tmp_path) -> None:
    assert _QuietTscHeysoo().review_findings(worktree=None) == []
    assert _QuietTscHeysoo().review_findings(worktree=str(tmp_path / "nope")) == []
