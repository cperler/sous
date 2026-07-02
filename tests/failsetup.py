"""Importable project whose deterministic setup ALWAYS fails — for the CLI-drain
termination test (a failing intake must not infinite-loop `orchestrator next`)."""

from __future__ import annotations

from tests.conftest import FakeProject


class _FailSetupProject(FakeProject):
    def setup_task(self, task_id: str) -> dict:  # raises -> dispatch must yield FAILURE
        raise RuntimeError("boom: deterministic setup failed")


def get_config() -> _FailSetupProject:
    return _FailSetupProject()
