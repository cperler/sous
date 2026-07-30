"""#322 — the POST-HOC half of the no-attribution norm.

#317 enforced "a run-produced commit carries no model attribution trailer" with a prompt
directive and nothing else. An instruction is not a guarantee, and this repo has the receipts:
``batch-headless-1`` and ``batch-headless-2`` each merged a commit signed ``Claude Opus 4.5``
— a model NEITHER run dispatched — and both times the only detector was a human reading
``git log``.

These tests cover the check that closes that: a deterministic (ENGINE-lane, no model) scan of
the commits each checkpoint stage actually produced, whose findings land in ``events.jsonl``
as warning-grade events. Two halves:

* the pure scanner (``orchestrator.commit_attribution``) — what counts as a signature, and
  just as importantly what does NOT (prose naming the trailer, a DCO ``Signed-off-by``);
* the engine wiring — real git commits in a real repo, read back out of the run's durable
  event log the way a post-hoc auditor would.

Report-only by design: nothing here amends a commit. DELIVER pushes before its checkpoint
lands, so any engine-side amend would rewrite already-remote history.
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.commit_attribution import attribution_findings, scan_commits
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

TRAILER = "Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>"


# --- the pure scanner ---------------------------------------------------------------------


def test_clean_message_has_no_findings() -> None:
    assert attribution_findings("Fix the thing (#322)\n\nBecause it was broken.\n") == ()


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>",
        "co-authored-by: someone <a@b.c>",
        "Co-authored-by: Codex <noreply@openai.com>",  # provider-neutral: not just claude
        "Coauthored-By: bot <b@b.b>",
        "Assisted-by: some agent <x@y.z>",
        "Generated-by: some agent <x@y.z>",
    ],
)
def test_an_attribution_trailer_is_flagged(line: str) -> None:
    findings = attribution_findings(f"Subject\n\nBody.\n\n{line}\n")
    assert [f["reason"] for f in findings] == ["attribution_trailer"]
    assert findings[0]["line"] == line  # the evidence, verbatim


def test_a_generated_with_footer_is_flagged() -> None:
    findings = attribution_findings(
        "Subject\n\n\N{ROBOT FACE} Generated with [Some Agent](https://example.test)\n"
    )
    assert [f["reason"] for f in findings] == ["generated_with_marker"]


def test_prose_naming_the_trailer_is_not_a_signature() -> None:
    """The check must not cry wolf on the very change that adds it: a commit message (or a
    directive) that MENTIONS `Co-Authored-By` mid-sentence has not signed anything."""
    message = (
        "Verify a run-produced commit carries no attribution trailer (#322)\n\n"
        "The stage prompt says: do NOT add a `Co-Authored-By` trailer, or any other\n"
        "model/agent attribution. See the note about Co-Authored-By handling below.\n"
    )
    assert attribution_findings(message) == ()


def test_signed_off_by_is_left_alone() -> None:
    """DCO is a legitimate human convention; flagging it would make the check unusable in
    any project that uses one."""
    assert attribution_findings("Subject\n\nSigned-off-by: A Human <a@b.c>\n") == ()


def test_scan_commits_reports_only_offenders_with_their_subject() -> None:
    flagged = scan_commits([
        ("aaa", "clean commit\n\nbody\n"),
        ("bbb", f"dirty commit\n\n{TRAILER}\n"),
    ])
    assert [entry["sha"] for entry in flagged] == ["bbb"]
    assert flagged[0]["subject"] == "dirty commit"
    assert flagged[0]["findings"][0]["detail"] == "co-authored-by"


def test_a_pathological_line_is_capped() -> None:
    line = "Co-Authored-By: " + "x" * 5000
    (finding,) = attribution_findings(line)
    assert len(finding["line"]) < 300 and finding["line"].endswith("…")


# --- engine wiring ------------------------------------------------------------------------


def _git(cwd, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _commit(cwd, message: str, *, name: str) -> str:
    (cwd / name).write_text("x")
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-qm", message)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    """A real git repo standing in for the task worktree, plus its base sha."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    return wt, _commit(wt, "base commit", name="base.txt")


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _to_implement(eng: Engine, *, worktree: str | None, base_sha: str | None):
    """Drive intake+scope so the next dispatch is IMPLEMENT, with the context the scan reads."""
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    intake = {"branch": "b", "baseline_captured": True, "worktree": worktree or "/wt/absent"}
    if base_sha:
        intake["base_sha"] = base_sha
    eng.record("r1", make_result(eng.next_work("r1", "t1"), structured_output=intake))
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # scope
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.IMPLEMENT
    return work


def _events(eng: Engine, etype: str) -> list[dict]:
    return [e for e in eng.store.read_events("r1") if e["type"] == etype]


def test_a_signed_commit_trips_the_check(tmp_path, project, worktree) -> None:
    """The whole point: a stage that ignored the directive is LOUD in the durable log."""
    wt, base = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha=base)
    sha = _commit(wt, f"Implement the thing (#322)\n\n{TRAILER}", name="a.py")

    eng.record("r1", make_result(work, checkpoint={"tag": "cp/implement", "sha": sha}))

    (found,) = _events(eng, "commit_attribution_trailer_found")
    assert found["level"] == "warning"  # visible to anyone scanning for trouble
    assert found["sha"] == sha
    assert found["stage"] == "implement"
    assert found["subject"] == "Implement the thing (#322)"
    assert found["findings"][0]["line"] == TRAILER  # the evidence, not just a verdict
    scanned = _events(eng, "commit_attribution_scanned")[-1]  # intake scanned first
    assert (scanned["commits"], scanned["flagged"], scanned["skipped"]) == (1, 1, None)


def test_an_unsigned_commit_passes_but_still_records_the_scan(tmp_path, project, worktree) -> None:
    """"Clean" and "never looked" must not read alike: the passing case leaves positive
    evidence that the check ran over a commit and found nothing."""
    wt, base = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha=base)
    sha = _commit(wt, "Implement the thing (#322)\n\nNo trailer here.", name="a.py")

    eng.record("r1", make_result(work, checkpoint={"tag": "cp/implement", "sha": sha}))

    assert _events(eng, "commit_attribution_trailer_found") == []
    scanned = _events(eng, "commit_attribution_scanned")[-1]
    assert (scanned["commits"], scanned["flagged"], scanned["skipped"]) == (1, 0, None)
    assert scanned["range"] == f"{base}..{sha}"


def test_the_stage_still_succeeds_when_a_trailer_is_found(tmp_path, project, worktree) -> None:
    """Report, don't block. A found trailer is an audit signal — it must not fail a stage
    that otherwise succeeded, and it must not rewrite the (possibly already-pushed) commit."""
    wt, base = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha=base)
    sha = _commit(wt, f"Implement (#322)\n\n{TRAILER}", name="a.py")

    summary = eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert summary["outcome"] == "stage_completed"
    body = _git(wt, "log", "-1", "--format=%B").stdout
    assert TRAILER in body  # untouched: no amend of a commit that may already be remote


def test_each_stage_scans_only_its_own_commits(tmp_path, project, worktree) -> None:
    """Scoped to <previous checkpoint>..<this checkpoint>, so one offending commit is
    flagged ONCE instead of re-reported by every later stage on the same branch."""
    wt, base = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha=base)
    bad = _commit(wt, f"Implement (#322)\n\n{TRAILER}", name="a.py")
    eng.record("r1", make_result(work, checkpoint={"tag": "cp/implement", "sha": bad}))
    assert len(_events(eng, "commit_attribution_trailer_found")) == 1

    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.TEST
    good = _commit(wt, "Add tests (#322)", name="test_a.py")
    eng.record("r1", make_result(nxt, checkpoint={"tag": "cp/test", "sha": good}))

    assert len(_events(eng, "commit_attribution_trailer_found")) == 1  # not re-reported
    test_scan = _events(eng, "commit_attribution_scanned")[-1]
    assert (test_scan["range"], test_scan["commits"]) == (f"{bad}..{good}", 1)


def test_a_missing_worktree_is_recorded_as_a_skip(tmp_path, project) -> None:
    """Never-silent about itself: a scan that could not run says so, rather than leaving a
    gap indistinguishable from a clean pass."""
    _, base = "unused", "0" * 40
    work = _to_implement(eng := _engine(tmp_path, project), worktree=None, base_sha=base)
    eng.record("r1", make_result(work))

    assert _events(eng, "commit_attribution_scanned")[-1]["skipped"] == "no_worktree"


def test_no_anchor_refuses_rather_than_scanning_all_of_history(tmp_path, project, worktree) -> None:
    """Without a lower bound the range would be the project's whole history, flagging every
    pre-existing human commit. Refusing is correct; recording the refusal keeps it honest."""
    wt, _ = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha=None)
    sha = _commit(wt, f"Implement (#322)\n\n{TRAILER}", name="a.py")

    eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert _events(eng, "commit_attribution_scanned")[-1]["skipped"] == "no_base"
    assert _events(eng, "commit_attribution_trailer_found") == []


def test_an_unreadable_range_degrades_to_a_recorded_skip(tmp_path, project, worktree) -> None:
    """A pruned checkpoint tag / rebased branch must not crash a stage that succeeded."""
    wt, _ = worktree
    work = _to_implement(eng := _engine(tmp_path, project), worktree=str(wt), base_sha="deadbee" * 5)
    sha = _commit(wt, "Implement (#322)", name="a.py")

    eng.record("r1", make_result(work, checkpoint={"tag": "cp", "sha": sha}))

    assert _events(eng, "commit_attribution_scanned")[-1]["skipped"].startswith("git_error")
