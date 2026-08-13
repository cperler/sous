"""#381: provisioned workspaces cannot silently execute a sibling worktree's code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from adapters.execution.deterministic_test import DeterministicTestRunner
from adapters.execution.review_isolation import ReviewIsolation
from adapters.execution.transport import RawResult
from adapters.execution.worktree_origin import verify_worktree_origin
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def test_adapter_without_origin_probes_is_explicitly_skipped(tmp_path) -> None:
    result = verify_worktree_origin(object(), tmp_path)

    assert result.trusted is True
    assert result.notices == ({
        "notice": "worktree_origin_verification_skipped",
        "expected_worktree": str(tmp_path),
        "reason": "adapter-declared probes absent",
        "detail": "toolchain origin was not verified: adapter-declared probes absent",
    },)


def test_in_worktree_launcher_symlink_to_shared_interpreter_is_valid(tmp_path) -> None:
    shared = tmp_path.parent / "shared-python"
    shared.touch(exist_ok=True)
    launcher = tmp_path / ".venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(shared)

    class Project:
        def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
            return [("runner interpreter", [sys.executable, "-c", f"print({str(launcher)!r})"])]

    assert verify_worktree_origin(Project(), tmp_path).trusted is True


class _ContaminatedProject(FakeProject):
    def __init__(self, sibling: Path, sentinel: Path) -> None:
        super().__init__()
        self.sibling = sibling
        self.sentinel = sentinel

    def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
        # These represent the two production failures: the console runner's interpreter
        # and an editable-installed module both resolve to a sibling worktree.
        return [
            ("test runner", [sys.executable, "-c", f"print({str(self.sibling)!r})"]),
            ("project module", [sys.executable, "-c", f"print({str(self.sibling)!r})"]),
        ]

    def test_unit_cmd(self, files=None) -> list[str]:  # noqa: ANN001
        return ["sh", "-c", f"touch {self.sentinel}"]


def test_sibling_toolchain_is_rejected_and_evented_before_tests_run(tmp_path) -> None:
    worktree = tmp_path / "task-worktree"
    sibling = tmp_path / "sibling-worktree" / ".venv" / "bin" / "python"
    worktree.mkdir()
    sibling.parent.mkdir(parents=True)
    sibling.touch()
    sentinel = tmp_path / "tests-ran"
    project = _ContaminatedProject(sibling, sentinel)
    engine = Engine(
        StatusStore(tmp_path / "run"), CostLedger(tmp_path / "run" / "costs.jsonl"), project
    )
    engine.create_run("r1")
    engine.add_task("r1", "T1")

    while True:
        work = engine.next_work("r1", "T1")
        assert work is not None
        if work.stage is Stage.TEST:
            break
        output = None
        if work.stage is Stage.INTAKE:
            output = {
                "branch": "task/T1", "worktree": str(worktree),
                "base_sha": "", "baseline_captured": False,
            }
        engine.record("r1", make_result(work, structured_output=output))

    result = DeterministicTestRunner(project).dispatch(work)
    assert result.status is ResultStatus.FAILURE
    assert not sentinel.exists(), "a mismatched toolchain must be rejected before test argv runs"
    assert {notice["probe"] for notice in result.execution_notices} == {
        "test runner", "project module",
    }
    engine.record("r1", result)

    events = [
        event for event in engine.store.read_events("r1")
        if event["type"] == "execution_notice"
        and event.get("notice") == "worktree_origin_mismatch"
    ]
    assert len(events) == 2
    assert all(event["level"] == "warning" for event in events)
    assert all(event["expected_worktree"] == str(worktree) for event in events)
    assert all(event["resolved_path"] == str(sibling) for event in events)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _review_repo(path: Path) -> Path:
    main = path.with_name("main")
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "tests@example.test")
    _git(main, "config", "user.name", "Tests")
    (main / ".gitignore").write_text(".venv/\n")
    (main / "tracked.txt").write_text("review me\n")
    _git(main, "add", ".")
    _git(main, "commit", "-qm", "base")
    _git(main, "worktree", "add", "-q", "-b", "task/review", str(path))
    return path


class _ReviewProject:
    def fresh_install_paths(self) -> list[str]:
        return [".venv"]

    def install_cmd(self) -> list[str]:
        return ["sh", "-c", "mkdir -p .venv && pwd > .venv/origin"]

    def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
        return [("review environment", ["sh", "-c", "cat .venv/origin"])]


def test_disposable_review_does_not_copy_venv_and_installs_fresh(tmp_path) -> None:
    live = _review_repo(tmp_path / "live")
    (live / ".venv").mkdir()
    (live / ".venv" / "origin").write_text("/some/sibling/worktree\n")
    project = _ReviewProject()

    from orchestrator.schemas.enums import ExecutionMode, Provider
    from orchestrator.schemas.work import LanePolicy, WorkItem

    work = WorkItem.create(
        id="review-1", run_id="r1", task_id="T1", stage=Stage.REVIEW,
        prompt=f"review {live}", schema_ref="review", model="reviewer",
        lane_policy=LanePolicy(
            execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE
        ),
        cwd=str(live), created_at="now",
    )
    seen: list[Path] = []

    def inner(isolated: WorkItem) -> RawResult:
        cwd = Path(isolated.cwd or "")
        seen.append(cwd)
        assert (cwd / ".venv" / "origin").read_text().strip() == str(cwd.resolve())
        return RawResult({"approved": True, "issues": []})

    with ReviewIsolation(project).session(work, inner) as transport:
        raw = transport(work)

    assert raw.error is None
    assert raw.execution_notices == ()
    assert seen and not seen[0].exists()
    assert (live / ".venv" / "origin").read_text().strip() == "/some/sibling/worktree"
