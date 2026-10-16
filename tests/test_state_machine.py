"""Pure state-machine helpers (no store, no I/O)."""

from __future__ import annotations

import pytest

from orchestrator.state_machine import deliver_push_mismatch, pr_not_opened, remote_branch_sha

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
        assert reason and reason.startswith("deliver gate:")


# --- #500: a DELIVER checkpoint the remote does not hold was never delivered ----------

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (f"{SHA_A}\trefs/heads/task/254\n", SHA_A),
        # ls-remote patterns match by suffix; only the exact ref counts.
        (f"{SHA_B}\trefs/heads/old/task/254\n{SHA_A}\trefs/heads/task/254\n", SHA_A),
        (f"{SHA_B}\trefs/heads/old/task/254\n", None),
        ("", None),  # the branch is not on the remote at all
        ("fatal: not a sha\trefs/heads/task/254\n", None),
        ("garbage\n", None),
    ],
)
def test_remote_branch_sha_reads_only_the_exact_ref(stdout, expected) -> None:
    assert remote_branch_sha(stdout, "task/254") == expected


def test_deliver_push_mismatch_passes_only_an_exact_match() -> None:
    assert deliver_push_mismatch(SHA_A, SHA_A, "task/254") is None


def test_deliver_push_mismatch_names_both_shas_and_the_fix() -> None:
    reason = deliver_push_mismatch(SHA_A, SHA_B, "task/254")
    assert reason and reason.startswith("deliver gate:")
    assert SHA_A in reason and SHA_B in reason
    assert "git push origin task/254" in reason
    assert "ls-remote" in reason  # not a possibly-stale origin/<branch> tracking ref


def test_deliver_push_mismatch_on_a_branch_missing_from_the_remote() -> None:
    reason = deliver_push_mismatch(SHA_A, None, "task/254")
    assert reason and "has no branch task/254" in reason
