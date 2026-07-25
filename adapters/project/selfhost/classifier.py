"""Self-host failure classifier + taxonomy (a pure Python/pytest/ruff project).

Deliberately unlike the Hey Soo! classifier: no `.spec.ts`/e2e/bats vocabulary —
just pytest failures and ruff lint. This is the whole point of Phase 5: the taxonomy
is project-config, so a structurally different project plugs in a different one with
zero engine changes.
"""

from __future__ import annotations

import ast
import re

from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import FailureKind

_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_PYTEST_ERROR = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
# ruff: "path/to/file.py:12:5: E501 ..."
_RUFF = re.compile(r"^(\S+\.py:\d+:\d+):\s+([A-Z]+\d+)", re.MULTILINE)


def _python_ast_fingerprint(source: str) -> str:
    """A structural AST dump of ``source`` in which comments and formatting are absent by
    construction. ``ast.parse`` never emits comment nodes (comments are not part of the
    grammar) and ``ast.dump`` (default args) omits line/column attributes, so two revisions
    that differ ONLY in comments, blank lines, or other pure whitespace/formatting produce
    an identical fingerprint. Literal VALUES are retained — a changed string or numeric
    literal changes the dump — and docstrings ARE nodes (an ``Expr``/``Constant`` head
    statement), so a docstring edit changes the fingerprint too."""
    return ast.dump(ast.parse(source))


def is_python_comment_only_change(path: str, before: str, after: str) -> bool:
    """True only when ``path`` is a Python source file whose ``before``/``after`` revisions
    are PROVABLY equivalent modulo comments and formatting — i.e. they parse to identical
    ASTs. This is the language-aware (#69) extension of the #41 docs-only tag, and the tag
    it feeds gates downstream test/review effort, so it is conservative by construction (a
    false positive would ship a real code change under-tested):

    - a non-``.py`` path returns False — this adapter reasons about Python only, and any
      other language must fall back to ``code``;
    - a syntax error in EITHER revision returns False — never assume; fall back to ``code``;
    - a docstring-only edit returns False — docstrings are load-bearing (``doctest``
      execution, runtime ``__doc__`` / help text, framework introspection), so they are NOT
      normalized away and a docstring change counts as a code change;
    - a whitespace / comment / formatting-only edit returns True — the identical AST is a
      proof (not a heuristic) that runtime behavior is unchanged, so the suite may be skipped.
    """
    if not path.endswith(".py"):
        return False
    try:
        return _python_ast_fingerprint(before) == _python_ast_fingerprint(after)
    except (SyntaxError, ValueError):  # unparseable / null bytes → cannot prove no-op → code
        return False


class SelfHostClassifier:
    def classify(self, test_output: str) -> list[Failure]:
        out: list[Failure] = []
        seen: set[str] = set()

        def add(test_id: str, kind: FailureKind, msg: str) -> None:
            if test_id and test_id not in seen:
                seen.add(test_id)
                out.append(Failure(test=test_id, kind=kind, message=msg))

        for m in _PYTEST_FAILED.finditer(test_output):
            add(m.group(1), FailureKind.UNIT, "pytest FAILED")
        for m in _PYTEST_ERROR.finditer(test_output):
            add(m.group(1), FailureKind.UNIT, "pytest ERROR")
        for m in _RUFF.finditer(test_output):
            add(m.group(1), FailureKind.SHELL, f"ruff {m.group(2)}")  # lint -> "shell"-ish gate
        return out

    def is_comment_only_change(self, path: str, before: str, after: str) -> bool:
        """#69 hook: is this single file's before→after edit provably comment-only? Delegates
        to the module-level Python AST comparison (see ``is_python_comment_only_change``)."""
        return is_python_comment_only_change(path, before, after)

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        impacted: list[str] = []
        for f in changed_files:
            name = f.rsplit("/", 1)[-1]
            if name.startswith("test_") and f.endswith(".py"):
                impacted.append(f)
            elif f.endswith(".py"):
                # source module foo.py -> tests/test_foo.py (heuristic)
                impacted.append(f"tests/test_{name}")
        return list(dict.fromkeys(impacted))
