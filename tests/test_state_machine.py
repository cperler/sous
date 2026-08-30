"""Pure state-machine helpers (no store, no I/O)."""

from __future__ import annotations

import pytest

from orchestrator.state_machine import pr_not_opened

# --- #351: a DELIVER that opened no PR is not a delivery -----------------------------


@pytest.mark.parametrize(
    ("output", "expect_veto"),
    [
        # The four shapes batch-codex-3 actually produced — every one recorded SUCCESS.
        ({"pr_number": 0, "pr_url": "https://github.com/o/r/pull/new/task/259"}, True),
        ({"pr_number": 0, "pr_url": "blocked: GitHub API unreachable; branch pushed"}, True),
        ({"pr_number": 0, "pr_url": ""}, True),
        ({}, True),
        # Other ways to not-deliver.
        ({"pr_number": -1, "pr_url": "https://github.com/o/r/pull/-1"}, True),
        ({"pr_number": True, "pr_url": "https://github.com/o/r/pull/1"}, True),
        ({"pr_number": "7", "pr_url": "https://github.com/o/r/pull/7"}, True),
        ({"pr_number": 7, "pr_url": None}, True),
        # A stale pair from a prior fix cycle: both look real, but disagree.
        ({"pr_number": 7, "pr_url": "https://github.com/o/r/pull/9"}, True),
        # Real deliveries.
        ({"pr_number": 7, "pr_url": "https://github.com/o/r/pull/7"}, False),
        ({"pr_number": 7, "pr_url": "https://github.com/o/r/pull/7/"}, False),
        ({"pr_number": 142, "pr_url": "  https://github.com/o/r/pull/142  "}, False),
    ],
)
def test_pr_not_opened_accepts_only_a_coherent_real_pr(output, expect_veto) -> None:
    reason = pr_not_opened(output)
    assert (reason is not None) is expect_veto, (output, reason)
    if expect_veto:
        assert reason and reason.startswith("publish gate:")
