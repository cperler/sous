"""The deterministic ENGINE-lane intake runner: worktree/baseline in
shell, no model call. Real-git-in-tmp coverage (mirrors tests/test_checkpoint.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from adapters.execution.deterministic_setup import DeterministicSetupRunner
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE, allow_fallback=False)


class _Proj:
    """A project WITHOUT setup_task -> exercises the built-in git logic. Noop install,
    fast real unit-test command (so the baseline is genuinely captured)."""

    def install_cmd(self) -> list[str]:
        return ["true"]

    def test_unit_cmd(self, files=None) -> list[str]:
        return ["sh", "-c", "exit 0"]


def _wi(task: str = "#7", context: dict | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task, stage=Stage.INTAKE, prompt="p",
        schema_ref="intake", model="engine", lane_policy=_ENGINE, created_at="now",
        checkpoint_tag="task/r1/-7/intake/0", context=context,
    )


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "init"], cwd=path, check=True)


def _commit(path: Path, name: str, content: str, msg: str) -> None:
    (path / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", msg], cwd=path, check=True)


def _head(path: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def _branch(path: Path) -> str:
    return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def test_real_git_setup_creates_worktree_and_tags(tmp_path, monkeypatch) -> None:
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.model == "engine"  # the $0 sentinel, not a real model
    assert (res.lane_used.execution_mode, res.lane_used.provider) == (
        ExecutionMode.ENGINE, Provider.NONE)
    out = res.structured_output
    assert out["branch"] == "task/7" and out["baseline_captured"] is True
    assert out["baseline_failures"] == [] and "green at base" in out["baseline"]
    wt = Path(out["worktree"])
    assert wt.exists() and (wt / ".git").exists()  # a real linked worktree
    branches = subprocess.run(["git", "branch"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "task/7" in branches
    assert res.checkpoint and res.checkpoint["tag"] == "task/r1/-7/intake/0"  # baseline anchor


def test_explicit_repo_root_used_instead_of_cwd(tmp_path, monkeypatch) -> None:
    """#42: when the project exposes ``repo_root``, intake discovers the repo from that
    explicit path — NOT process CWD. Proven by chdir'ing to a NON-git dir: a CWD-bound
    lookup would fail 'not inside a git repository', so success can only come from the
    explicit path."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git_repo(repo)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # process CWD is deliberately NOT the (or any) git repo

    class P(_Proj):
        repo_root = str(repo)

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["branch"] == "task/7"
    # the worktree was created under the EXPLICIT repo, not under CWD.
    wt = Path(out["worktree"])
    assert wt.exists() and str(wt).startswith(str(repo))
    assert not (elsewhere / ".worktrees").exists()


def test_reuse_existing_worktree_is_idempotent(tmp_path, monkeypatch) -> None:
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_Proj())

    first = runner.dispatch(_wi())
    second = runner.dispatch(_wi())  # retry: reuse the same worktree, no error

    assert first.status is second.status is ResultStatus.SUCCESS
    assert first.structured_output["worktree"] == second.structured_output["worktree"]


def test_non_git_dir_fails_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # a bare tmp dir, not a git repo

    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE  # a classifiable failure, not a schema_violation
    assert "git" in (res.error or "").lower()


def test_dispatch_yields_failure_when_setup_task_raises(tmp_path, monkeypatch) -> None:
    # Every dispatch MUST yield a StageResult — a raising setup_task (or a _git timeout)
    # must become FAILURE, never an escaped exception that leaves the lease held.
    monkeypatch.chdir(tmp_path)

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]

        def setup_task(self, task_id: str) -> dict:
            raise RuntimeError("boom")

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE
    assert "boom" in (res.error or "")


def test_baseline_not_fabricated_without_test_command(tmp_path, monkeypatch) -> None:
    """The old stub reported baseline_captured=True without running anything. Honest
    now: no unit-test command (or the ["true"] no-op sentinel) => captured False."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]

        def test_unit_cmd(self, files=None) -> list[str]:
            return ["true"]  # the no-op sentinel: nothing actually runs

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS  # setup itself is fine
    out = res.structured_output
    assert out["baseline_captured"] is False
    assert "no unit-test command" in out["baseline"]


def test_red_baseline_records_classified_failures(tmp_path, monkeypatch) -> None:
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    class _Classifier:
        def classify(self, test_output):
            from orchestrator.failure_classifier import Failure

            return [
                Failure(test=line.split(" ", 1)[1], kind="unit")
                for line in test_output.splitlines()
                if line.startswith("FAILED ")
            ]

        def impacted_tests(self, changed_files):
            return []

    class P:
        classifier = _Classifier()

        def install_cmd(self) -> list[str]:
            return ["true"]

        def test_unit_cmd(self, files=None) -> list[str]:
            return ["sh", "-c", "echo 'FAILED tests/test_x.py::t1'; exit 1"]

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    out = res.structured_output
    assert out["baseline_captured"] is True  # the suite RAN — red is still a baseline
    assert out["baseline_failures"] == ["tests/test_x.py::t1"]
    assert "RED at base" in out["baseline"] and "1 known-failing" in out["baseline"]


def test_dep_branch_composed_into_dependent_worktree(tmp_path, monkeypatch) -> None:
    """#216: a dependent's worktree provably CONTAINS its dependency's shared-signature
    change (the reduced #161/#194 ModelId scenario), so its gate runs against composed
    code — not pre-dep trunk. The base_sha the TEST stage will diff from is captured AFTER
    the merge, and the merged branch is recorded in composed_deps + the baseline note."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    # The pre-#161 world: a shared module typed permissively as ``str``.
    _commit(tmp_path, "shared.py", "model: str = 'x'\n", "add shared")
    base = _head(tmp_path)
    default = _branch(tmp_path)
    # A DEPENDENCY branch tightens the shared signature (the #161 ModelId retype).
    subprocess.run(["git", "checkout", "-q", "-b", "task/dep"], cwd=tmp_path, check=True)
    _commit(tmp_path, "shared.py", "ModelId = str\nmodel: ModelId = 'x'\n", "retype (#161)")
    subprocess.run(["git", "checkout", "-q", default], cwd=tmp_path, check=True)

    res = DeterministicSetupRunner(_Proj()).dispatch(
        _wi(context={"dep_branches": ["task/dep"]})
    )

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    wt = Path(out["worktree"])
    # The dependency's change is physically present in the dependent's worktree.
    assert "ModelId" in (wt / "shared.py").read_text()
    assert out["composed_deps"] == ["task/dep"]
    assert "composed deps: task/dep" in out["baseline"]
    # base_sha advanced past the run base (it reflects the composed HEAD).
    assert out["base_sha"] and out["base_sha"] != base


def test_dep_branch_merge_conflict_fails_loudly(tmp_path, monkeypatch) -> None:
    """#216: a conflict between a dependency and the dependent's base surfaces as a setup
    FAILURE naming the conflicting branch — never a silent green pass on a half-composed
    tree. The worktree is rolled back (merge --abort) so a retry starts clean."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _commit(tmp_path, "shared.py", "v = 1\n", "base A")
    default = _branch(tmp_path)
    subprocess.run(["git", "branch", "task/dep"], cwd=tmp_path, check=True)  # branch at A
    subprocess.run(["git", "checkout", "-q", "task/dep"], cwd=tmp_path, check=True)
    _commit(tmp_path, "shared.py", "v = 2\n", "dep edit")
    subprocess.run(["git", "checkout", "-q", default], cwd=tmp_path, check=True)
    _commit(tmp_path, "shared.py", "v = 3\n", "base B (diverged)")  # conflicts with dep

    res = DeterministicSetupRunner(_Proj()).dispatch(
        _wi(context={"dep_branches": ["task/dep"]})
    )

    assert res.status is ResultStatus.FAILURE
    err = (res.error or "").lower()
    assert "conflict" in err and "task/dep" in (res.error or "")


def test_no_dep_branches_is_byte_identical_base_only(tmp_path, monkeypatch) -> None:
    """#216: a task with no dep_branches (single-task run / disjoint batch) behaves exactly
    as before — base_sha is the run base, and no dep is composed."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _commit(tmp_path, "f.py", "x = 1\n", "seed")
    head = _head(tmp_path)

    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())  # no context / no deps

    assert res.status is ResultStatus.SUCCESS
    out = res.structured_output
    assert out["base_sha"] == head  # worktree HEAD == run base, unchanged
    assert out["composed_deps"] == []
    assert "composed deps" not in out["baseline"]


def test_project_setup_task_override_skips_git(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # NOT a git repo — the override must not touch git
    worktree = tmp_path / "custom-worktree"
    worktree.mkdir()

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]

        def setup_task(self, task_id: str) -> dict:
            return {"branch": f"b/{task_id.lstrip('#')}", "worktree": str(worktree),
                    "baseline_captured": True}

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output["branch"] == "b/7"
    assert not (tmp_path / ".worktrees").exists()


def test_project_setup_task_override_repairs_sibling_environment_before_baseline(
    tmp_path, monkeypatch
) -> None:
    """Custom provisioning cannot carry a sibling environment into baseline tests."""
    monkeypatch.chdir(tmp_path)  # NOT a git repo — setup_task remains the provisioning seam
    worktree = tmp_path / "review"
    sibling = tmp_path / "sibling" / ".venv"
    (worktree / ".venv").mkdir(parents=True)
    sibling.mkdir(parents=True)
    (worktree / ".venv" / "origin").write_text(f"{sibling}\n")

    class P:
        def install_cmd(self) -> list[str]:
            return ["sh", "-c", "mkdir -p .venv && pwd > .venv/origin"]

        def test_unit_cmd(self, files=None) -> list[str]:
            return [
                "sh", "-c",
                'test "$(cat .venv/origin)" = "$PWD" && touch baseline-ran',
            ]

        def fresh_install_paths(self) -> list[str]:
            return [".venv"]

        def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
            return [("environment source", ["sh", "-c", "cat .venv/origin"])]

        def setup_task(self, task_id: str) -> dict:
            return {
                "branch": f"b/{task_id.lstrip('#')}",
                "worktree": str(worktree),
                # These unverified claims must be replaced by the harness-owned baseline.
                "baseline_captured": True,
                "baseline_failures": ["bogus"],
            }

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    assert res.structured_output["baseline_captured"] is True
    assert res.structured_output["baseline_failures"] == []
    assert (worktree / "baseline-ran").exists()
    assert (worktree / ".venv" / "origin").read_text().strip() == str(worktree)
    # Since #381 the inherited environment is discarded on provisioning grounds alone, so the
    # sibling is gone BEFORE any probe could report a mismatch. The discard is the record.
    reset = next(
        notice for notice in res.execution_notices
        if notice["notice"] == "worktree_environment_reset"
    )
    assert reset["expected_worktree"] == str(worktree.resolve())
    assert not any(
        notice["notice"] == "worktree_origin_mismatch" for notice in res.execution_notices
    )
    assert not sibling.joinpath("origin").exists()  # cleanup never escaped the worktree


def test_probeless_override_still_discards_inherited_environment(
    tmp_path, monkeypatch
) -> None:
    """Declaring fresh_install_paths without probes must not trust an inherited environment.

    worktree_origin_probes is optional by contract, so verification returns a trusted SKIP.
    Before #381's fix that let a copied .venv survive an installer which preserves stale
    launchers — the false green this whole mechanism exists to prevent.
    """
    monkeypatch.chdir(tmp_path)
    worktree = tmp_path / "review"
    sibling = tmp_path / "sibling" / ".venv"
    (worktree / ".venv").mkdir(parents=True)
    sibling.mkdir(parents=True)
    # A stale launcher an incremental `uv sync`-style install would leave untouched.
    (worktree / ".venv" / "origin").write_text(f"{sibling}\n")

    class P:
        def install_cmd(self) -> list[str]:
            # Incremental: only writes origin when the environment was actually removed.
            return ["sh", "-c", "mkdir -p .venv && [ -f .venv/origin ] || pwd > .venv/origin"]

        def test_unit_cmd(self, files=None) -> list[str]:
            return ["sh", "-c", 'test "$(cat .venv/origin)" = "$PWD" && touch baseline-ran']

        def fresh_install_paths(self) -> list[str]:
            return [".venv"]

        # NOTE: no worktree_origin_probes — the supported, unverifiable configuration.

        def setup_task(self, task_id: str) -> dict:
            return {"branch": f"b/{task_id.lstrip('#')}", "worktree": str(worktree)}

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.SUCCESS
    # The sibling-pointing launcher is gone: the install rebuilt it in THIS worktree.
    assert (worktree / ".venv" / "origin").read_text().strip() == str(worktree)
    assert (worktree / "baseline-ran").exists()
    assert res.structured_output["baseline_captured"] is True
    # The discard is auditable even though no probe reported a mismatch.
    reset = next(
        notice for notice in res.execution_notices
        if notice["notice"] == "worktree_environment_reset"
    )
    assert reset["expected_worktree"] == str(worktree.resolve())
    assert reset["reason"] == "freshly provisioned worktree"


def test_project_setup_task_override_refuses_unrepairable_sibling_environment(
    tmp_path, monkeypatch
) -> None:
    """A stale override environment cannot retain a fabricated green baseline claim."""
    monkeypatch.chdir(tmp_path)
    worktree = tmp_path / "review"
    sibling = tmp_path / "sibling" / ".venv"
    (worktree / ".venv").mkdir(parents=True)
    sibling.mkdir(parents=True)
    (worktree / ".venv" / "origin").write_text(f"{sibling}\n")

    class P:
        def install_cmd(self) -> list[str]:
            return ["true"]  # cannot recreate the removed environment

        def test_unit_cmd(self, files=None) -> list[str]:
            return ["sh", "-c", "touch should-not-run"]

        def fresh_install_paths(self) -> list[str]:
            return [".venv"]

        def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
            return [("environment source", ["sh", "-c", "cat .venv/origin"])]

        def setup_task(self, task_id: str) -> dict:
            return {
                "branch": f"b/{task_id.lstrip('#')}",
                "worktree": str(worktree),
                "baseline_captured": True,
            }

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE
    assert not (worktree / "should-not-run").exists()
    # The stale environment is discarded first; the installer cannot rebuild it, so the
    # post-install probe has nothing to resolve and fails closed rather than passing.
    assert not (worktree / ".venv").exists()
    failed = next(
        notice for notice in res.execution_notices
        if notice["notice"] == "worktree_origin_probe_failed"
    )
    assert failed["expected_worktree"] == str(worktree)
    assert failed["probe"] == "environment source"


# --- #385: uncommitted work inherited from a previous, dead run ---------------------------


def _dirty(path: Path) -> None:
    """Leave a staged, an unstaged, and an untracked change behind — the shape a run that
    died mid-flight (rate-limited at DELIVER) leaves in its worktree."""
    _commit(path, "tracked.txt", "base\n", "tracked")
    (path / "tracked.txt").write_text("edited by a dead run\n")  # unstaged
    (path / "staged.txt").write_text("staged\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=path, check=True)
    (path / "untracked.txt").write_text("untracked\n")  # untracked


def test_reused_dirty_worktree_is_reported_and_evented(tmp_path, monkeypatch) -> None:
    """The near-miss this exists for: a dead run left finished work uncommitted, the next
    run REUSES that worktree, and intake must SAY SO — in its output, in the folded context,
    and in the event stream — instead of letting IMPLEMENT discover it by overwriting it."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_Proj())

    first = runner.dispatch(_wi())
    worktree = Path(first.structured_output["worktree"])
    assert first.structured_output["inherited_changes_count"] == 0  # fresh: explicitly clean
    _dirty(worktree)  # a previous run dies here, leaving work uncommitted

    second = runner.dispatch(_wi())  # a NEW run picks the task back up

    out = second.structured_output
    assert second.status is ResultStatus.SUCCESS  # reporting it is not a failure
    assert out["inherited_changes_count"] == 3
    entries = out["inherited_changes"]
    assert {line.split()[-1] for line in entries} == {
        "tracked.txt", "staged.txt", "untracked.txt"}
    assert "did not make them" in out["inherited_changes_note"]
    notice = next(n for n in second.execution_notices
                  if n["notice"] == "worktree_inherited_changes")
    assert notice["count"] == 3 and notice["expected_worktree"] == str(worktree)
    # Report, don't act: the inherited work is still there, untouched.
    assert (worktree / "untracked.txt").exists()
    assert (worktree / "tracked.txt").read_text() == "edited by a dead run\n"


def test_clean_reused_worktree_reports_clean_without_a_notice(tmp_path, monkeypatch) -> None:
    """An ordinary in-run retry must stay quiet: an explicit clean signal (so 'clean' and
    'never looked' don't read alike) and no warning event."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_Proj())

    runner.dispatch(_wi())
    second = runner.dispatch(_wi())

    out = second.structured_output
    assert out["inherited_changes"] == [] and out["inherited_changes_count"] == 0
    assert out["inherited_changes_note"].startswith("clean")
    assert not any(n["notice"] == "worktree_inherited_changes"
                   for n in second.execution_notices)


def test_install_artifacts_are_not_reported_as_inherited(tmp_path, monkeypatch) -> None:
    """The read happens BEFORE install, so this run's own dependency artifacts can never be
    mistaken for a dead run's leftovers."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    class P(_Proj):
        def install_cmd(self) -> list[str]:
            return ["sh", "-c", "echo junk > installed-artifact.txt"]

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    out = res.structured_output
    assert (Path(out["worktree"]) / "installed-artifact.txt").exists()  # install really ran
    assert out["inherited_changes"] == [] and out["inherited_changes_count"] == 0


def test_large_inherited_diff_is_truncated_but_the_count_is_true(tmp_path, monkeypatch) -> None:
    """A 2,400-line rewrite starts as a few dozen files: the LIST is bounded for the prompt,
    while the COUNT stays honest so nobody reads the cap as the size of the loss."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = DeterministicSetupRunner(_Proj())

    worktree = Path(runner.dispatch(_wi()).structured_output["worktree"])
    for i in range(25):
        (worktree / f"f{i:02d}.txt").write_text("x\n")

    out = runner.dispatch(_wi()).structured_output

    assert out["inherited_changes_count"] == 25  # the truth
    assert len(out["inherited_changes"]) == 21  # 20 entries + the explicit tail
    assert out["inherited_changes"][-1] == "… (5 more)"


def test_unreadable_status_never_reports_clean(tmp_path, monkeypatch) -> None:
    """A git that cannot answer must yield UNKNOWN, not a fabricated clean tree: the note
    says unreadable, the count is absent (absent = unknown), and it is evented."""
    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    import adapters.execution.deterministic_setup as ds

    real_git = ds._git

    def flaky_git(cwd: str, *args: str):
        if args[:1] == ("status",):
            raise subprocess.TimeoutExpired(cmd="git status", timeout=1)
        return real_git(cwd, *args)

    monkeypatch.setattr(ds, "_git", flaky_git)
    res = DeterministicSetupRunner(_Proj()).dispatch(_wi())

    out = res.structured_output
    assert res.status is ResultStatus.SUCCESS  # never fatal
    assert out["inherited_changes_note"].startswith("unreadable: TimeoutExpired")
    assert "inherited_changes_count" not in out  # absent = unknown, never 0
    assert any(n["notice"] == "worktree_status_unreadable" for n in res.execution_notices)


def test_non_git_override_worktree_reports_n_a_without_a_warning(tmp_path, monkeypatch) -> None:
    """A project ``setup_task`` may legitimately provision a non-git worktree; that is an
    honest n/a, not a fault — so it must not cry wolf on every dispatch."""
    monkeypatch.chdir(tmp_path)  # deliberately NOT a git repo
    worktree = tmp_path / "custom"
    worktree.mkdir()

    class P(_Proj):
        def setup_task(self, task_id: str) -> dict:
            return {"branch": "b/7", "worktree": str(worktree), "baseline_captured": False}

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    out = res.structured_output
    assert out["inherited_changes_note"] == "n/a (not a git worktree)"
    assert not any(n["notice"] in ("worktree_inherited_changes", "worktree_status_unreadable")
                   for n in res.execution_notices)


def test_override_worktree_reports_inherited_changes(tmp_path, monkeypatch) -> None:
    """The provisioning override is a seam, not an exemption (the #381 review lesson): a
    dirty directory handed back by ``setup_task`` is reported the same way, and the
    engine-verified fields override whatever the project claimed."""
    repo = tmp_path / "product"
    repo.mkdir()
    _git_repo(repo)
    monkeypatch.chdir(tmp_path)
    _dirty(repo)

    class P(_Proj):
        def setup_task(self, task_id: str) -> dict:
            return {
                "branch": "b/7",
                "worktree": str(repo),
                "inherited_changes": ["a lie the project told"],  # must not survive
                "inherited_changes_note": "clean",
            }

    out = DeterministicSetupRunner(P()).dispatch(_wi()).structured_output

    assert out["inherited_changes_count"] == 3
    assert "a lie the project told" not in out["inherited_changes"]
    assert "did not make them" in out["inherited_changes_note"]


def test_inherited_changes_survive_a_failed_intake(tmp_path, monkeypatch) -> None:
    """A FAILED intake is exactly when a human most needs to know the tree was already
    dirty, so the report must not be swallowed when install/origin verification aborts."""
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "product"
    repo.mkdir()
    _git_repo(repo)
    _dirty(repo)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "origin").write_text(f"{tmp_path / 'sibling'}\n")

    class P(_Proj):
        def fresh_install_paths(self) -> list[str]:
            return [".venv"]

        def worktree_origin_probes(self) -> list[tuple[str, list[str]]]:
            return [("environment source", ["sh", "-c", "cat .venv/origin"])]

        def setup_task(self, task_id: str) -> dict:
            return {"branch": "b/7", "worktree": str(repo), "baseline_captured": True}

    res = DeterministicSetupRunner(P()).dispatch(_wi())

    assert res.status is ResultStatus.FAILURE  # the origin refusal still stands
    inherited = next(n for n in res.execution_notices
                     if n["notice"] == "worktree_inherited_changes")
    assert inherited["count"] == 4  # the three dirty entries plus the stale .venv
