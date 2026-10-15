"""#558: TEST runs only the tests impacted by the change (adapter hook, full-suite fallback).

Covers the engine-side decision (``orchestrator.test_selection``), the deterministic TEST
runner that acts on it, the fold guard on the recorded scope, the model TEST prompt hint,
the self-host mapping, and the status/dashboard exposure of the scope.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from adapters.execution.deterministic_test import DeterministicTestRunner
from adapters.project.selfhost.config import SelfHostConfig, impacted_tests_for
from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import _stage_progress
from orchestrator.dashboard_render import _stage_strip
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
)
from orchestrator.schemas.stage_schemas import load_stage_schema
from orchestrator.schemas.status import Task
from orchestrator.schemas.work import LanePolicy, LaneUsed, StageResult, TokenUsage, WorkItem
from orchestrator.stages import render_prompt
from orchestrator.state_machine import CONTEXT_KEYS, DETERMINISTIC_ONLY_KEYS, _absorb_outputs
from orchestrator.status_store import StatusStore
from orchestrator.test_selection import (
    changed_files,
    matches_global,
    select_tests,
)

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE)


# --- fixtures ---------------------------------------------------------------------------
def _git(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> str:
    """A git repo with one source module and its test; returns the base sha."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True, check=True).stdout.strip()


class _Proj:
    """Records the argv requests the runner makes. ``hook`` is the impacted_tests body."""

    classifier = None

    def __init__(self, hook=None, *, global_paths=(), e2e=None, ignores_files=False):
        self.calls: list[tuple[str, object]] = []
        self._e2e = e2e
        self._ignores_files = ignores_files
        if hook is not None:
            self.impacted_tests = hook
        self.test_global_paths = list(global_paths)

    def test_unit_cmd(self, files=None):
        self.calls.append(("unit", files))
        if files and not self._ignores_files:
            return ["sh", "-c", "exit 0", "unit", *files]
        return ["sh", "-c", "exit 0", "unit-full"]

    def test_e2e_cmd(self, files=None):
        self.calls.append(("e2e", files))
        return self._e2e

    def test_shell_cmd(self, files=None):
        return None


def _wi(cwd, base_sha) -> WorkItem:
    ctx: dict = {"baseline_failures": []}
    if base_sha is not None:
        ctx["base_sha"] = base_sha
    return WorkItem.create(
        id="wi", run_id="r", task_id="#558", stage=Stage.TEST, prompt="p",
        schema_ref="test", model="engine", lane_policy=_ENGINE, created_at="now",
        cwd=str(cwd), context=ctx,
    )


def _run(tmp_path, proj, *, base=None):
    res = DeterministicTestRunner(proj).dispatch(_wi(tmp_path, base))
    Draft202012Validator(load_stage_schema("test")).validate(res.structured_output)
    return res


# --- select_tests: the happy path and every fallback ------------------------------------
def test_select_tests_happy_path_is_impacted_and_deduped() -> None:
    proj = _Proj(lambda changed: ["tests/test_a.py", "tests/test_a.py", " tests/test_b.py "])
    sel = select_tests(proj, ["src/a.py", "src/b.py"])
    assert sel.scope == "impacted"
    assert sel.files == ("tests/test_a.py", "tests/test_b.py")


def test_select_tests_passes_cwd_only_to_a_hook_that_accepts_it() -> None:
    seen: dict = {}

    def with_cwd(changed, *, cwd=None):
        seen["cwd"] = cwd
        return ["tests/test_a.py"]

    assert select_tests(_Proj(with_cwd), ["a.py"], cwd="/wt").scope == "impacted"
    assert seen["cwd"] == "/wt"
    # A one-argument hook is a valid hook too: it is called without the keyword.
    assert select_tests(_Proj(lambda changed: ["t.py"]), ["a.py"], cwd="/wt").scope == "impacted"


def test_select_tests_falls_back_to_full_on_every_unvouched_case() -> None:
    def raises(changed):
        raise RuntimeError("boom")

    cases = {
        "unknown diff": select_tests(_Proj(lambda c: ["t.py"]), None),
        "empty diff": select_tests(_Proj(lambda c: ["t.py"]), []),
        "no hook": select_tests(_Proj(), ["a.py"]),
        "hook raises": select_tests(_Proj(raises), ["a.py"]),
        "hook returns None": select_tests(_Proj(lambda c: None), ["a.py"]),
        "hook returns non-list": select_tests(_Proj(lambda c: "tests/test_a.py"), ["a.py"]),
        "empty impacted set": select_tests(_Proj(lambda c: []), ["a.py"]),
        "global path touched": select_tests(
            _Proj(lambda c: ["t.py"], global_paths=["conftest.py"]),
            ["a.py", "tests/conftest.py"],
        ),
    }
    for label, sel in cases.items():
        assert sel.scope == "full", label
        assert sel.files == (), label
        assert sel.reason, label


def test_select_tests_never_calls_the_hook_when_a_global_path_is_touched() -> None:
    called = []
    proj = _Proj(lambda c: called.append(c) or ["t.py"], global_paths=["pyproject.toml"])
    assert select_tests(proj, ["pyproject.toml"]).scope == "full"
    assert called == []


def test_global_paths_may_be_a_callable_and_a_broken_one_is_ignored() -> None:
    proj = _Proj(lambda c: ["t.py"])
    proj.test_global_paths = lambda: ["*.lock"]  # type: ignore[assignment]
    assert select_tests(proj, ["uv.lock"]).scope == "full"

    def broken():
        raise OSError

    proj.test_global_paths = broken  # type: ignore[assignment]
    assert select_tests(proj, ["uv.lock"]).scope == "impacted"


def test_matches_global_name_vs_path_patterns() -> None:
    assert matches_global("tests/deep/conftest.py", ["conftest.py"]) == "conftest.py"
    assert matches_global(".github/workflows/ci.yml", [".github/*"]) == ".github/*"
    assert matches_global("src/github.py", [".github/*"]) is None
    assert matches_global("src/a.py", []) is None


def test_changed_files_reads_committed_and_untracked_and_none_on_git_error(tmp_path) -> None:
    base = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")
    (tmp_path / "pkg" / "new.py").write_text("y = 1\n")
    assert sorted(changed_files(str(tmp_path), base)) == ["pkg/mod.py", "pkg/new.py"]
    assert changed_files(str(tmp_path), "not-a-sha") is None


# --- deterministic TEST runner ----------------------------------------------------------
def test_runner_narrows_only_the_unit_command_and_records_the_scope(tmp_path) -> None:
    base = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")
    proj = _Proj(lambda changed: ["tests/test_mod.py"], e2e=["sh", "-c", "exit 0", "e2e"])
    res = _run(tmp_path, proj, base=base)

    assert res.status is ResultStatus.SUCCESS
    assert ("unit", ["tests/test_mod.py"]) in proj.calls
    # e2e is still requested with no file list — it always runs whole.
    assert ("e2e", None) in proj.calls
    out = res.structured_output
    assert out["test_scope"] == "impacted"
    assert out["tests_selected"] == 1
    assert "impacted test file(s), NOT the full suite" in out["validation_notes"]


def test_runner_records_full_scope_with_a_reason_on_each_fallback(tmp_path) -> None:
    base = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")

    def raises(changed):
        raise ValueError

    for label, proj in {
        "no hook": _Proj(),
        "raises": _Proj(raises),
        "None": _Proj(lambda c: None),
        "empty": _Proj(lambda c: []),
        "global": _Proj(lambda c: ["tests/test_mod.py"], global_paths=["pkg/*"]),
        "adapter ignores files": _Proj(lambda c: ["tests/test_mod.py"], ignores_files=True),
    }.items():
        res = _run(tmp_path, proj, base=base)
        out = res.structured_output
        assert res.status is ResultStatus.SUCCESS, label
        assert out["test_scope"] == "full", label
        assert out["tests_selected"] == 0, label
        assert out["test_scope_reason"], label
        assert "unit: full suite" in out["validation_notes"], label


def test_runner_without_a_base_sha_runs_the_full_suite(tmp_path) -> None:
    proj = _Proj(lambda c: ["tests/test_mod.py"])
    out = _run(tmp_path, proj, base=None).structured_output
    assert out["test_scope"] == "full"
    assert ("unit", None) in proj.calls


def test_runner_impacted_red_still_fails_the_stage(tmp_path) -> None:
    base = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")

    class Red(_Proj):
        def test_unit_cmd(self, files=None):
            return ["sh", "-c", "exit 1"] if files else ["sh", "-c", "exit 0"]

    res = _run(tmp_path, Red(lambda c: ["tests/test_mod.py"]), base=base)
    assert res.status is ResultStatus.FAILURE
    assert res.structured_output["test_scope"] == "impacted"


def test_docs_only_still_skips_before_selection(tmp_path) -> None:
    base = _repo(tmp_path)
    (tmp_path / "README.md").write_text("docs\n")
    called = []
    res = _run(tmp_path, _Proj(lambda c: called.append(c) or ["t.py"]), base=base)
    assert res.structured_output["skipped"] == "docs-only"
    assert "test_scope" not in res.structured_output
    assert called == []


# --- fold guard -------------------------------------------------------------------------
def _result(output: dict, *, engine: bool) -> StageResult:
    mode = ExecutionMode.ENGINE if engine else ExecutionMode.INTERACTIVE
    provider = Provider.NONE if engine else Provider.CLAUDE
    return StageResult(
        work_item_id="wi", content_hash="h", run_id="r", task_id="t", stage=Stage.TEST,
        attempt=0, model="m", status=ResultStatus.SUCCESS, structured_output=output,
        lane_used=LaneUsed(execution_mode=mode, provider=provider, invocation="x"),
        token_usage=TokenUsage(), completed_at="2026-10-14T00:00:00Z",
    )


def test_test_scope_folds_only_from_the_engine_lane() -> None:
    assert "test_scope" in DETERMINISTIC_ONLY_KEYS
    assert "test_scope" in CONTEXT_KEYS[Stage.TEST]
    out = {"passed": True, "failures": [], "tests_meaningful": None, "test_scope": "full"}

    model_task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(model_task, _result(out, engine=False))
    assert "test_scope" not in model_task.context

    engine_task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(engine_task, _result(out, engine=True))
    assert engine_task.context["test_scope"] == "full"


# --- model TEST prompt ------------------------------------------------------------------
def _prompt(stage: Stage, impacted=None) -> str:
    return render_prompt(stage, task_id="#1", title="t", body="b", context={},
                         impacted_tests=impacted)


def test_test_prompt_names_the_impacted_tests_and_is_unchanged_without_them() -> None:
    plain = _prompt(Stage.TEST)
    assert _prompt(Stage.TEST, []) == plain
    hinted = _prompt(Stage.TEST, ["tests/test_a.py", "tests/test_b.py"])
    assert hinted.startswith(plain[: plain.index("## TEST")])
    assert "## Impacted tests\n" in hinted
    assert "- tests/test_a.py\n- tests/test_b.py" in hinted
    # Only TEST gets it.
    assert _prompt(Stage.REVIEW, ["tests/test_a.py"]) == _prompt(Stage.REVIEW)


def test_engine_prompt_hint_uses_the_same_selection(tmp_path, project) -> None:
    base = _repo(tmp_path)
    (tmp_path / "pkg" / "mod.py").write_text("x = 2\n")
    eng = Engine(StatusStore(tmp_path / "runs"), CostLedger(tmp_path / "c.jsonl"), project)
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    task.context.update({"base_sha": base, "worktree": str(tmp_path)})

    assert eng._impacted_tests_for_prompt(task) is None  # FakeProject has no hook
    project.impacted_tests = lambda changed: ["tests/test_mod.py"]
    assert eng._impacted_tests_for_prompt(task) == ["tests/test_mod.py"]
    task.context["worktree"] = str(tmp_path / "missing")
    assert eng._impacted_tests_for_prompt(task) is None


# --- self-host mapping ------------------------------------------------------------------
def _selfhost_tree(root: Path) -> None:
    (root / "orchestrator").mkdir()
    (root / "orchestrator" / "widget.py").write_text("")
    (root / "orchestrator" / "lonely.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_widget.py").write_text("")
    (root / "tests" / "test_widget_extra.py").write_text("")
    (root / "tests" / "test_user.py").write_text("from orchestrator import other, widget\n")
    (root / "tests" / "test_dotted.py").write_text("import orchestrator.widget\n")
    (root / "tests" / "test_near.py").write_text("import orchestrator.widget_two\n")


def test_selfhost_maps_source_to_named_tests_and_direct_importers(tmp_path) -> None:
    _selfhost_tree(tmp_path)
    got = impacted_tests_for(["orchestrator/widget.py", "README.md"], tmp_path)
    assert got is not None
    assert set(got) == {
        "tests/test_widget.py", "tests/test_widget_extra.py",
        "tests/test_user.py", "tests/test_dotted.py",
    }
    # A changed test file maps to itself.
    assert impacted_tests_for(["tests/test_user.py"], tmp_path) == ["tests/test_user.py"]


def test_selfhost_returns_none_for_anything_it_cannot_map(tmp_path) -> None:
    _selfhost_tree(tmp_path)
    for changed in (
        ["orchestrator/lonely.py"],                   # no test names it
        ["orchestrator/widget.py", "skills/x.json"],  # a non-Python file
        ["orchestrator/__init__.py"],                 # a package init
        ["scripts/tool.py"],                          # outside the known packages
        ["tests/test_gone.py"],                       # a deleted test file
    ):
        assert impacted_tests_for(changed, tmp_path) is None, changed


def test_selfhost_hook_declares_global_inputs_and_resolves_against_cwd(tmp_path) -> None:
    _selfhost_tree(tmp_path)
    cfg = SelfHostConfig(tasks_path=str(tmp_path / "tasks.json"))
    assert cfg.impacted_tests(["tests/test_user.py"], cwd=str(tmp_path)) == ["tests/test_user.py"]
    assert select_tests(cfg, ["tests/conftest.py"], cwd=str(tmp_path)).scope == "full"
    assert select_tests(cfg, ["pyproject.toml"], cwd=str(tmp_path)).scope == "full"
    assert select_tests(cfg, ["orchestrator/schemas/status.py"],
                        cwd=str(tmp_path)).scope == "full"


# --- status / dashboard -----------------------------------------------------------------
def _task_with_test_output(output: dict | None) -> Task:
    task = Task(task_id="t1", run_id="r1", created_at="x", updated_at="x")
    rec = task.stages[Stage.TEST]
    rec.status = StageStatus.COMPLETED
    rec.output = output
    return task


def test_dashboard_stage_entry_and_strip_show_an_impacted_test(tmp_path) -> None:
    task = _task_with_test_output({"test_scope": "impacted", "tests_selected": 3})
    entries = {e["stage"]: e for e in _stage_progress(task, {}, now_epoch=time.time())}
    assert entries["test"]["test_scope"] == "impacted"
    assert entries["test"]["tests_selected"] == 3
    assert "test_scope" not in entries["review"]

    cells, _ = _stage_strip({"state": "completed", "stages": list(entries.values())})
    assert "TEST done (impacted 3)" in cells

    full = _task_with_test_output({"test_scope": "full", "tests_selected": 0})
    entries = {e["stage"]: e for e in _stage_progress(full, {}, now_epoch=time.time())}
    cells, _ = _stage_strip({"state": "completed", "stages": list(entries.values())})
    assert "TEST done" in cells


def test_status_reports_the_test_scope_once_recorded(tmp_path, project) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    assert "test_scope" not in eng.status("r1")["tasks"]["t1"]

    task = eng.store.load_task("r1", "t1")
    task.stages[Stage.TEST].output = {"test_scope": "impacted", "tests_selected": 2}
    eng.store.save_task(task)
    st = eng.status("r1")["tasks"]["t1"]
    assert st["test_scope"] == "impacted"
    assert st["tests_selected"] == 2
