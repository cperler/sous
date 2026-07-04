"""Intake install caching — lockfile-hash skip (#63).

Two layers: pure-unit coverage of the ``install_cache`` decision helpers, and
real-git-in-tmp integration through ``DeterministicSetupRunner`` proving the skip is
taken ONLY when it is provably safe — the key subtlety being that installs write INTO
the worktree, so the cache is scoped per-worktree (a fresh worktree never skips)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adapters.execution import install_cache as ic
from adapters.execution.deterministic_setup import DeterministicSetupRunner
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


def _wi(task: str = "#7") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task, stage=Stage.INTAKE, prompt="p",
        schema_ref="intake", model="engine", lane_policy=_ENGINE, created_at="now",
    )


# --- pure unit: the decision helpers --------------------------------------------------

def test_project_lockfiles_merges_optional_override() -> None:
    assert ic.project_lockfiles(object()) == list(ic.DEFAULT_LOCKFILES)

    class Attr:
        lockfiles = ["custom.lock", "uv.lock"]  # dedup: uv.lock already present

    names = ic.project_lockfiles(Attr())
    assert "custom.lock" in names and names.count("uv.lock") == 1

    class Callable_:
        def lockfiles(self):
            return ["q.lock"]

    assert "q.lock" in ic.project_lockfiles(Callable_())


def test_compute_hash_none_without_lockfiles_and_changes_with_content(tmp_path) -> None:
    assert ic.compute_hash([]) is None
    lock = tmp_path / "uv.lock"
    lock.write_text("v1")
    h1 = ic.compute_hash([lock])
    lock.write_text("v2")
    h2 = ic.compute_hash([lock])
    assert h1 and h2 and h1 != h2


def test_discover_finds_present_lockfiles_sorted(tmp_path) -> None:
    (tmp_path / "uv.lock").write_text("1")
    (tmp_path / "package-lock.json").write_text("2")
    found = ic.discover(tmp_path, list(ic.DEFAULT_LOCKFILES))
    assert [p.name for p in found] == ["package-lock.json", "uv.lock"]


def test_should_skip_requires_recorded_success_and_matching_hash(tmp_path) -> None:
    marker = tmp_path / "m.json"
    ic.save_marker(marker, digest="abc", lockfiles=["uv.lock"], success=True)
    assert ic.should_skip(marker, "abc") is True
    assert ic.should_skip(marker, "different") is False  # hash mismatch
    assert ic.should_skip(None, "abc") is False  # no marker location
    assert ic.should_skip(marker, None) is False  # no lockfiles hashed
    ic.save_marker(marker, digest="abc", lockfiles=["uv.lock"], success=False)
    assert ic.should_skip(marker, "abc") is False  # previous install FAILED


def test_should_skip_false_on_corrupt_marker(tmp_path) -> None:
    marker = tmp_path / "m.json"
    marker.write_text("{not json")
    assert ic.should_skip(marker, "abc") is False


def test_env_escape_hatch_disables_skip(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "m.json"
    ic.save_marker(marker, digest="abc", lockfiles=[], success=True)
    monkeypatch.setenv(ic.ENV_DISABLE, "1")
    assert ic.cache_disabled() is True
    assert ic.should_skip(marker, "abc") is False


# --- integration: through the runner, real git worktrees ------------------------------

def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=path, check=True)


def _repo_with_lock(path: Path, content: str = "v1") -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    _git(path, "commit", "--allow-empty", "-qm", "init")
    (path / "uv.lock").write_text(content)
    _git(path, "add", "uv.lock")
    _git(path, "commit", "-qm", "lock")


class _InstallProj:
    """Install writes a sentinel INTO the worktree cwd, so its presence proves the install
    ran (and its absence after a skip proves it did NOT)."""

    def __init__(self, cmd: list[str] | None = None) -> None:
        self._cmd = cmd or ["sh", "-lc", "touch deps.installed"]

    def install_cmd(self) -> list[str]:
        return self._cmd

    def test_unit_cmd(self, files=None) -> list[str]:
        return ["true"]


def _marker(worktree: Path) -> Path:
    gd = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(gd) / ic.MARKER_NAME


def test_install_skipped_on_lockfile_hash_match(tmp_path, monkeypatch) -> None:
    _repo_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())
    out1 = r1.structured_output
    assert out1["install_skipped"] is False and out1["install_reason"] == "installed"
    assert out1["install_lockfiles"] == ["uv.lock"]
    wt = Path(out1["worktree"])
    assert (wt / "deps.installed").exists()  # install genuinely ran

    (wt / "deps.installed").unlink()  # a skip must NOT recreate it
    r2 = runner.dispatch(_wi())  # same task -> same worktree, unchanged lockfile
    out2 = r2.structured_output
    assert out2["install_skipped"] is True and out2["install_reason"] == "lockfile-hash-match"
    assert not (wt / "deps.installed").exists()  # the install was truly skipped
    assert "skipped (lockfile-hash-match)" in out2["baseline"]  # visible in the human note


def test_full_install_on_lockfile_hash_mismatch(tmp_path, monkeypatch) -> None:
    _repo_with_lock(tmp_path, "v1")
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())
    wt = Path(r1.structured_output["worktree"])
    (wt / "deps.installed").unlink()
    (wt / "uv.lock").write_text("v2")  # working-tree lockfile change -> hash differs

    r2 = runner.dispatch(_wi())
    assert r2.structured_output["install_skipped"] is False
    assert (wt / "deps.installed").exists()  # reinstalled


def test_full_install_when_no_lockfiles(tmp_path, monkeypatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _git(tmp_path, "commit", "--allow-empty", "-qm", "init")
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())
    assert r1.structured_output["install_skipped"] is False
    assert r1.structured_output["install_reason"] == "no-lockfiles"
    r2 = runner.dispatch(_wi())  # no hash basis -> never skips
    assert r2.structured_output["install_skipped"] is False


def test_full_install_on_unreadable_cache(tmp_path, monkeypatch) -> None:
    _repo_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())
    wt = Path(r1.structured_output["worktree"])
    _marker(wt).write_text("{corrupt")  # cache unreadable
    (wt / "deps.installed").unlink()

    r2 = runner.dispatch(_wi())
    assert r2.structured_output["install_skipped"] is False  # doubt -> reinstall
    assert (wt / "deps.installed").exists()


def test_full_install_when_previous_install_failed(tmp_path, monkeypatch) -> None:
    _repo_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj(["sh", "-lc", "touch deps.installed; exit 3"]))

    r1 = runner.dispatch(_wi())
    assert r1.structured_output["install_skipped"] is False
    assert r1.structured_output["install_reason"] == "install-failed"
    wt = Path(r1.structured_output["worktree"])
    (wt / "deps.installed").unlink()

    r2 = runner.dispatch(_wi())  # prior FAILURE recorded -> must not skip
    assert r2.structured_output["install_skipped"] is False
    assert (wt / "deps.installed").exists()


def test_fresh_worktree_never_skips_even_with_identical_lockfile(tmp_path, monkeypatch) -> None:
    """THE correctness test for #63: because installs write into the worktree (.venv /
    node_modules), a recreated worktree — same path, same lockfile, but NO deps present —
    must full-install. The per-worktree git-dir marker is dropped with the worktree, so no
    stale skip survives a post-cleanup re-run."""
    _repo_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())
    wt = Path(r1.structured_output["worktree"])
    assert (wt / "deps.installed").exists()

    # Post-cleanup: git worktree remove drops the worktree AND its private git dir/marker.
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=tmp_path, check=True)

    r2 = runner.dispatch(_wi())  # recreated worktree: identical lockfile, but no deps
    wt2 = Path(r2.structured_output["worktree"])
    assert r2.structured_output["install_skipped"] is False  # correct: reinstall
    assert (wt2 / "deps.installed").exists()  # deps really rebuilt in the fresh worktree


def test_env_escape_hatch_forces_full_install(tmp_path, monkeypatch) -> None:
    _repo_with_lock(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_InstallProj())

    r1 = runner.dispatch(_wi())  # records the marker while caching is enabled
    wt = Path(r1.structured_output["worktree"])
    (wt / "deps.installed").unlink()

    monkeypatch.setenv(ic.ENV_DISABLE, "1")
    r2 = runner.dispatch(_wi())  # would match the hash, but the escape hatch forces install
    assert r2.structured_output["install_skipped"] is False
    assert r2.structured_output["install_reason"] == "cache-disabled"
    assert (wt / "deps.installed").exists()
