"""#301: REVIEW commands run in disposable worktrees and temporary port blocks."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import adapters.execution.review_isolation as isolation_module
from adapters.execution.codex import CodexRunner
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.transport import RawResult, _codex_permission_read_only
from orchestrator.port_registry import ENV_PORT_BASE, ENV_PORT_COUNT, PortRegistry
from orchestrator.review_workflow import issue_fingerprint
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import (
    FinderSpec,
    LanePolicy,
    ReviewPlan,
    ToolPolicy,
    WorkItem,
)

CLAUDE = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
READ_ONLY = ToolPolicy(allow_file_writes=False)


class PortProject:
    needs_ports = True
    port_range = (55000, 55999)
    port_block_size = 10

    def __init__(self, registry: Path) -> None:
        self.port_registry_path = str(registry)

    def port_env(self, base: int, count: int) -> dict[str, str]:
        return {"APP_PORT": str(base + 1), "APP_URL": f"http://127.0.0.1:{base + 1}"}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> Path:
    main = path.with_name(f"{path.name}-main")
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "tests@example.test")
    _git(main, "config", "user.name", "Tests")
    (main / ".gitignore").write_text(".deps/\n", encoding="utf-8")
    (main / "tracked.txt").write_text("review this\n", encoding="utf-8")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "base")
    _git(main, "worktree", "add", "-q", "-b", f"task-{path.name}", str(path))
    assert (path / ".git").is_file()  # the production task-worktree shape
    (path / ".deps").mkdir()
    (path / ".deps" / "ready").write_text("installed\n", encoding="utf-8")
    return path


def _ports_without_probe(monkeypatch, project: PortProject) -> PortRegistry:
    """The managed test sandbox forbids bind probes; retain allocator semantics otherwise."""
    def registry(_project) -> PortRegistry:
        return PortRegistry(
            project.port_registry_path,
            port_range=project.port_range,
            block_size=project.port_block_size,
            bind_probe=False,
        )

    monkeypatch.setattr(isolation_module, "registry_for_project", registry)
    return registry(project)


def _work(repo: Path, *, policy: LanePolicy = CLAUDE, plan: ReviewPlan | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-review", run_id="r1", task_id="t1", stage=Stage.REVIEW,
        prompt=f"review worktree: {repo}", schema_ref="review", model="review-model",
        created_at="now",
        lane_policy=policy, cwd=str(repo), plan=plan, tool_policy=READ_ONLY,
        env={
            ENV_PORT_BASE: "45000", ENV_PORT_COUNT: "10", "PORT": "45000",
            "APP_PORT": "45001", "APP_URL": "http://127.0.0.1:45001",
        },
    )


def _plan(repo: Path) -> ReviewPlan:
    return ReviewPlan(
        finders=(
            FinderSpec(lens="find:code", prompt=f"code in {repo}", agent=None,
                       schema_ref="review_findings"),
            FinderSpec(lens="find:spec", prompt=f"spec in {repo}", agent=None,
                       schema_ref="review_findings"),
        ),
        verify_template="verify {finding} at {diff_hint}",
        verify_schema_ref="review_verdict",
        dedupe_rule="fingerprint-v1",
    )


def test_panel_calls_get_independent_worktrees_and_port_blocks(tmp_path, monkeypatch) -> None:
    live = _repo(tmp_path / "live")
    project = PortProject(tmp_path / "ports.json")
    _ports_without_probe(monkeypatch, project)
    finding = {"severity": "critical", "file": "tracked.txt", "line": 1,
               "description": "possible regression"}
    fingerprint = issue_fingerprint(finding)
    seen_cwds: list[Path] = []
    seen_ports: list[str] = []

    def transport(work: WorkItem) -> RawResult:
        cwd = Path(work.cwd or "")
        seen_cwds.append(cwd)
        seen_ports.append((work.env or {})[ENV_PORT_BASE])
        assert cwd != live and (cwd / ".git").is_dir()
        assert str(live) not in work.prompt
        if str(work.phase).startswith("find:"):
            assert str(cwd) in work.prompt
        assert (cwd / ".deps" / "ready").is_file()  # ignored dependencies copied too
        assert _git(cwd, "remote") == ""  # no writable path back to the live repository
        assert not (cwd / "leak.txt").exists()  # every panel call starts clean
        (cwd / "tracked.txt").write_text("mutated by reviewer\n", encoding="utf-8")
        (cwd / "leak.txt").write_text("cache\n", encoding="utf-8")
        if work.phase == "find:code":
            return RawResult({"findings": [finding]})
        if work.phase == "find:spec":
            return RawResult({"findings": []})
        return RawResult({"fingerprint": fingerprint, "verdict": "refuted",
                          "reasoning": "test disproved it"})

    result = HeadlessClaudeRunner(transport, review_project=project).dispatch(
        _work(live, plan=_plan(live))
    )

    assert result.status is ResultStatus.SUCCESS
    assert len(seen_cwds) == 3 and len(set(seen_cwds)) == 3
    assert len(set(seen_ports)) == 3 and "45000" not in seen_ports
    assert all(not cwd.exists() for cwd in seen_cwds)
    assert (live / "tracked.txt").read_text() == "review this\n"
    assert not (live / "leak.txt").exists()
    assert (tmp_path / "ports.json").read_text().strip() == "[]"


def test_codex_review_is_writable_only_inside_disposable_workspace(tmp_path) -> None:
    live = _repo(tmp_path / "live")
    seen: list[WorkItem] = []

    def transport(work: WorkItem) -> RawResult:
        seen.append(work)
        assert work.workspace_isolated
        assert not _codex_permission_read_only(work)
        Path(work.cwd or "", ".pytest_cache").mkdir()
        Path(work.cwd or "", "tracked.txt").write_text("codex changed it\n", encoding="utf-8")
        return RawResult({"approved": True, "issues": [], "tests_meaningful": True})

    work = _work(live, policy=CODEX).model_copy(update={"env": None})
    result = CodexRunner(transport).dispatch(work)

    assert result.status is ResultStatus.SUCCESS
    assert seen and not Path(seen[0].cwd or "").exists()
    assert (live / "tracked.txt").read_text() == "review this\n"
    assert not (live / ".pytest_cache").exists()


def test_bytecode_caches_are_not_copied_into_the_disposable_review_copy(tmp_path) -> None:
    """#410: a copied cache names the ORIGINAL worktree in tracebacks raised from the copy."""
    live = _repo(tmp_path / "live")
    (live / "__pycache__").mkdir()
    (live / "__pycache__" / "top.cpython-313.pyc").write_bytes(b"stale")
    (live / "pkg").mkdir()
    (live / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (live / "pkg" / "__pycache__").mkdir()
    (live / "pkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"stale")
    (live / "pkg" / "loose.pyc").write_bytes(b"stale")
    seen: list[Path] = []

    def transport(work: WorkItem) -> RawResult:
        cwd = Path(work.cwd or "")
        seen.append(cwd)
        assert not (cwd / "__pycache__").exists()
        assert not (cwd / "pkg" / "__pycache__").exists()
        assert not (cwd / "pkg" / "loose.pyc").exists()
        assert (cwd / "pkg" / "mod.py").read_text() == "value = 1\n"  # sources still copied
        assert (cwd / ".deps" / "ready").is_file()  # unrelated ignored payload untouched
        return RawResult({"approved": True, "issues": [], "tests_meaningful": True})

    work = _work(live).model_copy(update={"env": None})  # no inherited ports to isolate
    result = HeadlessClaudeRunner(transport).dispatch(work)

    assert result.status is ResultStatus.SUCCESS
    assert seen and not seen[0].exists()
    assert (live / "__pycache__" / "top.cpython-313.pyc").is_file()  # live tree unchanged


def test_port_exhaustion_fails_closed_without_calling_reviewer(tmp_path, monkeypatch) -> None:
    live = _repo(tmp_path / "live")
    project = PortProject(tmp_path / "ports.json")
    project.port_range = (55000, 55009)
    registry = _ports_without_probe(monkeypatch, project)
    registry.allocate("other-run", "other-task")
    called = False

    def transport(_work: WorkItem) -> RawResult:
        nonlocal called
        called = True
        return RawResult({"approved": True})

    result = HeadlessClaudeRunner(transport, review_project=project).dispatch(_work(live))

    assert result.status is ResultStatus.FAILURE
    assert result.error and "no verifier port block" in result.error
    assert not called


def test_inherited_task_ports_without_project_mapping_fail_closed(tmp_path) -> None:
    live = _repo(tmp_path / "live")
    called = False

    def transport(_work: WorkItem) -> RawResult:
        nonlocal called
        called = True
        return RawResult({"approved": True})

    result = HeadlessClaudeRunner(transport).dispatch(_work(live))

    assert result.status is ResultStatus.FAILURE
    assert result.error and "project port configuration" in result.error
    assert not called


def test_exception_still_cleans_workspace_and_port_allocation(tmp_path, monkeypatch) -> None:
    live = _repo(tmp_path / "live")
    project = PortProject(tmp_path / "ports.json")
    _ports_without_probe(monkeypatch, project)
    seen_cwd: Path | None = None

    def transport(work: WorkItem) -> RawResult:
        nonlocal seen_cwd
        seen_cwd = Path(work.cwd or "")
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError, match="provider exploded"):
        HeadlessClaudeRunner(transport, review_project=project).dispatch(_work(live))

    assert seen_cwd is not None and not seen_cwd.exists()
    assert (tmp_path / "ports.json").read_text().strip() == "[]"
