"""#41: deterministic docs-only change classification + the TEST short-circuit.

Detection diffs the task's fork point (base_sha) against the worktree with a real git read
— a model can never set the tag (the fold guard is tested in test_context_plane). A docs-only
change (every changed file is documentation) has no behavioral surface, so the deterministic
TEST runner short-circuits to an HONEST skip (marked skipped: docs-only), never a faked green.
Comment-only-in-code detection is deliberately OUT OF SCOPE (doc-FILES only).
"""

from __future__ import annotations

import subprocess

from jsonschema import Draft202012Validator

from adapters.execution.deterministic_test import (
    DeterministicTestRunner,
    _is_doc_file,
    classify_change,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.stage_schemas import load_stage_schema
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE)


def _git(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()


def _wi(cwd, base_sha) -> WorkItem:
    ctx = {"baseline_failures": []}
    if base_sha is not None:
        ctx["base_sha"] = base_sha
    return WorkItem.create(
        id="wi", run_id="r", task_id="#42", stage=Stage.TEST, prompt="p",
        schema_ref="test", model="engine", lane_policy=_ENGINE, created_at="now",
        cwd=str(cwd), context=ctx,
    )


class _FailingProj:
    """Its unit command FAILS if run — so a green result proves the suite was skipped."""

    classifier = None

    def test_unit_cmd(self, files=None):
        return ["sh", "-c", "exit 1"]


# --- _is_doc_file classification --------------------------------------------------------
def test_is_doc_file_recognizes_docs_and_rejects_source() -> None:
    assert _is_doc_file("README.md")
    assert _is_doc_file("docs/guide.rst")
    assert _is_doc_file("sub/dir/notes.txt")
    assert _is_doc_file("LICENSE")  # extensionless doc file
    assert _is_doc_file("path/to/docs/anything.py")  # under a docs/ dir
    assert not _is_doc_file("src/app.py")
    assert not _is_doc_file("license.py")  # shares the stem but IS source (has a .py suffix)
    assert not _is_doc_file("config.yaml")


# --- classify_change (real git) ---------------------------------------------------------
def test_all_docs_diff_is_docs_only(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.rst").write_text("guide\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "docs")
    assert classify_change(str(tmp_path), base) == "docs-only"


def test_mixed_diff_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    (tmp_path / "src.py").write_text("x = 2\n")  # a real code change kills the docs tag
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "mixed")
    assert classify_change(str(tmp_path), base) == "code"


def test_untracked_docs_only_are_counted(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "NOTES.md").write_text("uncommitted note\n")  # untracked, never committed
    assert classify_change(str(tmp_path), base) == "docs-only"


def test_untracked_code_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "new_module.py").write_text("y = 3\n")  # untracked source
    assert classify_change(str(tmp_path), base) == "code"


def test_empty_or_missing_base_is_undetermined(tmp_path) -> None:
    base = _init_repo(tmp_path)
    assert classify_change(str(tmp_path), base) is None  # nothing changed → undetermined
    assert classify_change(str(tmp_path), None) is None  # no base → can't detect
    assert classify_change(None, base) is None  # no cwd → can't detect


# --- the TEST runner short-circuit ------------------------------------------------------
def test_runner_short_circuits_docs_only_without_running_suite(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("docs change only\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "docs")

    res = DeterministicTestRunner(_FailingProj()).dispatch(_wi(tmp_path, base))

    assert res.status is ResultStatus.SUCCESS  # green DESPITE the exit-1 unit cmd => not run
    out = res.structured_output
    assert out["change_class"] == "docs-only"
    assert out["skipped"] == "docs-only"  # honest skip marker, not a faked green
    assert out["passed"] is True and out["failures"] == []
    assert out["tests_meaningful"] is None  # #261: the skip path claims no judgment either
    assert "no behavioral surface" in out["validation_notes"]
    Draft202012Validator(load_stage_schema("test")).validate(out)  # schema-clean


def test_runner_runs_the_suite_for_a_code_change(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 99\n")  # a code change
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "code")

    res = DeterministicTestRunner(_FailingProj()).dispatch(_wi(tmp_path, base))

    # the failing unit command IS run (no short-circuit) → the stage fails as usual
    assert res.status is ResultStatus.FAILURE
    assert res.structured_output.get("change_class") == "code"
    assert "skipped" not in res.structured_output


def test_runner_without_base_sha_runs_normally(tmp_path) -> None:
    # No base_sha in context (e.g. a no-git fake intake) → detection can't fire → full run.
    res = DeterministicTestRunner(_FailingProj()).dispatch(_wi(tmp_path, None))
    assert res.status is ResultStatus.FAILURE  # the suite ran (and failed)
    assert "change_class" not in res.structured_output
