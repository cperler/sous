"""Seams that existed but were wired to nothing (audit gap 6): closed-issue early
exit, add-task --depends-on / --provider-tag producers, e2e/shell commands visible
to prompts, and the codex sandbox's --add-dir git-common-dir grant."""

from __future__ import annotations

import json
import subprocess

import pytest

from adapters.project.github_issues import GitHubIssuesSource
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


# --- closed-issue early exit ---------------------------------------------------

def _issue_payload(state: str) -> str:
    return json.dumps({"number": 7, "title": "T", "body": "B", "labels": [], "state": state})


def test_resolve_refuses_closed_issue() -> None:
    src = GitHubIssuesSource("o/r", runner=lambda argv: _issue_payload("CLOSED"))
    with pytest.raises(ValueError, match="CLOSED"):
        src.resolve("#7")


def test_resolve_allows_open_issue_and_opt_out() -> None:
    src = GitHubIssuesSource("o/r", runner=lambda argv: _issue_payload("OPEN"))
    assert src.resolve("#7").title == "T"
    relaxed = GitHubIssuesSource("o/r", runner=lambda argv: _issue_payload("CLOSED"),
                                 allow_closed=True)
    assert relaxed.resolve("#7").issue_number == 7  # deliberate re-run path


def test_resolve_requests_state_field() -> None:
    seen: dict = {}

    def runner(argv):
        seen["argv"] = argv
        return _issue_payload("OPEN")

    GitHubIssuesSource("o/r", runner=runner).resolve("#7")
    json_arg = seen["argv"][seen["argv"].index("--json") + 1]
    assert "state" in json_arg


# --- add-task producers for the DAG + provider routing ---------------------------

def test_add_task_depends_on_and_provider_tag_overrides(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2", depends_on=["t1"], provider_tag="codex")
    run = eng.store.load_run("r1")
    assert run.dependency_graph["t2"] == ["t1"]
    task = eng.store.load_task("r1", "t2")
    assert task.provider_tag == "codex" and task.depends_on == ["t1"]
    # the DAG consumer actually sees the edge: t2 is not dispatchable until t1 completes
    assert eng.dispatchable("r1") == ["t1"]


# --- e2e/shell commands reach the prompts ----------------------------------------

def test_prompt_carries_e2e_and_shell_commands(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(3):  # intake, scope, implement
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST
    assert "test (e2e): echo e2e" in w.prompt  # the adapter's e2e suite is visible
    assert "test (shell): echo shell" in w.prompt


def test_minimal_project_without_e2e_still_renders(tmp_path, project) -> None:
    class Minimal:
        name = "min"
        task_source = project.task_source
        install_cmd = staticmethod(lambda: ["true"])  # the no-op sentinel is omitted
        test_unit_cmd = staticmethod(lambda: ["pytest", "-q"])
        typecheck_cmd = staticmethod(lambda: [])

        def agent_for(self, stage, role=None):
            return None

        def setup_task(self, task_id):
            return {"branch": "b", "worktree": "/wt", "baseline_captured": True}

    eng = _engine(tmp_path, Minimal())
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")
    assert "test (unit): pytest -q" in w.prompt
    assert "e2e" not in w.prompt and "install" not in w.prompt  # absent + sentinel omitted


# --- codex --add-dir git-common-dir grant ----------------------------------------

def test_codex_transport_grants_git_common_dir(tmp_path, monkeypatch) -> None:
    """In a real linked worktree, codex exec gets --add-dir <main .git dir> so
    in-sandbox commits work (the reference run_codex_stage's grant)."""
    from adapters.execution.transport import codex_cli_transport
    from orchestrator.schemas.enums import ExecutionMode, Provider
    from orchestrator.schemas.work import LanePolicy, WorkItem

    # real repo + linked worktree
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=tmp_path, check=True)
    wt = tmp_path / ".worktrees" / "x"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "task/x"],
                   cwd=tmp_path, check=True)

    seen: dict = {}
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "git":  # the probe itself stays real
            return real_run(argv, **kwargs)
        seen["argv"] = argv

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    policy = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    wi = WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
                         schema_ref="implement", model="gpt-5-codex", lane_policy=policy,
                         created_at="t", cwd=str(wt))
    codex_cli_transport()(wi)
    argv = seen["argv"]
    assert "--add-dir" in argv
    granted = argv[argv.index("--add-dir") + 1]
    assert granted.endswith(".git") and str(tmp_path) in granted  # the MAIN repo's git dir


def test_codex_transport_no_grant_without_repo(tmp_path, monkeypatch) -> None:
    from adapters.execution.transport import codex_cli_transport
    from orchestrator.schemas.enums import ExecutionMode, Provider
    from orchestrator.schemas.work import LanePolicy, WorkItem

    seen: dict = {}
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "git":
            return real_run(argv, **kwargs)  # fails: not a repo
        seen["argv"] = argv

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    policy = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    wi = WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
                         schema_ref="implement", model="gpt-5-codex", lane_policy=policy,
                         created_at="t", cwd=str(tmp_path))
    codex_cli_transport()(wi)
    assert "--add-dir" not in seen["argv"]  # best-effort: no repo, no grant, no failure
