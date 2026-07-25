"""Importable fake project whose unit-test command exits non-zero — a red trunk for
the ``trunk-gate`` CLI smoke test (`--project tests.redtrunkproject`)."""

from __future__ import annotations

from tests.conftest import FakeProject


class RedTrunkProject(FakeProject):
    def test_unit_cmd(self, files=None):
        # Deterministically red: emit a detail line to stderr, then exit 1.
        return ["sh", "-c", "echo trunk-integration-break >&2; exit 1"]


def get_config() -> RedTrunkProject:
    return RedTrunkProject()
