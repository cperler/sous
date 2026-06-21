"""Failure-classifier interface (target.md §3 / §5, build-fresh D5).

The engine owns the *mechanism* (collect failures, diff regressions vs a baseline).
The *taxonomy* — how a line of test output maps to a unit/e2e/shell failure, and
how a changed source file maps to its impacted tests — is project-config (the
concrete regexes live in the Hey Soo! adapter, not here).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .schemas.enums import FailureKind


class Failure(BaseModel):
    """One classified test failure."""

    test: str  # stable identifier, e.g. "tests/unit/test_x.py::test_y"
    kind: FailureKind = FailureKind.UNKNOWN
    message: str = ""


@runtime_checkable
class FailureClassifier(Protocol):
    """Provided by project-config. Pure: text in, structured failures out."""

    def classify(self, test_output: str) -> list[Failure]:
        """Parse raw test output into structured failures with taxonomy buckets."""
        ...

    def impacted_tests(self, changed_files: list[str]) -> list[str]:
        """Map changed source files to the tests that should run (change->test-set)."""
        ...


def compute_regressions(
    current: list[Failure], baseline: list[Failure]
) -> list[Failure]:
    """Engine mechanism: failures present now but not in the baseline.

    Identity is the ``test`` field. Inherited (baseline) failures are excluded so
    a stage is judged only on regressions it introduced (ports the as-built
    regression-aware pass; the taxonomy that produced ``Failure.kind`` is adapter-owned).
    """

    baseline_tests = {f.test for f in baseline}
    return [f for f in current if f.test not in baseline_tests]
