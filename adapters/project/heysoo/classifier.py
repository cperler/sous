"""Hey Soo! failure classifier + test taxonomy (target.md §5, build-fresh D5).

Implements the engine's ``FailureClassifier`` Protocol. The taxonomy conventions
(``.spec.ts`` -> e2e, ``test_*.py``/``conftest.py`` -> unit, ``*.bats`` -> shell)
and the regexes (ported from the bash ``classify-failures.sh`` / ``regression-helpers.sh``)
live HERE in project-config, not in the engine.
"""

from __future__ import annotations

import re

from orchestrator.failure_classifier import Failure
from orchestrator.schemas.enums import FailureKind

# Ported regexes (Jest/pytest + Playwright list reporter).
_PYTEST_FAILED = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_PYTEST_ERROR = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)
# Playwright: "  ✘  3 [chromium] › tests/e2e/foo.spec.ts:12:3 › title"
_PLAYWRIGHT_FAIL = re.compile(r"[✘✗]\s+\d+\s+.*?›\s+(tests/e2e/\S+\.spec\.ts:\d+:\d+)")


def _taxonomy(test_id: str) -> FailureKind:
    if ".spec.ts" in test_id or test_id.startswith("tests/e2e/"):
        return FailureKind.E2E
    if test_id.endswith(".bats") or ".bats" in test_id:
        return FailureKind.SHELL
    if "::" in test_id or test_id.endswith(".py") or "test_" in test_id:
        return FailureKind.UNIT
    return FailureKind.UNKNOWN


class HeysooClassifier:
    def classify(self, test_output: str) -> list[Failure]:
        out: list[Failure] = []
        seen: set[str] = set()

        def add(test_id: str, message: str = "") -> None:
            if test_id and test_id not in seen:
                seen.add(test_id)
                out.append(Failure(test=test_id, kind=_taxonomy(test_id), message=message))

        # infra signal short-circuits to a single INFRA failure (ported heuristic).
        if re.search(r"(ECONNREFUSED|address already in use|browserType.launch|Timed out waiting for the dev server)", test_output):
            out.append(Failure(test="<infra>", kind=FailureKind.INFRA, message="infrastructure failure"))
            return out

        for m in _PYTEST_FAILED.finditer(test_output):
            add(m.group(1), "pytest FAILED")
        for m in _PYTEST_ERROR.finditer(test_output):
            add(m.group(1), "pytest ERROR")
        for m in _PLAYWRIGHT_FAIL.finditer(test_output):
            add(m.group(1), "playwright failed")
        return out

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        """Map changed source files to the tests that should run (change->test-set)."""
        impacted: list[str] = []
        for f in changed_files:
            if f.endswith(".spec.ts"):
                impacted.append(f)
            elif f.startswith("frontend/") and f.endswith((".ts", ".tsx")):
                # frontend source change -> its colocated/e2e specs (heuristic)
                impacted.append(f.rsplit(".", 1)[0] + ".spec.ts")
            elif f.endswith(".py") and not f.rsplit("/", 1)[-1].startswith("test_"):
                # lambda/backend source -> sibling test_<name>.py
                head, name = f.rsplit("/", 1) if "/" in f else ("", f)
                impacted.append(f"{head}/test_{name}" if head else f"test_{name}")
            elif "test_" in f or f.endswith(".bats"):
                impacted.append(f)
        # de-dup, preserve order
        return list(dict.fromkeys(impacted))
