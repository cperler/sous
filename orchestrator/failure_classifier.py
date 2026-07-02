"""Failure-classifier interface (target.md §3 / §5, build-fresh D5).

The *taxonomy* — how a line of test output maps to a unit/e2e/shell failure, and how a
changed source file maps to its impacted tests — is project-config (the concrete regexes
live in the Hey Soo! adapter, not here). This Protocol is the pluggable seam.

The engine does NOT yet invoke this — the infra-failure classification + reset loop it
exists for is unwired (see the "Infra-failure classification + reset loop" DEFERRED row,
which names when/how it will be wired). The engine-side regression-diff mechanism that
was here (``compute_regressions``) had no caller and was removed with that row; it
returns when the loop is actually built.
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
