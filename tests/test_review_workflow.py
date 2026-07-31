"""#73 part 2: ``synthesize`` — the deterministic engine-side review fold, and its
wiring into ``Engine.record``.

This IS the verdict of the multi-agent REVIEW workflow, so it is tested exhaustively
before any runner exists: a fake runner (``make_result(..., sub_results=...)``) drives the
full engine path from day one.

Design tests (b) — the pure fold, table-driven — and (c) — a fake-runner result recording
identically to an equivalent hand-written single review, notice events, stage-log
evidence, and convergence auto-approval on synthesized fingerprints across a fix cycle.
"""

from __future__ import annotations

import json

import pytest

import orchestrator.review_workflow as review_workflow
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.review_workflow import (
    FINGERPRINT_RULE,
    LENS_ORDER,
    issue_fingerprint,
    synthesize,
)
from orchestrator.schemas.enums import Stage, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# --------------------------------------------------------------------------- helpers

CRITICAL = {"severity": "critical", "file": "a.py", "line": 12,
            "description": "breaks the invariant", "suggested_fix": "guard the None case"}
IMPORTANT = {"severity": "important", "file": "b.py",
             "description": "drops the error path"}
SUGGESTION = {"severity": "suggestion", "file": "c.py", "description": "rename the helper"}


def _panel(findings_by_lens: dict, verdicts: list | None = None) -> dict:
    return {"findings_by_lens": findings_by_lens, "verdicts": verdicts or []}


def _verdict(finding: object, verdict: str, reasoning: str = "checked the tree") -> dict:
    return {"fingerprint": issue_fingerprint(finding), "verdict": verdict,
            "reasoning": reasoning}


def _kinds(notices: tuple[dict[str, str], ...]) -> list[str]:
    return [n["notice"] for n in notices]


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"),
                  project, **kw)


def _advance_to_review(eng, run="r1", task="t1"):
    """Drive intake→…→deliver green; return the REVIEW WorkItem."""
    for _ in range(5):  # intake, scope, implement, test, deliver
        eng.record(run, make_result(eng.next_work(run, task)))
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


def _review_log(root, task="t1") -> dict:
    """The most recent REVIEW stage-log payload — the durable record, which (unlike the
    stage RECORD) survives a fix cycle re-opening the stage."""
    return json.loads(sorted((root / "stages" / task).glob("*-review.json"))[-1].read_text())


def _fix_cycle_back_to_review(eng, run="r1", task="t1"):
    """implement→test→deliver again after a rejection; return the re-REVIEW WorkItem."""
    for _ in range(3):
        eng.record(run, make_result(eng.next_work(run, task)))
    w = eng.next_work(run, task)
    assert w.stage is Stage.REVIEW
    return w


# ---------------------------------------------------------------- (b) the pure fold


def test_confirmed_critical_finding_rejects() -> None:
    """The headline case: a finder raises a critical, the adversary fails to kill it,
    the fold refuses approval and hands the finding through UNCHANGED so the fix cycle's
    learnings/fingerprints read exactly as a single reviewer's would."""
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        [_verdict(CRITICAL, "confirmed", "reproduced it")],
    ))
    assert review["approved"] is False
    assert review["issues"] == [CRITICAL]  # object identity of shape, not a re-render
    assert review["non_blocking"] == []
    assert review["tests_meaningful"] is None  # #261: find:tests never ran → no verdict
    assert notices == ()


def test_all_refuted_approves_but_files_the_refutations() -> None:
    """An adversary killing a finding must not silently erase it: the review approves,
    and every refuted finding survives in non_blocking with the verifier's reasoning and
    an explicit file disposition so the false-negative loop stays open to a human."""
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}, "find:spec": {"findings": [IMPORTANT]}},
        [_verdict(CRITICAL, "refuted", "the guard is three lines up"),
         _verdict(IMPORTANT, "refuted", "the error path is covered by the decorator")],
    ))
    assert review["approved"] is True
    assert review["issues"] == []
    titles = [n["title"] for n in review["non_blocking"]]
    assert titles == ["refuted: breaks the invariant", "refuted: drops the error path"]
    assert "the guard is three lines up" in review["non_blocking"][0]["detail"]
    assert "breaks the invariant" in review["non_blocking"][0]["detail"]  # the finding too
    assert all(n["disposition"] == "file" for n in review["non_blocking"])
    assert notices == ()


def test_missing_verifier_leaves_the_finding_blocking() -> None:
    """Fail toward scrutiny: verification may only REMOVE scrutiny it affirmatively
    earned, so a finding nobody verified still blocks."""
    review, notices, _ = synthesize(_panel({"find:code": {"findings": [CRITICAL]}}))
    assert review["approved"] is False
    assert review["issues"] == [CRITICAL]
    assert notices == ()


def test_unmatchable_verdict_leaves_the_finding_blocking_and_notices() -> None:
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        [{"fingerprint": "some:other finding", "verdict": "refuted", "reasoning": "nope"}],
    ))
    assert review["issues"] == [CRITICAL]
    assert review["approved"] is False
    assert _kinds(notices) == ["verdict_without_finding"]


def test_errored_verdict_value_leaves_the_finding_blocking_and_notices() -> None:
    """A verifier that returns something other than confirmed/refuted (a timeout stub, a
    schema-drifted 'error') cannot demote a finding."""
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        [{"fingerprint": issue_fingerprint(CRITICAL), "verdict": "error",
          "reasoning": "the verifier timed out"}],
    ))
    assert review["issues"] == [CRITICAL]
    assert review["approved"] is False
    assert _kinds(notices) == ["unknown_verdict"]


def test_malformed_verdicts_leave_findings_blocking_and_notice() -> None:
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        ["not an object", {"verdict": "refuted", "reasoning": "no fingerprint"}],
    ))
    assert review["issues"] == [CRITICAL]
    assert _kinds(notices) == ["verdict_malformed", "verdict_malformed"]


def test_duplicate_verdict_first_wins_and_notices() -> None:
    """First verdict wins — a second verifier cannot re-litigate a confirmation into a
    refutation (and the extra is recorded, not swallowed)."""
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        [_verdict(CRITICAL, "confirmed", "stands"), _verdict(CRITICAL, "refuted", "no it doesn't")],
    ))
    assert review["issues"] == [CRITICAL]
    assert _kinds(notices) == ["duplicate_verdict"]


def test_explicit_tests_meaningful_false_is_vacuous_with_zero_issues() -> None:
    """The #13 strong form: find:tests is its own dispatch, and its explicit false is
    vacuous even when every other lens is clean."""
    review, notices, _ = synthesize(_panel({
        "find:code": {"findings": []},
        "find:tests": {"findings": [], "tests_meaningful": False},
    }))
    assert review["issues"] == []
    assert review["tests_meaningful"] is False
    assert review["approved"] is False
    assert notices == ()


def test_omitted_tests_lens_folds_to_null_not_judged_and_fails_open() -> None:
    """#261: an omitted judgment folds to NULL — "not judged" — never a synthesized true
    (the fold must not record a verification no lens performed). Fail-OPEN is preserved:
    only an explicit false is vacuous, so the review still approves."""
    review, _, _ = synthesize(_panel({"find:code": {"findings": []}}))
    assert review["tests_meaningful"] is None  # no claim, not an affirmation
    assert review["approved"] is True  # ... and it still fails OPEN

    # ... and so does an omitted field on a present find:tests lens.
    review2, _, _ = synthesize(_panel({"find:tests": {"findings": []}}))
    assert review2["tests_meaningful"] is None
    assert review2["approved"] is True

    # An explicit TRUE from the judging lens is still recorded verbatim — the fix removes
    # the FABRICATED true, not the real one.
    review3, _, _ = synthesize(_panel({"find:tests": {"findings": [], "tests_meaningful": True}}))
    assert review3["tests_meaningful"] is True

    # A non-boolean report is ignored (fails OPEN) and must not become a true either.
    review4, notices, _ = synthesize(
        _panel({"find:tests": {"findings": [], "tests_meaningful": "yes"}})
    )
    assert review4["tests_meaningful"] is None and review4["approved"] is True
    assert _kinds(notices) == ["tests_meaningful_ignored"]


def test_tests_meaningful_from_a_non_tests_lens_is_ignored_with_a_notice() -> None:
    review, notices, _ = synthesize(_panel({
        "find:code": {"findings": [], "tests_meaningful": False},
        "find:tests": {"findings": [], "tests_meaningful": True},
    }))
    assert review["tests_meaningful"] is True  # only find:tests judges tests
    assert review["approved"] is True
    assert _kinds(notices) == ["tests_meaningful_ignored"]


def test_suggestions_are_non_blocking_and_approve() -> None:
    review, notices, _ = synthesize(_panel({"find:code": {"findings": [SUGGESTION]}}))
    assert review["approved"] is True
    assert review["issues"] == []
    assert review["non_blocking"] == [
        {"title": "rename the helper", "detail": "suggestion — c.py — rename the helper",
         "disposition": "file"}
    ]
    assert notices == ()


def test_cross_lens_dedupe_and_stable_sort() -> None:
    """Two lenses independently finding the same thing is agreement, not two issues; the
    survivors sort by (severity rank, fingerprint) so the output — and therefore the
    convergence fingerprint list — is byte-stable."""
    other_critical = {"severity": "critical", "file": "a.py", "description": "aaa first"}
    review, notices, _ = synthesize(_panel({
        "find:spec": {"findings": [IMPORTANT, CRITICAL]},
        "find:code": {"findings": [dict(CRITICAL), other_critical]},
    }))
    assert review["issues"] == [other_critical, CRITICAL, IMPORTANT]  # critical(a<b), then important
    assert notices == ()


def test_fingerprint_matching_is_normalized() -> None:
    """A verifier that re-wrote the finding's whitespace/case still addresses it — both
    sides run the named ``fingerprint-v1`` rule."""
    assert FINGERPRINT_RULE == "fingerprint-v1"
    noisy = {"fingerprint": "A.PY:Breaks   THE\tinvariant", "verdict": "refuted",
             "reasoning": "already guarded"}
    review, notices, _ = synthesize(_panel({"find:code": {"findings": [CRITICAL]}}, [noisy]))
    assert review["issues"] == []
    assert review["approved"] is True
    assert notices == ()


def test_fingerprint_casefold_is_pinned_to_unicode_15(monkeypatch) -> None:
    """Newer Unicode case pairs cannot change an existing fingerprint rule's identity."""
    # U+1C89/U+1C8A became uppercase/lowercase partners after Unicode 15.  The v1 table
    # predates that mapping, so they intentionally remain different fingerprint characters.
    assert issue_fingerprint("\u1c89.py:BUG") == "\u1c89.py:bug"
    assert issue_fingerprint("\u1c8a.py:BUG") == "\u1c8a.py:bug"

    # This interpreter still has Unicode 15, where ``str.casefold()`` happens to return the
    # same result. Prove the public path is wired to the pinned helper, so reverting it to
    # runtime ``casefold()`` fails here rather than only on a newer Python.
    monkeypatch.setattr(review_workflow, "_casefold_v1", lambda text: "pinned-result")
    assert issue_fingerprint("any input") == "pinned-result"


def test_shuffled_lens_order_folds_identically() -> None:
    """The walk is driven by LENS_ORDER, never by the input dict's key order."""
    lenses = {
        "find:tests": {"findings": [], "tests_meaningful": True},
        "find:code": {"findings": [CRITICAL], "improvement": {"title": "from code"}},
        "find:spec": {"findings": [IMPORTANT], "improvement": {"title": "from spec"}},
    }
    forward, n1, _ = synthesize(_panel(lenses))
    reversed_, n2, _ = synthesize(_panel(dict(reversed(list(lenses.items())))))
    assert json.dumps(forward) == json.dumps(reversed_)
    assert _kinds(n1) == _kinds(n2) == ["improvement_dropped"]


def test_repeated_folds_are_byte_identical() -> None:
    panel = _panel(
        {"find:code": {"findings": [CRITICAL, SUGGESTION]},
         "find:spec": {"findings": [IMPORTANT]},
         "find:tests": {"findings": [], "tests_meaningful": True}},
        [_verdict(IMPORTANT, "refuted", "covered")],
    )
    first, n1, _ = synthesize(panel)
    second, n2, _ = synthesize(panel)
    assert json.dumps(first) == json.dumps(second)
    assert n1 == n2


def test_improvement_and_retrospective_take_the_first_in_lens_order() -> None:
    review, notices, _ = synthesize(_panel({
        "find:tests": {"findings": [], "retrospective": {"title": "tests lesson"}},
        "find:spec": {"findings": [], "improvement": {"title": "spec idea"},
                      "retrospective": {"title": "spec lesson"}},
        "find:code": {"findings": [], "improvement": {"title": "code idea"}},
    }))
    assert review["improvement"] == {"title": "code idea"}
    assert review["retrospective"] == {"title": "spec lesson"}
    assert _kinds(notices) == ["improvement_dropped", "retrospective_dropped"]


def test_absent_improvement_and_retrospective_keys_are_omitted() -> None:
    """Matching today's single-reviewer shape: the keys simply are not there."""
    review, _, _ = synthesize(_panel({"find:code": {"findings": []}}))
    assert set(review) == {"approved", "issues", "non_blocking", "tests_meaningful"}


def test_tolerates_unvalidated_lane_shapes() -> None:
    """The panel's input is model-authored and only schema-shaped; a malformed lens must
    never raise out of record(). Everything skipped is noticed, never swallowed."""
    review, notices, _ = synthesize(_panel({
        "find:code": {"findings": ["a plain string finding", "  ", 17,
                                   {"file": "x.py", "description": "  "}]},
        "find:spec": ["not an object"],
        "find:design": {"findings": "not a list"},
    }))
    assert review["issues"] == ["a plain string finding"]  # blocking by default
    assert review["approved"] is False
    assert _kinds(notices) == [
        "finding_skipped", "finding_skipped", "finding_skipped",  # blank / 17 / no description
        "lens_payload_malformed",
        "lens_findings_malformed",
    ]


def test_unknown_severity_blocks_and_notices() -> None:
    """Fail toward scrutiny again: an unrecognized severity is not a licence to skip."""
    review, notices, _ = synthesize(_panel(
        {"find:code": {"findings": [{"severity": "nit", "description": "unranked"}]}}
    ))
    assert review["issues"] == [{"severity": "nit", "description": "unranked"}]
    assert _kinds(notices) == ["unknown_severity"]


def test_missing_severity_blocks_silently() -> None:
    """A severity-less finding is a legitimate schema shape (only ``description`` is
    required), so it blocks WITHOUT a notice — the notice budget is for surprises."""
    bare = {"file": "z.py", "description": "no severity given"}
    review, notices, _ = synthesize(_panel({"find:code": {"findings": [bare]}}))
    assert review["issues"] == [bare]
    assert notices == ()


def test_unknown_lens_folds_after_the_known_ones_with_a_notice() -> None:
    review, notices, _ = synthesize(_panel({
        "find:security": {"findings": [], "improvement": {"title": "from the new lens"}},
        "find:code": {"findings": [], "improvement": {"title": "from code"}},
    }))
    assert review["improvement"] == {"title": "from code"}  # known lenses win precedence
    assert _kinds(notices) == ["unknown_lens", "improvement_dropped"]


def test_malformed_sub_results_root_folds_to_an_empty_panel() -> None:
    for bad in ("nope", [1, 2], 7):
        review, notices, _ = synthesize(bad)
        assert review == {"approved": True, "issues": [], "non_blocking": [],
                          "tests_meaningful": None}  # #261: no lens judged → no claim
        assert _kinds(notices) == ["sub_results_malformed"]
    empty, notices, _ = synthesize({})
    assert empty["approved"] is True and notices == ()


def test_lens_order_is_the_designed_one() -> None:
    assert LENS_ORDER == ("find:code", "find:spec", "find:design", "find:tests")


# ------------------------------------------- (c) the fake runner through Engine.record


def _equivalent(fold_review: dict) -> dict:
    """What a single hand-written reviewer would have had to return for the same verdict."""
    return fold_review


def test_synthesized_rejection_records_identically_to_a_hand_written_review(
    tmp_path, project
) -> None:
    """Design test (c): the workflow is invisible above the seam. A fake runner returning
    sub_results and a hand-written single review that says the same thing must produce
    the same outcome, task state, next stage, learnings and convergence fingerprints."""
    panel = _panel({"find:code": {"findings": [CRITICAL]},
                    "find:tests": {"findings": [], "tests_meaningful": True}},
                   [_verdict(CRITICAL, "confirmed", "reproduced it")])
    folded, _, _ = synthesize(panel)

    eng_a = _engine(tmp_path / "a", project)
    eng_a.create_run("r1")
    eng_a.add_task("r1", "t1")
    out_a = eng_a.record("r1", make_result(_advance_to_review(eng_a), sub_results=panel))

    eng_b = _engine(tmp_path / "b", project)
    eng_b.create_run("r1")
    eng_b.add_task("r1", "t1")
    out_b = eng_b.record(
        "r1", make_result(_advance_to_review(eng_b), structured_output=_equivalent(folded))
    )

    assert out_a["outcome"] == out_b["outcome"] == "review_rejected_fix_cycle"
    assert out_a["task_state"] == out_b["task_state"] == "retrying"
    assert out_a["next_stage"] == out_b["next_stage"] == "implement"
    task_a, task_b = eng_a.store.load_task("r1", "t1"), eng_b.store.load_task("r1", "t1")
    assert task_a.learnings == task_b.learnings
    assert task_a.last_review_rejection == task_b.last_review_rejection
    assert task_a.last_review_rejection == [issue_fingerprint(CRITICAL)]
    assert task_a.review_cycles == task_b.review_cycles == 1
    # the fold's output — not the (absent) runner output — is what the engine recorded
    # (the REVIEW stage record itself is back to PENDING: the fix cycle re-opened it)
    assert _review_log(tmp_path / "a")["structured_output"] == folded


def test_synthesized_approval_records_identically_and_completes(tmp_path, project) -> None:
    panel = _panel({"find:code": {"findings": [CRITICAL, SUGGESTION]},
                    "find:tests": {"findings": [], "tests_meaningful": True}},
                   [_verdict(CRITICAL, "refuted", "the guard is three lines up")])
    folded, _, _ = synthesize(panel)
    assert folded["approved"] is True

    eng_a = _engine(tmp_path / "a", project)
    eng_a.create_run("r1")
    eng_a.add_task("r1", "t1")
    out_a = eng_a.record("r1", make_result(_advance_to_review(eng_a), sub_results=panel))

    eng_b = _engine(tmp_path / "b", project)
    eng_b.create_run("r1")
    eng_b.add_task("r1", "t1")
    out_b = eng_b.record(
        "r1", make_result(_advance_to_review(eng_b), structured_output=_equivalent(folded))
    )
    assert out_a["outcome"] == out_b["outcome"] == "task_completed"
    assert eng_a.store.load_task("r1", "t1").state is TaskState.COMPLETED
    # Engine-authored refuted and advisory entries still reach evidence-out after filing
    # became opt-in because the fold explicitly stamps both as `file`.
    filed = [f["title"] for f in project.task_source.followups]
    assert any(t.startswith("refuted: breaks the invariant") for t in filed)
    assert "rename the helper" in filed


def test_synthesis_notices_land_in_the_event_stream(tmp_path, project) -> None:
    """The fold is pure and sink-free (#235/#201); the engine call site is what emits."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel(
        {"find:code": {"findings": [CRITICAL]}},
        [{"fingerprint": "nothing:matches this", "verdict": "sideways", "reasoning": "?"}],
    )
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    emitted = [e for e in eng.store.read_events("r1") if e["type"] == "review_synthesis_notice"]
    assert [e["notice"] for e in emitted] == ["unknown_verdict", "verdict_without_finding"]
    assert all(e["stage"] == "review" and e["task_id"] == "t1" for e in emitted)
    assert all(e["detail"] for e in emitted)


def test_runner_supplied_output_is_superseded_by_the_fold_not_silently(
    tmp_path, project
) -> None:
    """A runner that ALSO self-reports a verdict does not get to keep it — that is the
    synthesizer-model hole the fold closes — but the override is audited."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel({"find:code": {"findings": [CRITICAL]}})
    out = eng.record("r1", make_result(
        _advance_to_review(eng), sub_results=panel,
        structured_output={"approved": True, "issues": []},  # the runner's own verdict
    ))
    assert out["outcome"] == "review_rejected_fix_cycle"  # the panel's finding still blocks
    notices = [e["notice"] for e in eng.store.read_events("r1")
               if e["type"] == "review_synthesis_notice"]
    assert notices == ["runner_output_superseded"]


def test_stage_log_keeps_the_raw_panel_and_the_folded_verdict(tmp_path, project) -> None:
    """Raw sub_results are the evidence; the folded review is the verdict everything
    downstream consumed. The durable record carries both."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel({"find:code": {"findings": [CRITICAL]}},
                   [_verdict(CRITICAL, "confirmed", "reproduced it")])
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    logs = sorted((tmp_path / "stages" / "t1").glob("*-review.json"))
    payload = json.loads(logs[-1].read_text())
    assert payload["sub_results"] == panel
    assert payload["structured_output"] == synthesize(panel)[0]


def test_plan_less_review_stage_log_has_no_sub_results_key(tmp_path, project) -> None:
    """Regression guard: the non-synthesized path's payload stays byte-identical."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(_advance_to_review(eng),
                                 structured_output={"approved": True, "issues": []}))
    payload = json.loads(sorted((tmp_path / "stages" / "t1").glob("*-review.json"))[-1].read_text())
    assert "sub_results" not in payload
    assert payload["structured_output"] == {"approved": True, "issues": []}


def test_convergence_auto_approval_fires_on_synthesized_fingerprints(tmp_path, project) -> None:
    """Design test (c), second half: a fix cycle driven entirely by the panel. The
    re-review surfacing no NET-NEW finding converges — the convergence math is untouched,
    it just reads fingerprints the fold produced."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel({"find:code": {"findings": [IMPORTANT]},
                    "find:tests": {"findings": [], "tests_meaningful": True}},
                   [_verdict(IMPORTANT, "confirmed", "still there")])
    out = eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert eng.store.load_task("r1", "t1").last_review_rejection == [issue_fingerprint(IMPORTANT)]

    # The fix didn't fully land, but the panel found nothing NEW: a subset re-review.
    out2 = eng.record("r1", make_result(_fix_cycle_back_to_review(eng), sub_results=panel))
    assert out2["outcome"] == "task_completed"
    verdicts = [e for e in eng.store.read_events("r1") if e["type"] == "review_verdict"]
    assert verdicts[-1]["kind"] == "converged_auto_approved"


def test_net_new_synthesized_finding_does_not_converge(tmp_path, project) -> None:
    """The other side of the guard: a re-review that surfaces a finding the first
    rejection did not have is NOT converged."""
    eng = _engine(tmp_path, project, max_review_cycles=2)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    first = _panel({"find:code": {"findings": [IMPORTANT]}},
                   [_verdict(IMPORTANT, "confirmed", "still there")])
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=first))
    second = _panel({"find:code": {"findings": [IMPORTANT]},
                     "find:spec": {"findings": [{"severity": "important", "file": "d.py",
                                                 "description": "net-new finding"}]}})
    out = eng.record("r1", make_result(_fix_cycle_back_to_review(eng), sub_results=second))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert eng.store.load_task("r1", "t1").review_cycles == 2


def test_synthesized_vacuous_tests_rejects_through_the_engine(tmp_path, project) -> None:
    """The #13 gate reads the fold's ``tests_meaningful`` exactly as a single reviewer's."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel({"find:code": {"findings": []},
                    "find:tests": {"findings": [], "tests_meaningful": False}})
    out = eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    assert out["outcome"] == "review_rejected_fix_cycle"
    assert "independent test-validate" in eng.store.load_task("r1", "t1").learnings[-1]


def test_policy_findings_still_override_a_synthesized_approval(tmp_path, project) -> None:
    """Ordering guard: the fold runs BEFORE _merge_policy_findings, so a deterministic
    project policy gate still overrides an approved panel (a model — or a panel — can
    never skip a policy gate)."""
    project.review_findings = lambda worktree=None: [  # type: ignore[attr-defined]
        {"description": "policy: e2e coverage missing", "file": "e2e/"}
    ]
    # max_review_cycles=0 parks instead of re-opening REVIEW, so the merged output the
    # verdict was read from survives on the stage record for inspection.
    eng = _engine(tmp_path, project, max_review_cycles=0)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel({"find:code": {"findings": [CRITICAL]}},
                   [_verdict(CRITICAL, "refuted", "already guarded")])
    out = eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    assert out["outcome"] == "review_rejected_held"
    review = eng.store.load_task("r1", "t1").stages[Stage.REVIEW].output
    assert review["approved"] is False
    assert [i["description"] for i in review["issues"]] == ["policy: e2e coverage missing"]
    # the refuted panel finding is still carried for a human, not erased by the merge
    assert any(n["title"].startswith("refuted:") for n in review["non_blocking"])


def test_ledger_still_writes_one_row_for_a_sub_results_bearing_result(tmp_path, project) -> None:
    """Part-2 boundary: one row per StageResult is unchanged here — one-row-per-sub-call
    is part 3 (#73 design §4), and this pins that this part did not quietly change it."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    w = _advance_to_review(eng)
    before = len(eng.ledger.rows())
    eng.record("r1", make_result(w, sub_results=_panel({"find:code": {"findings": []}})))
    rows = eng.ledger.rows()
    assert len(rows) == before + 1
    assert rows[-1]["stage"] == "review" and rows[-1]["attempt"] == 0


# ---------------------------------- (#285/#268) panel telemetry + the runner-notice contract
#
# Fixture-driven because the fold's purity contract makes constructed ``sub_results``
# sufficient: the summary is a function of that input and nothing else. These results carry
# no dispatched plan, so they never trip the #288 ``review_plan_not_executed`` marker — that
# path is pinned in tests/test_review_plan.py. Actual interactive/headless panel execution is
# covered by the shim conformance harness.

ONLY_CODE = {"severity": "critical", "file": "d.py", "description": "code alone"}
ONLY_DESIGN = {"severity": "critical", "file": "e.py", "description": "design gap alone"}


def _panel_with_notices(findings_by_lens: dict, verdicts: list | None = None,
                        notices: object = None) -> dict:
    panel = _panel(findings_by_lens, verdicts)
    panel["notices"] = notices
    return panel


def _panel_notices(eng, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == "review_panel_notice"]


def test_panel_summary_counts_per_lens_unique_shared_and_agreement() -> None:
    """The signal the fold used to throw away: ``if fingerprint in seen: continue`` is
    cross-lens AGREEMENT, and agreement is what says whether a lens earned its cost. A lens
    whose findings are all shared discovered nothing on its own."""
    summary = synthesize(_panel({
        "find:code": {"findings": [CRITICAL, IMPORTANT, ONLY_CODE]},
        "find:spec": {"findings": [dict(CRITICAL), SUGGESTION]},
    })).panel_summary
    assert summary["lenses"] == {
        "find:code": {"total": 3, "unique": 2, "shared": 1},
        "find:spec": {"total": 2, "unique": 1, "shared": 1},
    }
    assert summary["found_by"] == {
        issue_fingerprint(CRITICAL): ["find:code", "find:spec"],
        issue_fingerprint(IMPORTANT): ["find:code"],
        issue_fingerprint(ONLY_CODE): ["find:code"],
        issue_fingerprint(SUGGESTION): ["find:spec"],
    }
    assert summary["findings"] == 4  # distinct fingerprints
    assert summary["agreed"] == 1  # only CRITICAL was raised by two lenses
    assert summary["finders"] == 2


def test_panel_summary_finder_count_includes_a_lens_that_found_nothing() -> None:
    """``finders`` is derived from the findings_by_lens KEYS (sub_calls are not visible to
    the fold), so a lens that ran and found nothing still counts as a dispatch made."""
    summary = synthesize(_panel({
        "find:code": {"findings": [CRITICAL]},
        "find:tests": {"findings": [], "tests_meaningful": True},
    })).panel_summary
    assert summary["finders"] == 2
    assert "find:tests" not in summary["lenses"]  # it raised nothing to attribute
    assert summary["findings"] == 1


def test_panel_summary_tallies_verdicts_inconclusive_verifiers_and_the_cap() -> None:
    """The verification half: what the panel actually adjudicated, and what it left
    unverified. ``verifiers`` is derived (verdicts + inconclusive notices) because the fold
    cannot see ``sub_calls`` — which is exactly why the runner must DECLARE an inconclusive
    verifier rather than stay silent."""
    summary = synthesize(_panel_with_notices(
        {"find:code": {"findings": [CRITICAL, IMPORTANT, ONLY_CODE]}},
        [_verdict(CRITICAL, "confirmed"), _verdict(IMPORTANT, "refuted")],
        [{"notice": "verifier_inconclusive", "detail": "verify:3 timed out — stays blocking"},
         {"notice": "verifier_cap", "detail": "12 blocking findings exceed the 8-verifier cap",
          "count": 4}],
    )).panel_summary
    assert summary["verdicts"] == {"confirmed": 1, "refuted": 1}
    assert summary["inconclusive"] == 1
    assert summary["verifiers"] == 3  # 2 verdicts + 1 inconclusive
    assert summary["cap_hit"] is True and summary["cap_dropped"] == 4


def test_panel_summary_counts_a_coerced_verdict_as_confirmed() -> None:
    """The tallies describe what the VERDICT was computed from, after the fold's
    fail-toward-scrutiny coercions — an unrecognized verdict value blocked the finding, so
    counting it as confirmed is what keeps the telemetry and the review.json agreeing."""
    summary = synthesize(_panel(
        {"find:code": {"findings": [CRITICAL]}},
        [{"fingerprint": issue_fingerprint(CRITICAL), "verdict": "error", "reasoning": "?"}],
    )).panel_summary
    assert summary["verdicts"] == {"confirmed": 1, "refuted": 0}


def test_panel_summary_is_byte_stable_and_independent_of_lens_dict_order() -> None:
    """Same determinism contract as ``review`` itself: replay must reproduce the recorded
    summary exactly, and a differently-ordered findings_by_lens is the SAME panel.

    ``find:spec`` and ``find:design`` are the one pair where ``LENS_ORDER``
    (``code, spec, design, tests``) and alphabetical order actually diverge — every other
    lens pair happens to sort the same way either method is applied, so a fixture missing
    this pair cannot distinguish "sorted by name" from "walk order" and proves nothing about
    which one ``_panel_summary`` actually uses. Both lenses carry a distinct finding so each
    key is genuinely present in ``summary["lenses"]``, not just in the input.
    """
    lenses = {
        "find:tests": {"findings": [dict(CRITICAL)], "tests_meaningful": True},
        "find:code": {"findings": [CRITICAL, ONLY_CODE]},
        "find:design": {"findings": [ONLY_DESIGN, dict(IMPORTANT)]},
        "find:spec": {"findings": [IMPORTANT]},
    }
    first = synthesize(_panel(lenses)).panel_summary
    again = synthesize(_panel(lenses)).panel_summary
    reordered = synthesize(_panel(dict(reversed(list(lenses.items()))))).panel_summary
    assert json.dumps(first) == json.dumps(again) == json.dumps(reordered)
    # Sorted by lens NAME (the documented contract), not LENS_ORDER: "design" < "spec"
    # alphabetically even though LENS_ORDER walks spec before design. If ``_panel_summary``
    # ever switched to preserving walk order instead, this would need to become
    # ["find:code", "find:spec", "find:design", "find:tests"] — and say so explicitly.
    assert list(first["lenses"]) == ["find:code", "find:design", "find:spec", "find:tests"]
    assert list(first["found_by"]) == sorted(first["found_by"])
    # Attribution lists are sorted by lens name too, rather than inheriting the fold order.
    assert first["found_by"][issue_fingerprint(IMPORTANT)] == ["find:design", "find:spec"]
    assert sorted(LENS_ORDER) != list(LENS_ORDER)  # the divergence this test exists to catch


def test_panel_summary_never_leaks_into_review_json() -> None:
    """Canonical findings pass through the fold unchanged: the telemetry is a SEPARATE
    return value, and review.json keeps exactly the keys a single reviewer's would."""
    folded = synthesize(_panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}}, None,
        [{"notice": "verifier_cap", "detail": "capped", "count": 2}],
    ))
    assert set(folded.review) == {"approved", "issues", "non_blocking", "tests_meaningful"}
    assert folded.review["issues"] == [CRITICAL]
    assert folded.review["issues"][0] is CRITICAL
    assert "found_by" not in folded.review["issues"][0]
    assert folded.panel_summary["found_by"] == {
        issue_fingerprint(CRITICAL): ["find:code"]
    }
    assert folded.panel_summary["cap_dropped"] == 2


def test_synthesis_result_is_still_indexable_and_iterable() -> None:
    """The shape change is additive: ``[0]``/``[1]`` and unpacking keep working, named
    access is the documentation."""
    folded = synthesize(_panel({"find:code": {"findings": [CRITICAL]}}))
    review, notices, summary = folded
    assert folded[0] is review is folded.review
    assert folded[1] == notices == folded.notices
    assert folded[2] is summary is folded.panel_summary


# --- the runner-notice contract (#268) ----------------------------------------------------


def test_unknown_runner_notice_kind_passes_through_intact() -> None:
    """A runner signal this engine's table has not learned about must reach the event
    stream, not vanish at the seam — it simply contributes no counter."""
    summary = synthesize(_panel_with_notices(
        {"find:code": {"findings": []}}, None,
        [{"notice": "some_future_signal", "detail": "a runner told us something new"}],
    )).panel_summary
    assert summary["notices"] == [
        {"notice": "some_future_signal", "detail": "a runner told us something new"}
    ]
    assert summary["cap_hit"] is False and summary["inconclusive"] == 0


def test_malformed_runner_notices_are_skipped_with_a_fold_notice() -> None:
    """Never raise, never swallow: the model-authored panel can hand us anything."""
    folded = synthesize(_panel_with_notices(
        {"find:code": {"findings": []}}, None,
        ["not an object", {"detail": "no kind"}, {"notice": "  "},
         {"notice": "verifier_cap", "detail": "capped", "count": "four"}],
    ))
    assert _kinds(folded.notices) == [
        "runner_notice_malformed", "runner_notice_malformed", "runner_notice_malformed",
        "runner_notice_extra_malformed",
    ]
    # the survivor keeps its notice/detail; only the unusable count was dropped
    assert folded.panel_summary["notices"] == [{"notice": "verifier_cap", "detail": "capped"}]
    assert folded.panel_summary["cap_hit"] is True
    assert folded.panel_summary["cap_dropped"] == 0  # flagged, but honestly uncounted


def test_non_list_runner_notices_degrade_to_none_with_a_fold_notice() -> None:
    folded = synthesize(_panel_with_notices({"find:code": {"findings": []}}, None, "oops"))
    assert _kinds(folded.notices) == ["runner_notices_malformed"]
    assert folded.panel_summary["notices"] == []


def test_absent_runner_notices_are_silent() -> None:
    """A panel with nothing to report is the normal case — no notice budget spent."""
    folded = synthesize(_panel({"find:code": {"findings": []}}))
    assert folded.notices == () and folded.panel_summary["notices"] == []


def test_the_fold_never_parses_a_notice_detail_string() -> None:
    """The contract is structural: a count lives in the declared ``count`` extra, never in
    prose. A detail that TALKS about numbers contributes none of them."""
    summary = synthesize(_panel_with_notices(
        {"find:code": {"findings": []}}, None,
        [{"notice": "verifier_cap", "detail": "12 blocking findings, 4 unverified"}],
    )).panel_summary
    assert summary["cap_dropped"] == 0


# --- through Engine.record: persistence + the #268 events ---------------------------------


def test_panel_summary_lands_in_the_stage_log_next_to_the_raw_sub_results(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}, "find:spec": {"findings": [dict(CRITICAL)]}},
        [_verdict(CRITICAL, "confirmed", "reproduced it")],
        [{"notice": "verifier_cap", "detail": "capped", "count": 3}],
    )
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    payload = _review_log(tmp_path)
    assert payload["sub_results"] == panel  # the raw evidence is untouched
    assert payload["panel_summary"] == synthesize(panel).panel_summary
    assert payload["panel_summary"]["agreed"] == 1
    assert payload["structured_output"] == synthesize(panel).review  # no telemetry leaked


def test_panel_summary_is_absent_on_a_single_reviewer_review(tmp_path, project) -> None:
    """The shape today's interactive lane actually produces (#288): no plan means no
    sub_results, so there is no panel and the key must not appear at all."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(_advance_to_review(eng),
                                 structured_output={"approved": True, "issues": []}))
    payload = _review_log(tmp_path)
    assert "panel_summary" not in payload and "sub_results" not in payload
    assert _panel_notices(eng) == []


def test_panel_summary_is_absent_on_a_failed_panel(tmp_path, project) -> None:
    """A finder that failed terminally short-circuits the panel: there is no verdict to
    fold and no honest summary to write, so neither is recorded."""
    from orchestrator.schemas.enums import ResultStatus

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(
        _advance_to_review(eng), status=ResultStatus.FAILURE, error="finder find:code failed",
        sub_results=_panel_with_notices({}, None, [{"notice": "verifier_cap", "detail": "x"}]),
    ))
    payload = _review_log(tmp_path)
    assert "panel_summary" not in payload
    assert payload["sub_results"] is not None  # the evidence is still kept
    assert _panel_notices(eng) == []


def test_every_runner_notice_kind_reaches_the_event_stream_at_warning_grade(
    tmp_path, project
) -> None:
    """#268's whole point: these were persisted in the stage log and NOWHERE else, so
    "this review only verified 8 of 12 blocking findings" never reached status, the
    dashboard or alerting."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}}, None,
        [{"notice": "verifier_cap", "detail": "12 exceed the cap", "count": 4},
         {"notice": "verifier_inconclusive", "detail": "verify:2 timed out"},
         {"notice": "unknown_dedupe_rule", "detail": "plan rule is not fingerprint-v1"},
         {"notice": "some_future_signal", "detail": "not in the table yet"}],
    )
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    emitted = _panel_notices(eng)
    assert [e["notice"] for e in emitted] == [
        "verifier_cap", "verifier_inconclusive", "unknown_dedupe_rule", "some_future_signal",
    ]
    assert all(e["level"] == "warning" for e in emitted)
    assert all(e["stage"] == "review" and e["task_id"] == "t1" for e in emitted)
    assert emitted[0]["count"] == 4  # the declared extra rides the event, not just prose


def test_malformed_notices_do_not_crash_record_and_still_emit_the_survivor(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}}, None,
        ["not an object", {"notice": "verifier_inconclusive", "detail": "verify:1 errored"}],
    )
    out = eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    assert out["outcome"] == "review_rejected_fix_cycle"  # the verdict is unaffected
    assert [e["notice"] for e in _panel_notices(eng)] == ["verifier_inconclusive"]
    synth = [e["notice"] for e in eng.store.read_events("r1")
             if e["type"] == "review_synthesis_notice"]
    assert synth == ["runner_notice_malformed"]


def test_a_non_list_notices_field_does_not_crash_record(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel_with_notices({"find:code": {"findings": []}}, None, 7)
    out = eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    assert out["outcome"] == "task_completed"
    assert _panel_notices(eng) == []


def test_a_replayed_record_appends_no_duplicate_panel_notice(
    tmp_path, project, monkeypatch
) -> None:
    """The #277 boundary is the ONLY dedupe: the panel notices ride the same events batch
    as ``stage_recorded``, so a crash at the task-doc write and a replay converge on one
    event per notice — no second dedupe key was invented."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    panel = _panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}}, None,
        [{"notice": "verifier_cap", "detail": "capped", "count": 2}],
    )
    result = make_result(_advance_to_review(eng), sub_results=panel)

    original = eng.store._write_task
    state = {"armed": True}

    def boom(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("crash at the single durable commit point")
        return original(*args, **kwargs)

    monkeypatch.setattr(eng.store, "_write_task", boom)
    with pytest.raises(RuntimeError):
        eng.record("r1", result)
    assert len(_panel_notices(eng)) == 1  # the batch landed before the crash

    eng.record("r1", result)  # the replay
    assert len(_panel_notices(eng)) == 1
    assert len([e for e in eng.store.read_events("r1")
                if e["type"] == "stage_recorded" and e.get("work_item_id") == result.work_item_id
                ]) == 1


def test_status_surfaces_panel_notices_per_task(tmp_path, project) -> None:
    """A capped or inconclusive review is visible from a poll — without opening a stage
    log — and a clean run says so."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    clean = eng.status("r1")["review_panel"]
    assert clean == {
        "notices": 0, "by_notice": {}, "by_task": {},
        # #288: the two other ways a requested panel fails to materialize, both zero here.
        "plan_not_executed": 0, "plan_not_executed_by_task": {},
        "workflow_skipped": 0, "workflow_skipped_by_reason": {},
        "clean": True,
    }

    panel = _panel_with_notices(
        {"find:code": {"findings": [CRITICAL]}}, None,
        [{"notice": "verifier_cap", "detail": "capped", "count": 4},
         {"notice": "verifier_inconclusive", "detail": "verify:1 timed out"},
         {"notice": "verifier_inconclusive", "detail": "verify:2 timed out"}],
    )
    eng.record("r1", make_result(_advance_to_review(eng), sub_results=panel))
    audit = eng.status("r1")["review_panel"]
    assert audit["clean"] is False
    assert audit["notices"] == 3
    assert audit["by_notice"] == {"verifier_cap": 1, "verifier_inconclusive": 2}
    assert audit["by_task"] == {"t1": {"verifier_cap": 1, "verifier_inconclusive": 2}}
