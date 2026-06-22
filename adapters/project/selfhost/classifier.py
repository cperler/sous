"""Self-host failure classifier + taxonomy (a pure Python/pytest/ruff project).

Deliberately unlike the Hey Soo! classifier: no `.spec.ts`/e2e/bats vocabulary —
just pytest failures and ruff lint. This is the whole point of Phase 5: the taxonomy
is project-config, so a structurally different project plugs in a different one with
zero engine changes.
"""

from __future__ import annotations

import re

from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import FailureKind

_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_PYTEST_ERROR = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
# ruff: "path/to/file.py:12:5: E501 ..."
_RUFF = re.compile(r"^(\S+\.py:\d+:\d+):\s+([A-Z]+\d+)", re.MULTILINE)


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
