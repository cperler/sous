"""OPTIMIZE is skipped, with no model call, when a task changed only non-code files.

Live motivation: prediction ``batch-400`` task #400 was a docs-only change, yet the project's
``optimize`` roster entry put OPTIMIZE in its pipeline and an Opus/high dispatch spent $0.47 to
report ``impact: none``. The engine now reads the task's own diff at the OPTIMIZE dispatch
point and records the stage SKIPPED on the engine lane when every changed path is non-code.

Two halves: the pure classifier (``orchestrator.non_code``) and the engine wiring, against a
REAL git worktree because the decision is a git read.
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.model_table import ENGINE_MODEL
from orchestrator.non_code import (
    DEFAULT_NON_CODE_GLOBS,
    classify_non_code,
    resolve_non_code_globs,
)
from orchestrator.project_loader import validate_config
from orchestrator.render import render_completion_note
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, Stage, StageStatus
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

# --- the pure classifier ------------------------------------------------------------------


def test_an_all_docs_change_is_non_code() -> None:
    verdict = classify_non_code(
        ["README.md", "docs/guide.rst", "pkg/docs/api.md", "LICENSE", "notes/x.adoc"],
        DEFAULT_NON_CODE_GLOBS,
    )
    assert verdict.all_non_code
    assert verdict.code == ()
    assert len(verdict.non_code) == 5


def test_one_code_file_makes_the_change_code() -> None:
    verdict = classify_non_code(["README.md", "src/app.py"], DEFAULT_NON_CODE_GLOBS)
    assert not verdict.all_non_code
    assert verdict.code == ("src/app.py",)
    assert verdict.non_code == ("README.md",)


@pytest.mark.parametrize(
    "path",
    ["data/model.weird", "config.yaml", "settings.json", "pyproject.toml", "requirements.txt",
     "CMakeLists.txt", "license.py", "Makefile"],
)
def test_unknown_and_config_files_count_as_code(path) -> None:
    """Conservative: anything not matched — including config/data formats that can change
    runtime behaviour, and *.txt dependency/build files — is code."""
    assert not classify_non_code([path], DEFAULT_NON_CODE_GLOBS).all_non_code


def test_an_empty_change_is_non_code() -> None:
    verdict = classify_non_code([], DEFAULT_NON_CODE_GLOBS)
    assert verdict.all_non_code and verdict.non_code == () and verdict.code == ()


def test_override_globs_extend_and_replace() -> None:
    extended = (*DEFAULT_NON_CODE_GLOBS, "experiments/**")
    assert classify_non_code(["experiments/run1.py", "README.md"], extended).all_non_code
    assert not classify_non_code(["experiments/run1.py"], DEFAULT_NON_CODE_GLOBS).all_non_code
    # A replacement that drops the defaults no longer treats Markdown as non-code.
    assert not classify_non_code(["README.md"], ("experiments/**",)).all_non_code


# --- the adapter hook ---------------------------------------------------------------------


def test_an_adapter_without_the_hook_gets_the_defaults_and_passes_the_contract() -> None:
    project = FakeProject()
    assert not hasattr(project, "non_code_globs")
    assert validate_config(project) == []
    assert resolve_non_code_globs(project) == (DEFAULT_NON_CODE_GLOBS, None)


def test_the_hook_may_be_an_attribute_or_a_callable() -> None:
    class Attr(FakeProject):
        non_code_globs = ("experiments/**",)

    class Call(FakeProject):
        def non_code_globs(self) -> list[str]:
            return [*DEFAULT_NON_CODE_GLOBS, "experiments/**"]

    assert resolve_non_code_globs(Attr()) == (("experiments/**",), None)
    globs, notice = resolve_non_code_globs(Call())
    assert notice is None and globs[-1] == "experiments/**" and "*.md" in globs
    assert validate_config(Attr()) == [] and validate_config(Call()) == []


@pytest.mark.parametrize("value", ["*.md", 42, ["*.md", 3]])
def test_a_malformed_hook_falls_back_to_the_defaults_with_a_notice(value) -> None:
    class Bad(FakeProject):
        non_code_globs = value

    globs, notice = resolve_non_code_globs(Bad())
    assert globs == DEFAULT_NON_CODE_GLOBS and notice


def test_a_raising_hook_falls_back_to_the_defaults_with_a_notice() -> None:
    class Raises(FakeProject):
        def non_code_globs(self):
            raise RuntimeError("boom")

    globs, notice = resolve_non_code_globs(Raises())
    assert globs == DEFAULT_NON_CODE_GLOBS and notice and "boom" in notice


# --- engine wiring ------------------------------------------------------------------------


class OptimizingProject(FakeProject):
    def agent_for(self, stage: Stage, role: str | None = None):
        return (
            {"implement": "impl-agent", "review": "code-reviewer",
             "docstring": "docstring-agent", "optimize": "performance-optimizer"}
        ).get(role)


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _commit(cwd, name: str, body: str = "x") -> str:
    path = cwd / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-qm", f"change {name}")
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A real worktree whose base commit holds code; ``base`` is the task's fork point."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    base = _commit(wt, "app.py", "print('hi')\n")
    return wt, base


def _engine(tmp_path, project) -> Engine:
    store_root = tmp_path / "runs"
    return Engine(StatusStore(store_root), CostLedger(store_root / "stage-costs.jsonl"), project)


def _drive(eng: Engine, *, intake: dict):
    """Run the pipeline until the next dispatch is OPTIMIZE or TEST; return that WorkItem."""
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1"), structured_output=intake))
    while (work := eng.next_work("r1", "t1")).stage not in (Stage.OPTIMIZE, Stage.TEST):
        eng.record("r1", make_result(work))
    return work


def _events(eng: Engine, etype: str) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == etype]


def _intake(wt, base) -> dict:
    return {"branch": "b", "baseline_captured": True, "worktree": str(wt), "base_sha": base}


def test_a_docs_only_task_skips_optimize_with_no_dispatch(tmp_path, repo) -> None:
    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    _commit(wt, "docs/guide.md", "guide\n")
    eng = _engine(tmp_path, OptimizingProject())
    work = _drive(eng, intake=_intake(wt, base))

    assert work.stage is Stage.TEST  # OPTIMIZE was passed over, not dispatched
    assert not [e for e in _events(eng, "stage_dispatched") if e["stage"] == "optimize"]
    task = eng.store.load_task("r1", "t1")
    rec = task.stages[Stage.OPTIMIZE]
    assert rec.status is StageStatus.SKIPPED
    assert rec.model == ENGINE_MODEL and rec.provider is Provider.NONE
    assert rec.lane is ExecutionMode.ENGINE and rec.cost_usd == 0.0
    assert rec.output and rec.output["skipped_reason"] == "non_code"
    assert sorted(rec.output["changed_files"]) == ["README.md", "docs/guide.md"]

    (event,) = _events(eng, "optimize_skipped_non_code")
    assert event["level"] == "info"
    assert sorted(event["changed_files"]) == ["README.md", "docs/guide.md"]
    assert event["changed_files_count"] == 2 and event["changed_files_truncated"] is False
    assert event["non_code_globs"] == list(DEFAULT_NON_CODE_GLOBS)

    assert eng.status("r1")["tasks"]["t1"]["stages"]["optimize"] == "skipped"
    # Nothing was dispatched, so nothing is left to balance, and no ledger row was written.
    assert eng.events_audit("r1")["clean"]
    assert eng.lane_audit("r1")["clean"]
    assert not [r for r in eng.run_rows("r1") if r.get("stage") == "optimize"]


def test_the_skip_is_recorded_not_recomputed(tmp_path, repo) -> None:
    """Resume reads the SKIPPED record back: a code change landing after the decision does
    not resurrect the stage, and the event is not re-emitted."""
    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, OptimizingProject())
    work = _drive(eng, intake=_intake(wt, base))
    _commit(wt, "late.py", "x = 1\n")
    eng.record("r1", make_result(work))  # TEST
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.DELIVER
    assert eng.store.load_task("r1", "t1").stages[Stage.OPTIMIZE].status is StageStatus.SKIPPED
    assert len(_events(eng, "optimize_skipped_non_code")) == 1


def test_a_code_change_still_dispatches_optimize(tmp_path, repo) -> None:
    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    _commit(wt, "app.py", "print('faster')\n")
    eng = _engine(tmp_path, OptimizingProject())
    work = _drive(eng, intake=_intake(wt, base))
    assert work.stage is Stage.OPTIMIZE
    assert not _events(eng, "optimize_skipped_non_code")
    assert not _events(eng, "optimize_skip_check_failed")


def test_an_uncommitted_code_file_still_counts(tmp_path, repo) -> None:
    """The read covers the worktree, not just commits: untracked code is code."""
    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    (wt / "new_module.py").write_text("y = 2\n")
    eng = _engine(tmp_path, OptimizingProject())
    assert _drive(eng, intake=_intake(wt, base)).stage is Stage.OPTIMIZE


def test_a_missing_base_sha_dispatches_with_a_notice(tmp_path, repo) -> None:
    wt, _ = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, OptimizingProject())
    intake = {"branch": "b", "baseline_captured": True, "worktree": str(wt)}
    work = _drive(eng, intake=intake)
    assert work.stage is Stage.OPTIMIZE
    (notice,) = _events(eng, "optimize_skip_check_failed")
    assert "base_sha" in notice["reason"]


def test_a_git_failure_dispatches_with_a_notice(tmp_path, repo) -> None:
    wt, _ = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, OptimizingProject())
    work = _drive(eng, intake=_intake(wt, "0" * 40))  # an object git does not have
    assert work.stage is Stage.OPTIMIZE
    (notice,) = _events(eng, "optimize_skip_check_failed")
    assert "failed" in notice["reason"]


def test_a_missing_worktree_dispatches_with_a_notice(tmp_path, repo) -> None:
    _, base = repo
    eng = _engine(tmp_path, OptimizingProject())
    work = _drive(eng, intake=_intake(tmp_path / "gone", base))
    assert work.stage is Stage.OPTIMIZE
    (notice,) = _events(eng, "optimize_skip_check_failed")
    assert "worktree" in notice["reason"]


def test_an_adapter_override_extends_the_globs(tmp_path, repo) -> None:
    class Experiments(OptimizingProject):
        non_code_globs = (*DEFAULT_NON_CODE_GLOBS, "experiments/**")

    wt, base = repo
    _commit(wt, "experiments/run1.py", "print(1)\n")
    eng = _engine(tmp_path, Experiments())
    assert _drive(eng, intake=_intake(wt, base)).stage is Stage.TEST
    (event,) = _events(eng, "optimize_skipped_non_code")
    assert "experiments/**" in event["non_code_globs"]


def test_an_empty_override_opts_out_of_the_skip(tmp_path, repo) -> None:
    class NeverSkip(OptimizingProject):
        non_code_globs = ()

    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, NeverSkip())
    assert _drive(eng, intake=_intake(wt, base)).stage is Stage.OPTIMIZE


def test_a_broken_override_is_evented_and_uses_the_defaults(tmp_path, repo) -> None:
    class Broken(OptimizingProject):
        def non_code_globs(self):
            raise RuntimeError("bad adapter")

    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, Broken())
    assert _drive(eng, intake=_intake(wt, base)).stage is Stage.TEST
    (warn,) = _events(eng, "non_code_globs_invalid")
    assert warn["level"] == "warning" and "bad adapter" in warn["detail"]


def test_the_completion_note_says_the_pass_was_skipped(tmp_path, repo) -> None:
    wt, base = repo
    _commit(wt, "README.md", "# docs\n")
    eng = _engine(tmp_path, OptimizingProject())
    _drive(eng, intake=_intake(wt, base))
    note = render_completion_note(eng.store.load_task("r1", "t1"))
    assert "Speed pass skipped — only non-code files changed" in note
    assert "1 changed file(s)" in note
