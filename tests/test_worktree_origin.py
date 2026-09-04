"""#381: provisioned workspaces cannot silently execute a sibling worktree's code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from adapters.execution.deterministic_test import DeterministicTestRunner
from adapters.execution.review_isolation import ReviewIsolation
from adapters.execution.runners import build_registry
from adapters.execution.transport import RawResult
from adapters.execution.worktree_origin import verify_worktree_origin
from adapters.project.origin_probes import PROBE_FILE, runner_source_probe
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
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
        def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
            return [(
                "runner interpreter",
                [sys.executable, "-c", f"print({str(launcher)!r})"],
                "launcher",
            )]

    assert verify_worktree_origin(Project(), tmp_path).trusted is True


def test_source_probe_rejects_intermediate_symlink_into_sibling(tmp_path) -> None:
    worktree = tmp_path / "review"
    sibling_module = tmp_path / "sibling" / "pkg" / "module.py"
    sibling_module.parent.mkdir(parents=True)
    sibling_module.write_text("executed_elsewhere = True\n")
    local_package = worktree / "src" / "pkg"
    local_package.parent.mkdir(parents=True)
    local_package.symlink_to(sibling_module.parent)
    reported = local_package / "module.py"

    class Project:
        def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
            return [(
                "project module",
                [sys.executable, "-c", f"print({str(reported)!r})"],
                "source",
            )]

    result = verify_worktree_origin(Project(), worktree)

    assert result.trusted is False
    assert result.notices == ({
        "notice": "worktree_origin_mismatch",
        "probe": "project module",
        "probe_kind": "source",
        "expected_worktree": str(worktree),
        "reported_path": str(reported),
        "resolved_path": str(sibling_module),
        "detail": "project module resolved outside the provisioned worktree",
    },)


class _ContaminatedProject(FakeProject):
    def __init__(self, sibling: Path, sentinel: Path) -> None:
        super().__init__()
        self.sibling = sibling
        self.sentinel = sentinel

    def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
        # These represent the two production failures: the console runner's interpreter
        # and an editable-installed module both resolve to a sibling worktree.
        return [
            (
                "test runner",
                [sys.executable, "-c", f"print({str(self.sibling)!r})"],
                "launcher",
            ),
            (
                "project module",
                [sys.executable, "-c", f"print({str(self.sibling)!r})"],
                "source",
            ),
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


def test_interactive_review_with_origin_hooks_is_rerouted_to_contained_runner(tmp_path) -> None:
    """The filesystem-free Workflow shim must never receive a probe-bearing REVIEW."""

    class Project(FakeProject):
        def fresh_install_paths(self) -> list[str]:
            return [".venv"]

        def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
            return [("project source", ["probe", "source"], "source")]

    project = Project()
    registry = build_registry(
        setup_project=project,
        headless_transport=lambda _work: RawResult({"approved": True, "issues": []}),
    )
    engine = Engine(
        StatusStore(tmp_path / "run"),
        CostLedger(tmp_path / "run" / "costs.jsonl"),
        project,
        router=Router(execution_mode=ExecutionMode.INTERACTIVE),
        registry=registry,
    )
    engine.create_run("r1")
    engine.add_task("r1", "T1")

    while True:
        work = engine.next_work("r1", "T1")
        assert work is not None
        if work.stage is Stage.REVIEW:
            break
        engine.record("r1", make_result(work))

    assert work.lane_policy.execution_mode is ExecutionMode.HEADLESS
    assert work.lane_policy.provider is Provider.CLAUDE
    event = next(
        event for event in engine.store.read_events("r1")
        if event["type"] == "stage_rerouted_for_worktree_origin"
    )
    assert event["level"] == "warning"
    assert event["from"] == "interactive:claude"
    assert event["to"] == "headless:claude"


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

    def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
        return [("review environment", ["sh", "-c", "cat .venv/origin"], "source")]


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


# --- #502: the source probe must go through the TEST RUNNER, not `python -c` --------------

def _svc_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A worktree holding its own `svc` package, plus an outside copy on PYTHONPATH.

    This is the live shape from family-finance `ff-batch-20260903-1724`: a workspace whose
    own source sits right there, while the environment the runner imports through resolves
    the same package somewhere else entirely.
    """
    worktree = tmp_path / "wt"
    (worktree / "svc").mkdir(parents=True)
    (worktree / "svc" / "__init__.py").write_text("HERE = 'worktree'\n")
    (worktree / "tests").mkdir()
    outside = tmp_path / "installed"
    (outside / "svc").mkdir(parents=True)
    (outside / "svc" / "__init__.py").write_text("HERE = 'sibling'\n")
    return worktree, outside


def _pytest_script() -> list[str]:
    """The BARE console script, which (unlike `python -m pytest`) leaves cwd off sys.path.

    That is the family-finance shape: `uv run pytest`, whose import resolution is decided by
    the environment rather than by where the command happened to be launched from.
    """
    script = Path(sys.executable).with_name("pytest")
    if not script.exists():  # pragma: no cover - the dev/CI env always installs pytest
        pytest.skip("no pytest console script next to this interpreter")
    return [str(script)]


def _probed(probe: tuple[str, list[str], str]):
    return type("P", (), {"worktree_origin_probes": lambda self: [probe]})()


def test_python_c_probe_passes_where_the_runner_imports_another_worktree(tmp_path, monkeypatch) -> None:
    """The false green #502 is about, reproduced against real interpreters.

    `python -c` puts the workspace's own cwd first on `sys.path`, so it reports the local
    copy — and says nothing about the import the tests will perform.
    """
    worktree, outside = _svc_worktree(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(outside))

    legacy = ("svc module", [sys.executable, "-c", "import svc as m; print(m.__file__)"], "source")
    assert verify_worktree_origin(_probed(legacy), worktree).trusted is True

    runner = runner_source_probe("svc", [*_pytest_script(), "-q", PROBE_FILE])
    verdict = verify_worktree_origin(_probed(runner), worktree)

    assert verdict.trusted is False
    (notice,) = verdict.notices
    assert notice["notice"] == "worktree_origin_mismatch"
    assert notice["probe_kind"] == "runner-source"
    assert notice["resolved_path"] == str(outside / "svc" / "__init__.py")


def test_runner_probe_trusts_a_workspace_the_runner_really_imports(tmp_path, monkeypatch) -> None:
    """The other direction: an honest environment must not be failed closed."""
    worktree, _ = _svc_worktree(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(worktree))  # the install points at THIS worktree

    probe = runner_source_probe("svc", [*_pytest_script(), "-q", PROBE_FILE])
    assert verify_worktree_origin(_probed(probe), worktree).trusted is True
    # The throwaway probe test and its output file are removed, whatever the verdict, and
    # no bytecode cache is left behind: a REVIEW workspace must not go dirty for a probe.
    assert sorted(p.name for p in (worktree / "tests").iterdir()) == []


def test_runner_probe_fails_closed_when_the_suite_cannot_run(tmp_path) -> None:
    """No result is not a pass: a runner that cannot start leaves origin unestablished."""
    worktree, _ = _svc_worktree(tmp_path)

    probe = runner_source_probe("svc", [*_pytest_script(), "--not-a-flag", PROBE_FILE])
    verdict = verify_worktree_origin(_probed(probe), worktree)

    assert verdict.trusted is False
    (notice,) = verdict.notices
    assert notice["notice"] == "worktree_origin_probe_failed"
    assert notice["probe_kind"] == "runner-source"


def test_runner_source_kind_is_resolved_like_source_not_like_launcher(tmp_path) -> None:
    """`runner-source` is a LABEL on the strong evidence, not a weaker containment rule."""
    worktree = tmp_path / "review"
    sibling = tmp_path / "sibling" / "pkg"
    sibling.mkdir(parents=True)
    (sibling / "module.py").write_text("")
    alias = worktree / "pkg"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(sibling)

    probe = ("svc module", [sys.executable, "-c", f"print({str(alias / 'module.py')!r})"], "runner-source")
    verdict = verify_worktree_origin(_probed(probe), worktree)

    assert verdict.trusted is False
    assert verdict.notices[0]["probe_kind"] == "runner-source"
    assert verdict.notices[0]["resolved_path"] == str(sibling / "module.py")


def test_unknown_probe_kind_is_still_refused(tmp_path) -> None:
    probe = ("svc module", [sys.executable, "-c", "print('/tmp')"], "runner")
    verdict = verify_worktree_origin(_probed(probe), tmp_path)

    assert verdict.trusted is False
    assert verdict.notices[0]["notice"] == "worktree_origin_probe_failed"
    assert "runner-source" in str(verdict.notices[0]["reason"])


def test_probe_test_is_written_where_the_project_tests_live() -> None:
    """Placement is load-bearing: pytest prepends a collected file's OWN directory to
    `sys.path`, so a probe dropped at the repo root would re-create the cwd-first lie."""
    _, argv, kind = runner_source_probe("svc", ["pytest", PROBE_FILE])

    assert (argv[0], kind) == ("sh", "runner-source")
    assert 'for candidate in tests test; do' in argv[2]
    assert PROBE_FILE not in argv[2]  # substituted for the real, per-run file
