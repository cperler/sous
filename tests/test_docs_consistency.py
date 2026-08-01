"""Lightweight consistency check for the current-state docs (#276).

README.md and ARCHITECTURE.md are the advertised contributor map, and USING.md /
CHEATSHEET.md the operator's guide, so the claims that drift fastest are pinned here
rather than re-audited by hand:

* the developer command list stays in sync with the CI gate (`uv run pytest` / `ruff` /
  `mypy`) — the omission that motivated this test was a missing `uv run mypy`;
* no hardcoded pytest total is re-embedded (three docs had disagreed with each other and
  with the suite);
* the queue file is not described as "lock-free" — ``QueueFile`` deliberately takes a
  cross-process advisory lock for every mutation (`orchestrator/queue_file.py`);
* the shipped headless transports (``claude -p`` / ``codex exec``) are not advertised as
  something else (the Agent SDK was never a shipped path — it is a historical design
  note, and `docs/` may still discuss it as such).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CURRENT_STATE_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "USING.md",
    "CHEATSHEET.md",
)

# "725 pytest cases", "838 cases", "pytest suite (725)" — any concrete total, which goes
# stale the moment a test is added. Counts must be generated or absent, never typed in.
_TEST_TOTAL_PATTERNS = (
    re.compile(r"\b\d[\d,]*\s+(?:pytest\s+)?(?:cases|tests)\b", re.IGNORECASE),
    re.compile(r"\b(?:pytest\s+)?suite\s*\(\s*\d", re.IGNORECASE),
)


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _ci_gate_commands() -> list[str]:
    """The ``uv run <tool>`` commands CI actually enforces, in workflow order."""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    seen: list[str] = []
    for tool in re.findall(r"^\s*run:\s*uv run ([a-z][a-z0-9_-]*)", text, re.MULTILINE):
        if tool != "sync" and tool not in seen:
            seen.append(tool)
    return seen


def test_ci_gate_commands_are_discoverable() -> None:
    """Guards the parser itself: if CI's shape changes, the parity test below must not
    silently degrade into asserting nothing."""
    assert _ci_gate_commands() == ["pytest", "ruff", "mypy"]


@pytest.mark.parametrize("tool", _ci_gate_commands())
def test_readme_lists_every_ci_gate_command(tool: str) -> None:
    assert f"uv run {tool}" in _read("README.md"), (
        f"README's development instructions omit `uv run {tool}`, which CI enforces "
        f"({CI_WORKFLOW.relative_to(REPO)})"
    )


@pytest.mark.parametrize("doc", CURRENT_STATE_DOCS)
def test_no_hardcoded_test_totals(doc: str) -> None:
    for line in _read(doc).splitlines():
        for pattern in _TEST_TOTAL_PATTERNS:
            assert not pattern.search(line), (
                f"{doc} embeds a test total that will go stale: {line.strip()!r}. "
                "Describe the suite without a count (or generate the number)."
            )


@pytest.mark.parametrize("doc", CURRENT_STATE_DOCS)
def test_queue_append_not_advertised_as_lock_free(doc: str) -> None:
    # QueueFile._with_lock takes fcntl.flock(LOCK_EX) (or an os.mkdir-spin fallback) for
    # every read-modify-write, including append. Paragraph-scoped: "lock-free" is a fair
    # description of other subsystems (e.g. record()'s pre-lock check), just not the queue.
    for paragraph in re.split(r"\n\s*\n", _read(doc).lower()):
        if "lock-free" not in paragraph:
            continue
        assert not re.search(r"\bqueue|\benqueue\b", paragraph), (
            f"{doc} calls the queue lock-free, but QueueFile locks every mutation "
            f"(orchestrator/queue_file.py): {paragraph.strip()[:200]!r}"
        )


@pytest.mark.parametrize("doc", CURRENT_STATE_DOCS)
def test_shipped_headless_transports_only(doc: str) -> None:
    assert "agent sdk" not in _read(doc).lower(), (
        f"{doc} advertises the Agent SDK as a headless path; the shipped transports are "
        "`claude -p` and `codex exec` (adapters/execution/transport.py). Historical "
        "discussion belongs in docs/."
    )


def test_queue_file_really_locks() -> None:
    """The other half of the lock-free claim: pin the behaviour the doc now describes, so
    the docs test can't be satisfied by changing the code instead."""
    source = (REPO / "orchestrator" / "queue_file.py").read_text(encoding="utf-8")
    assert "_with_lock" in source
    assert "LOCK_EX" in source
    for mutator in ("def append", "def claim_head", "def complete_head", "def unclaim_head"):
        body = source.split(mutator, 1)[1].split("\n    def ", 1)[0]
        assert "_with_lock" in body, f"{mutator} mutates the queue without taking the lock"
