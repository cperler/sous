"""#553: run-scoped sharing of the INTAKE test baseline across same-tree tasks.

Real git in tmp, and a counting test command (appends to a file OUTSIDE the worktree) so
"the suite ran exactly once" is measured, not assumed."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from adapters.execution import baseline_cache
from adapters.execution.deterministic_setup import DeterministicSetupRunner
from adapters.execution.runners import build_registry
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=path, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "a.txt").write_text("a\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-qm", "init")
    return path


class _CountingProj:
    """Counts suite runs in ``counter``; ``rc`` makes the suite red, ``failing`` is what the
    classifier reports for it."""

    def __init__(self, repo: Path, counter: Path, *, rc: int = 0,
                 failing: list[str] | None = None, argv_tag: str = "") -> None:
        self.repo_root = str(repo)
        self._counter = counter
        self._rc = rc
        self._tag = argv_tag
        if failing is not None:
            self.classifier = _Classifier(failing)

    def install_cmd(self) -> list[str]:
        return ["true"]

    def test_unit_cmd(self, files=None) -> list[str]:
        return ["sh", "-c", f"echo run{self._tag} >> '{self._counter}'; exit {self._rc}"]


class _Failure:
    def __init__(self, test: str) -> None:
        self.test = test


class _Classifier:
    def __init__(self, failing: list[str]) -> None:
        self._failing = failing

    def classify(self, output: str) -> list[_Failure]:
        return [_Failure(t) for t in self._failing]


def _wi(task: str, context: dict | None = None) -> WorkItem:
    safe = task.lstrip("#")
    return WorkItem.create(
        id=f"wi-{safe}", run_id="r1", task_id=task, stage=Stage.INTAKE, prompt="p",
        schema_ref="intake", model="engine", lane_policy=_ENGINE, created_at="now",
        checkpoint_tag=f"task/r1/-{safe}/intake/0", context=context,
    )


def _runs(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def _intake(proj, cache_dir, task: str, context: dict | None = None) -> dict:
    res = DeterministicSetupRunner(proj, baseline_cache_dir=cache_dir).dispatch(_wi(task, context))
    assert res.status is ResultStatus.SUCCESS, res
    return res.structured_output


def test_same_tree_tasks_share_one_baseline_run(tmp_path) -> None:
    repo, counter, cache = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter)

    first = _intake(proj, cache, "#5")
    second = _intake(proj, cache, "#6")

    assert _runs(counter) == 1
    assert "green at base" in first["baseline"] and "reused" not in first["baseline"]
    assert "baseline_reused_from" not in first
    assert "reused from #5 at tree" in second["baseline"]
    assert "green at base" in second["baseline"]
    assert second["baseline_captured"] is True and second["baseline_failures"] == []
    assert second["baseline_reused_from"] == "#5"
    assert len(second["baseline_tree"]) == 40


def test_a_different_tree_runs_its_own_baseline(tmp_path) -> None:
    repo, counter, cache = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter)
    _intake(proj, cache, "#5")
    (repo / "b.txt").write_text("trunk moved\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "trunk moves")

    later = _intake(proj, cache, "#6")

    assert _runs(counter) == 2
    assert "reused" not in later["baseline"]


def test_composed_dep_branch_changes_the_key(tmp_path) -> None:
    repo, counter, cache = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter)
    _git(repo, "checkout", "-qb", "task/dep")
    (repo / "dep.txt").write_text("dep\n")
    _git(repo, "add", "dep.txt")
    _git(repo, "commit", "-qm", "dep work")
    _git(repo, "checkout", "-q", "-")
    _intake(proj, cache, "#5")

    dependent = _intake(proj, cache, "#9", {"dep_branches": ["task/dep"]})

    assert dependent["composed_deps"] == ["task/dep"]
    assert _runs(counter) == 2
    assert "reused" not in dependent["baseline"]


def test_red_baseline_failures_are_reused_verbatim(tmp_path) -> None:
    repo, counter, cache = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter, rc=1, failing=["tests/test_x.py::t1"])

    _intake(proj, cache, "#5")
    second = _intake(proj, cache, "#6")

    assert _runs(counter) == 1
    assert second["baseline_failures"] == ["tests/test_x.py::t1"]
    assert "RED at base" in second["baseline"] and "reused from #5" in second["baseline"]


def test_no_cache_dir_keeps_per_task_capture(tmp_path) -> None:
    repo, counter = _repo(tmp_path / "repo"), tmp_path / "count"
    proj = _CountingProj(repo, counter)

    _intake(proj, None, "#5")
    second = _intake(proj, None, "#6")

    assert _runs(counter) == 2
    assert "reused" not in second["baseline"]


def test_env_var_disables_sharing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(baseline_cache.ENV_DISABLE, "1")
    repo, counter, cache = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter)

    _intake(proj, cache, "#5")
    _intake(proj, cache, "#6")

    assert _runs(counter) == 2


def test_key_depends_on_argv_lockfile_and_cleanliness(tmp_path) -> None:
    repo = _repo(tmp_path / "repo")
    proj = _CountingProj(repo, tmp_path / "count")
    base = baseline_cache.compute_key(repo, ["pytest", "-q"], proj)
    assert base is not None
    assert baseline_cache.compute_key(repo, ["pytest", "-q"], proj) == base
    assert baseline_cache.compute_key(repo, ["pytest", "-x"], proj) != base

    (repo / "uv.lock").write_text("v1\n")
    _git(repo, "add", "uv.lock")
    _git(repo, "commit", "-qm", "lock v1")
    v1 = baseline_cache.compute_key(repo, ["pytest", "-q"], proj)
    assert v1 is not None and v1 != base

    (repo / "uv.lock").write_text("v2\n")  # tracked edit => dirty => no sharing
    assert baseline_cache.compute_key(repo, ["pytest", "-q"], proj) is None
    _git(repo, "checkout", "--", "uv.lock")
    (repo / "scratch.py").write_text("x\n")  # untracked file => no sharing
    assert baseline_cache.compute_key(repo, ["pytest", "-q"], proj) is None


def test_ignored_files_do_not_block_sharing(tmp_path) -> None:
    repo = _repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".venv/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore venv")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "x").write_text("installed\n")
    assert baseline_cache.compute_key(repo, ["t"], _CountingProj(repo, tmp_path / "c")) is not None


def test_non_git_worktree_has_no_key(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert baseline_cache.compute_key(plain, ["t"], object()) is None


def test_uncaptured_result_is_not_persisted(tmp_path) -> None:
    calls: list[int] = []

    def miss() -> dict:
        calls.append(1)
        return {"captured": False, "failures": [], "note": "baseline run timed out (900s)"}

    for task in ("#5", "#6"):
        result, provenance = baseline_cache.cached_or_run(
            tmp_path, "k", tree="t" * 40, task_id=task, run_fn=miss
        )
        assert provenance is None and result["captured"] is False
    assert len(calls) == 2
    assert not (tmp_path / baseline_cache.CACHE_DIRNAME / "k.json").exists()


def test_corrupt_entry_falls_through_to_a_fresh_run(tmp_path) -> None:
    root = tmp_path / baseline_cache.CACHE_DIRNAME
    root.mkdir()
    (root / "k.json").write_text("{not json")
    calls: list[int] = []

    def fresh() -> dict:
        calls.append(1)
        return {"captured": True, "failures": [], "note": "green at base"}

    result, provenance = baseline_cache.cached_or_run(
        tmp_path, "k", tree="t" * 40, task_id="#5", run_fn=fresh
    )
    assert calls == [1] and provenance is None and result["note"] == "green at base"
    # ...and the corrupt entry was replaced by a good one the next caller reuses.
    _, provenance = baseline_cache.cached_or_run(
        tmp_path, "k", tree="t" * 40, task_id="#6", run_fn=fresh
    )
    assert calls == [1] and provenance == {"source_task": "#5", "tree": "t" * 40}


def test_concurrent_same_key_callers_run_the_suite_once(tmp_path) -> None:
    calls: list[int] = []
    gate = threading.Event()

    def slow() -> dict:
        calls.append(1)
        gate.wait(0.3)  # hold the lock long enough that the others queue behind it
        return {"captured": True, "failures": [], "note": "green at base"}

    results: list[tuple[dict, dict | None]] = []

    def worker(task: str) -> None:
        results.append(baseline_cache.cached_or_run(
            tmp_path, "k", tree="t" * 40, task_id=task, run_fn=slow
        ))

    threads = [threading.Thread(target=worker, args=(f"#{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert sum(1 for _, prov in results if prov is None) == 1
    assert sum(1 for _, prov in results if prov is not None) == 3


def test_registry_wires_the_run_dir_as_the_cache_dir(tmp_path) -> None:
    repo, counter, run_dir = _repo(tmp_path / "repo"), tmp_path / "count", tmp_path / "run"
    proj = _CountingProj(repo, counter)
    reg = build_registry(include_interactive=False, setup_project=proj, run_log_root=run_dir)
    runner = reg.resolve(_ENGINE)
    assert isinstance(runner, DeterministicSetupRunner)

    runner.dispatch(_wi("#5"))
    runner.dispatch(_wi("#6"))

    assert _runs(counter) == 1
    assert list((run_dir / baseline_cache.CACHE_DIRNAME).glob("*.json"))
