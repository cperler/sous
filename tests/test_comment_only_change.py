"""#69: comment-only-in-code detection extends the #41 docs-only tag.

The detection is PROJECT-owned and language-aware: the selfhost adapter compares each
changed Python file's before/after ASTs (stdlib ``ast``). If the ASTs are identical the
change PROVABLY touched only comments/formatting — no heuristic, no loophole. The engine
lane (``classify_change``) consults this via an optional ``is_comment_only_change`` hook and
stays language-agnostic; any file it cannot prove a no-op (non-Python, unparseable, added/
deleted/renamed, binary, or hook-crash) falls back to ``code``.

The tag GATES DOWNSTREAM EFFORT (lighter TEST, relaxed REVIEW, no missing-tests rejection),
so a false docs-only ships a real change under-tested. These tests are deliberately
ADVERSARIAL: every case where doubt exists must resolve to ``code``.
"""

from __future__ import annotations

import subprocess

from adapters.execution.deterministic_test import (
    DeterministicTestRunner,
    classify_change,
)
from adapters.project.selfhost.classifier import (
    SelfHostClassifier,
    is_python_comment_only_change,
)
from adapters.project.selfhost.config import SelfHostConfig
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_ENGINE = LanePolicy(execution_mode=ExecutionMode.ENGINE, provider=Provider.NONE)


# --- pure AST comparison (is_python_comment_only_change) --------------------------------
def test_comment_only_edit_is_comment_only() -> None:
    before = "x = 1  # old comment\ndef f():\n    return x\n"
    after = "x = 1  # a totally rewritten comment\ndef f():\n    # added comment\n    return x\n"
    assert is_python_comment_only_change("src.py", before, after) is True


def test_whitespace_and_formatting_only_is_comment_only() -> None:
    # DELIBERATE: a pure reformat (blank lines, spacing, trailing whitespace) has an
    # IDENTICAL AST — that identity is a proof, not a guess, that runtime behavior is
    # unchanged, so we classify it comment-only (docs-only) and skip the suite safely.
    before = "def f(a,b):\n    return a+b\n"
    after = "def f(a, b):\n\n    return a + b\n"
    assert is_python_comment_only_change("src.py", before, after) is True


def test_changed_string_literal_is_code() -> None:
    # Looks comment-ish (only a quoted string moved) but the literal VALUE is behavioral.
    before = 'msg = "hello"\n'
    after = 'msg = "goodbye"\n'
    assert is_python_comment_only_change("src.py", before, after) is False


def test_changed_numeric_literal_is_code() -> None:
    assert is_python_comment_only_change("src.py", "x = 1\n", "x = 2\n") is False


def test_docstring_only_edit_is_code() -> None:
    # DELIBERATE + JUSTIFIED: docstrings ARE AST nodes and are load-bearing (doctest runs
    # them, __doc__/help expose them, some frameworks introspect them). We do NOT normalize
    # them away — a docstring change has a real behavioral surface, so it stays `code`.
    before = 'def f():\n    """Old docstring."""\n    return 1\n'
    after = 'def f():\n    """New docstring with >>> f() doctest."""\n    return 1\n'
    assert is_python_comment_only_change("src.py", before, after) is False


def test_line_moved_out_of_comment_is_code() -> None:
    # A statement that was a `#` comment becomes real code — the classic loophole; must be code.
    before = "x = 1\n# y = 2\n"
    after = "x = 1\ny = 2\n"
    assert is_python_comment_only_change("src.py", before, after) is False


def test_line_moved_into_comment_is_code() -> None:
    # ...and the reverse: a real statement commented out changes behavior → code.
    before = "x = 1\ny = 2\n"
    after = "x = 1\n# y = 2\n"
    assert is_python_comment_only_change("src.py", before, after) is False


def test_syntax_error_in_after_is_code() -> None:
    assert is_python_comment_only_change("src.py", "x = 1\n", "x = (1\n") is False


def test_syntax_error_in_before_is_code() -> None:
    assert is_python_comment_only_change("src.py", "def (:\n", "x = 1\n") is False


def test_non_python_path_is_code() -> None:
    # Identical content, but a non-.py path: this adapter reasons about Python only → code.
    assert is_python_comment_only_change("config.yaml", "a: 1\n", "a: 1  # note\n") is False


def test_real_code_addition_is_code() -> None:
    before = "def f():\n    return 1\n"
    after = "def f():\n    log()\n    return 1\n"
    assert is_python_comment_only_change("src.py", before, after) is False


def test_classifier_method_delegates() -> None:
    c = SelfHostClassifier()
    assert c.is_comment_only_change("src.py", "x = 1  # a\n", "x = 1  # b\n") is True
    assert c.is_comment_only_change("src.py", "x = 1\n", "x = 2\n") is False


def test_config_exposes_hook() -> None:
    cfg = SelfHostConfig(tasks_path="/dev/null")  # tasks_path avoids any GitHub source
    assert cfg.is_comment_only_change("src.py", "x = 1  # a\n", "x = 1  # b\n") is True
    assert cfg.is_comment_only_change("src.py", "x = 1\n", "x = 2\n") is False


# --- classify_change integration (real git + the selfhost hook) -------------------------
def _git(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(tmp_path) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src.py").write_text("x = 1  # base comment\ndef f():\n    return x\n")
    (tmp_path / "other.py").write_text("y = 10\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()


def _commit(tmp_path, msg: str) -> None:
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", msg)


def test_comment_only_py_change_is_docs_only(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # a REWRITTEN comment\ndef f():\n    return x\n")
    _commit(tmp_path, "comment tweak")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "docs-only"


def test_real_py_change_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 2  # a REWRITTEN comment\ndef f():\n    return x\n")
    _commit(tmp_path, "value change")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_mixed_comment_only_and_real_code_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # new comment\ndef f():\n    return x\n")  # noop
    (tmp_path / "other.py").write_text("y = 11\n")  # a real code change → poisons the tag
    _commit(tmp_path, "mixed")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_comment_only_py_plus_doc_file_is_docs_only(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # new comment\ndef f():\n    return x\n")  # noop
    (tmp_path / "README.md").write_text("docs\n")  # doc file (the #41 path)
    _commit(tmp_path, "comment + docs")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "docs-only"


def test_non_python_source_change_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "config.yaml").write_text("a: 1\n")  # non-Python source, not a doc file
    _commit(tmp_path, "yaml")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_unparseable_py_edit_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # comment\ndef f(:\n    return x\n")  # syntax error
    _commit(tmp_path, "broken")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_added_py_file_is_code(tmp_path) -> None:
    # An ADDED file has no base revision to compare against → cannot prove a no-op → code,
    # even if its whole content is a comment.
    base = _init_repo(tmp_path)
    (tmp_path / "new.py").write_text("# just a comment\n")
    _commit(tmp_path, "add file")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_deleted_py_file_is_code(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "other.py").unlink()  # delete a source file → not provably comment-only
    _commit(tmp_path, "delete file")
    assert classify_change(str(tmp_path), base, SelfHostClassifier()) == "code"


def test_no_hook_falls_back_to_doc_files_only(tmp_path) -> None:
    # Backward compatibility: without a project (or a project lacking the hook), a
    # comment-only .py edit is NOT recognized and stays `code` — the #41 behavior.
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # new comment\ndef f():\n    return x\n")  # noop
    _commit(tmp_path, "comment tweak")
    assert classify_change(str(tmp_path), base) == "code"
    assert classify_change(str(tmp_path), base, object()) == "code"


def test_hook_exception_is_code(tmp_path) -> None:
    # A project hook that raises must never crash classification — it degrades to `code`.
    class _Boom:
        def is_comment_only_change(self, path, before, after):
            raise RuntimeError("boom")

    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # new comment\ndef f():\n    return x\n")
    _commit(tmp_path, "comment tweak")
    assert classify_change(str(tmp_path), base, _Boom()) == "code"


# --- end-to-end: the runner short-circuits a comment-only change ------------------------
class _CommentAwareFailingProj:
    """Unit command FAILS if run; exposes the #69 hook — a green result proves the suite was
    skipped BECAUSE the comment-only edit was recognized (not because tests were faked)."""

    classifier = None

    def __init__(self) -> None:
        self._c = SelfHostClassifier()

    def test_unit_cmd(self, files=None):
        return ["sh", "-c", "exit 1"]

    def is_comment_only_change(self, path: str, before: str, after: str) -> bool:
        return self._c.is_comment_only_change(path, before, after)


def _wi(cwd, base_sha) -> WorkItem:
    return WorkItem.create(
        id="wi", run_id="r", task_id="#69", stage=Stage.TEST, prompt="p",
        schema_ref="test", model="engine", lane_policy=_ENGINE, created_at="now",
        cwd=str(cwd), context={"baseline_failures": [], "base_sha": base_sha},
    )


def test_runner_short_circuits_comment_only_change(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 1  # rewritten\ndef f():\n    return x\n")  # noop
    _commit(tmp_path, "comment tweak")

    res = DeterministicTestRunner(_CommentAwareFailingProj()).dispatch(_wi(tmp_path, base))

    assert res.status is ResultStatus.SUCCESS  # green DESPITE the exit-1 unit cmd → not run
    out = res.structured_output
    assert out["change_class"] == "docs-only"
    assert out["skipped"] == "docs-only"
    assert "comment-only" in out["validation_notes"]


def test_runner_runs_suite_for_real_change_with_hook(tmp_path) -> None:
    base = _init_repo(tmp_path)
    (tmp_path / "src.py").write_text("x = 2\ndef f():\n    return x\n")  # real change
    _commit(tmp_path, "value change")

    res = DeterministicTestRunner(_CommentAwareFailingProj()).dispatch(_wi(tmp_path, base))

    assert res.status is ResultStatus.FAILURE  # suite ran (no short-circuit) and failed
    assert res.structured_output.get("change_class") == "code"
