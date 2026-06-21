"""Importable fake project module for the CLI smoke test (`--project tests.fakeproject`)."""

from __future__ import annotations

from tests.conftest import FakeProject


def get_config() -> FakeProject:
    return FakeProject()
