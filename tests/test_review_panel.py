"""#73 part 5/5: headless×claude transport execution of a REVIEW plan — find → dedupe →
adversarial verify (design §2, test (g)).

The panel is ONE dispatch that fans out below the seam: one blind finder per lens, then one
adversarial verifier per deduped blocking finding, returning one StageResult whose
``sub_results`` the engine's pure fold turns into canonical ``review.json``.

The load-bearing properties under test are the FAILURE DIRECTIONS. A verifier that errors,
times out, or echoes an unmatchable fingerprint may not demote its finding — verification
only removes scrutiny it affirmatively earned. A FINDER that dies takes the whole dispatch
with it: a missing lens is a missing review, not a lenient one.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.review_panel import _MAX_VERIFIERS, run_review_panel
from adapters.execution.runners import build_registry
from adapters.execution.transport import (
    RawResult,
    claude_cli_transport,
    stream_teeing_transport,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.review_workflow import issue_fingerprint, synthesize
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import (
    FinderSpec,
    LanePolicy,
    ReviewPlan,
    WorkItem,
)
from orchestrator.status_store import StatusStore

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)

_VERIFY_TEMPLATE = (
    "## VERIFY: try to refute this finding\n### Finding\n{finding}\n\n"
    "### Where to look\n{diff_hint}\n"
)


def _plan(*lenses: str, dedupe_rule: str = "fingerprint-v1") -> ReviewPlan:
    return ReviewPlan(
        finders=tuple(
            FinderSpec(lens=lens, prompt=f"## {lens.upper()}\nlook", agent=f"{lens}-agent",
                       schema_ref="review_findings")
            for lens in lenses
        ),
        verify_template=_VERIFY_TEMPLATE,
        verify_schema_ref="review_verdict",
        dedupe_rule=dedupe_rule,
    )


def _work(plan: ReviewPlan, **kw) -> WorkItem:
    args: dict = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.REVIEW, prompt="single-reviewer prompt",
        schema_ref="review", model="claude-opus-5", created_at="now", lane_policy=H,
        agent="code-reviewer", cwd="/wt/1", session_ref="sess-stage",
        checkpoint_tag="ckpt/t1/review", reset_to="ckpt/t1/implement",
        salvage_anchor="ckpt/t1/implement", plan=plan,
    )
    args.update(kw)
    return WorkItem.create(**args)


def _finding(description: str, *, severity: str = "critical", file: str = "a.py",
             line: int = 7) -> dict:
    return {"severity": severity, "file": file, "line": line, "description": description}


class FakeTransport:
    """A scripted transport: ``by_phase`` maps a sub-call's phase to the RawResult it gets.

    Records every WorkItem it was handed, which is how the sub-item hygiene assertions see
    what the panel actually dispatched."""

    def __init__(self, by_phase: dict[str, RawResult], default: RawResult | None = None) -> None:
        self.by_phase = by_phase
        self.default = default
        self.seen: list[WorkItem] = []

    def __call__(self, work: WorkItem) -> RawResult:
        self.seen.append(work)
        raw = self.by_phase.get(work.phase or "", self.default)
        assert raw is not None, f"unscripted sub-call phase {work.phase}"
        return raw

    @property
    def phases(self) -> list[str]:
        return [w.phase for w in self.seen if w.phase]


def _findings_raw(*findings: dict, **extra) -> RawResult:
    return RawResult({"findings": list(findings), **extra}, raw_output="finder stream")


def _claude_stream(structured: dict, session: str = "s1") -> str:
    """A minimal claude ``stream-json`` stdout carrying one sub-call's structured output."""
    return (
        json.dumps({"type": "system", "subtype": "init", "session_id": session}) + "\n"
        + json.dumps({"type": "result", "subtype": "success", "session_id": session,
                      "structured_output": structured, "usage": {}}) + "\n"
    )


def _verdict_raw(fingerprint: str, verdict: str, reasoning: str = "read the code") -> RawResult:
    return RawResult(
        {"fingerprint": fingerprint, "verdict": verdict, "reasoning": reasoning},
        raw_output="verifier stream",
    )


# --- fan-out: one sub-call per finder, streams at the documented paths --------------------

def test_panel_fans_out_one_sub_call_per_finder(tmp_path) -> None:
    plan = _plan("find:code", "find:spec", "find:tests")
    transport = FakeTransport({}, default=_findings_raw())

    result = run_review_panel(_work(plan), transport)

    assert transport.phases == ["find:code", "find:spec", "find:tests"]
    assert [c.phase for c in result.sub_calls or ()] == ["find:code", "find:spec", "find:tests"]
    assert set(result.sub_results["findings_by_lens"]) == {
        "find:code", "find:spec", "find:tests",
    }
    assert result.status is ResultStatus.SUCCESS
    # The fold owns review.json: the runner never self-reports an output for a plan review.
    assert result.structured_output is None


def test_each_sub_call_streams_to_its_own_phase_named_file(tmp_path) -> None:
    """Design §2's per-sub-call tee, through the REAL teeing wrapper: one stream file per
    sub-call, named ``<stage>-attempt<N>.<phase>.stream.jsonl`` with the phase sanitized
    colon-free."""
    plan = _plan("find:code", "find:spec")
    fp = issue_fingerprint(_finding("boom"))
    transport = stream_teeing_transport(
        FakeTransport(
            {
                "find:code": _findings_raw(_finding("boom")),
                "find:spec": _findings_raw(),
                "verify:1": _verdict_raw(fp, "confirmed"),
            }
        ),
        tmp_path,
    )

    result = run_review_panel(_work(plan, attempt=2), transport)

    stages = tmp_path / "stages" / "t1"
    for name in ("find-code", "find-spec", "verify-1"):
        assert (stages / f"review-attempt2.{name}.stream.jsonl").is_file()
    assert [c.stream_file for c in result.sub_calls or ()] == [
        "stages/t1/review-attempt2.find-code.stream.jsonl",
        "stages/t1/review-attempt2.find-spec.stream.jsonl",
        "stages/t1/review-attempt2.verify-1.stream.jsonl",
    ]
    # The dispatch-level map keeps the pre-#73 top-level `stream` key AND lists every sub-call.
    assert result.stream_files["stream"] == "stages/t1/review-attempt2.find-code.stream.jsonl"
    assert [f["phase"] for f in result.stream_files["sub_calls"]] == [
        "find:code", "find:spec", "verify:1",
    ]


def test_the_live_streaming_path_tees_each_sub_call_to_its_phase_file(
    tmp_path, monkeypatch
) -> None:
    """The in-flight (#66) tee, which is what a live ``orchestrator tail`` follows: the REAL
    ``claude_cli_transport`` streaming path names every sub-call's file by phase, and a
    sub-call's own schema retry suffixes THAT phase's file."""
    import adapters.execution.transport as T

    schema = json.dumps({"type": "object", "required": ["findings"]})
    streams = [
        _claude_stream({"nope": 1}),                 # find:code, invalid
        _claude_stream({"findings": []}),            # find:code retry1, corrected
        _claude_stream({"findings": []}),            # find:spec
    ]
    names: list[str] = []

    def fake_teed(argv, *, timeout, cwd, tee_path, env=None):
        stdout = streams[min(len(names), len(streams) - 1)]
        tee_path.parent.mkdir(parents=True, exist_ok=True)
        tee_path.write_text(stdout, encoding="utf-8")
        names.append(tee_path.name)
        return 0, stdout, ""

    monkeypatch.setattr(T, "_run_teed", fake_teed)
    transport = claude_cli_transport(lambda ref: schema, run_log_root=tmp_path)

    result = run_review_panel(_work(_plan("find:code", "find:spec")), transport)

    assert names == [
        "review-attempt0.find-code.stream.jsonl",
        "review-attempt0.find-code.retry1.stream.jsonl",
        "review-attempt0.find-spec.stream.jsonl",
    ]
    by_phase = {c.phase: c for c in result.sub_calls or ()}
    assert by_phase["find:code"].schema_retries == 1
    assert by_phase["find:code"].stream_file == \
        "stages/t1/review-attempt0.find-code.retry1.stream.jsonl"


def test_sub_items_are_blind_and_carry_no_dispatch_bookkeeping() -> None:
    """Finder blindness is load-bearing: resuming the stage's session would let each finder
    read the previous finders' turns. The checkpoint fields go too, so the wrapper can't tag
    or reset the worktree once per sub-call."""
    plan = _plan("find:code", "find:spec")
    transport = FakeTransport({}, default=_findings_raw())

    run_review_panel(_work(plan), transport)

    for sub, finder in zip(transport.seen, plan.finders, strict=True):
        assert sub.session_ref is None
        assert sub.plan is None  # no recursion back into the panel driver
        assert sub.checkpoint_tag is None and sub.reset_to is None
        assert sub.salvage_anchor is None
        assert sub.prompt == finder.prompt and sub.agent == finder.agent
        assert sub.schema_ref == "review_findings"
        assert sub.cwd == "/wt/1"  # the worktree still rides — the finders read the tree
        assert sub.model == "claude-opus-5"


# --- dedupe ------------------------------------------------------------------------------

def test_cross_lens_duplicates_collapse_to_one_verifier_call() -> None:
    """Two lenses reporting the same finding (modulo whitespace/case — the fingerprint-v1
    normalization) buy ONE verifier, not two."""
    plan = _plan("find:code", "find:spec")
    fp = issue_fingerprint(_finding("Null deref in the guard"))
    transport = FakeTransport({
        "find:code": _findings_raw(_finding("Null deref in the guard")),
        "find:spec": _findings_raw(_finding("null   DEREF in the guard")),
        "verify:1": _verdict_raw(fp, "refuted"),
    })

    result = run_review_panel(_work(plan), transport)

    assert transport.phases == ["find:code", "find:spec", "verify:1"]
    assert len(result.sub_results["verdicts"]) == 1


def test_an_unknown_dedupe_rule_verifies_everything_and_says_so() -> None:
    """A rule the runner doesn't implement must never SILENTLY collapse the wrong findings:
    it verifies everything (waste, not a correctness loss) and notices the mismatch."""
    plan = _plan("find:code", "find:spec", dedupe_rule="fingerprint-v9")
    transport = FakeTransport(
        {
            "find:code": _findings_raw(_finding("same thing")),
            "find:spec": _findings_raw(_finding("same thing")),
        },
        default=_verdict_raw(issue_fingerprint(_finding("same thing")), "confirmed"),
    )

    result = run_review_panel(_work(plan), transport)

    assert transport.phases == ["find:code", "find:spec", "verify:1", "verify:2"]
    assert [n["notice"] for n in result.sub_results["notices"]] == ["unknown_dedupe_rule"]


def test_dedupe_order_follows_the_folds_lens_walk() -> None:
    """First-occurrence-wins must pick the SAME representative on both sides of the seam, so
    the runner walks the FOLD's fixed lens order (…design, tests) rather than the plan's own
    (…tests, design). Two lenses report one fingerprint with different details; the object
    the verifier is shown must be the one the fold will keep."""
    plan = _plan("find:tests", "find:design")
    transport = FakeTransport(
        {
            "find:tests": _findings_raw(_finding("dup", line=99)),
            "find:design": _findings_raw(_finding("dup", line=1)),
        },
        default=_verdict_raw("whatever", "confirmed"),
    )

    result = run_review_panel(_work(plan), transport)

    assert [p for p in transport.phases if p.startswith("verify:")] == ["verify:1"]
    verify_prompt = next(w.prompt for w in transport.seen if w.phase == "verify:1")
    assert "- line: 1" in verify_prompt  # find:design's object — the fold keeps the same one
    review, _, _ = synthesize(result.sub_results)
    assert review["issues"] == [_finding("dup", line=1)]


# --- verify scope + the cap --------------------------------------------------------------

def test_suggestion_findings_skip_verification() -> None:
    """Build-time decision (design open question 1): suggestions cannot block, so a verifier
    call on one buys nothing."""
    plan = _plan("find:code")
    blocking = _finding("real bug")
    transport = FakeTransport({
        "find:code": _findings_raw(blocking, _finding("nit", severity="suggestion")),
        "verify:1": _verdict_raw(issue_fingerprint(blocking), "confirmed"),
    })

    run_review_panel(_work(plan), transport)

    assert transport.phases == ["find:code", "verify:1"]


def test_absent_or_unrecognized_severity_is_verified_like_a_blocking_finding() -> None:
    plan = _plan("find:code")
    no_sev = {"file": "a.py", "description": "unlabelled"}
    weird = {"severity": "showstopper", "file": "b.py", "description": "odd label"}
    transport = FakeTransport(
        {"find:code": _findings_raw(no_sev, weird)},
        default=_verdict_raw("nope", "confirmed"),
    )

    run_review_panel(_work(plan), transport)

    assert transport.phases == ["find:code", "verify:1", "verify:2"]


def test_verifier_cap_bounds_the_calls_and_leaves_the_dropped_findings_blocking() -> None:
    """The design caps finders by construction but leaves verifiers linear in findings. The
    cap is deterministic (severity rank, fingerprint), the drop is NOTICED, and an unverified
    finding keeps no verdict — which the fold reads as blocking."""
    findings = [_finding(f"bug number {i:02d}") for i in range(_MAX_VERIFIERS + 4)]
    plan = _plan("find:code")
    transport = FakeTransport(
        {"find:code": _findings_raw(*findings)},
        default=_verdict_raw("unmatchable", "refuted"),
    )

    result = run_review_panel(_work(plan), transport)

    verifier_phases = [p for p in transport.phases if p.startswith("verify:")]
    assert len(verifier_phases) == _MAX_VERIFIERS
    cap_notice = next(n for n in result.sub_results["notices"] if n["notice"] == "verifier_cap")
    assert f"{len(findings)} blocking findings" in cap_notice["detail"]
    # #268: the count rides STRUCTURALLY, not only in the prose — the engine-side fold reads
    # the declared extra and must never regex a detail string to recover it.
    assert cap_notice["count"] == len(findings) - _MAX_VERIFIERS
    # Nothing was verified away, so the fold still blocks on every one of them.
    folded = synthesize(result.sub_results)
    assert len(folded.review["issues"]) == len(findings)
    assert folded.review["approved"] is False
    # …and the panel telemetry says so, from the runner's declared count (#285).
    assert folded.panel_summary["cap_hit"] is True
    assert folded.panel_summary["cap_dropped"] == len(findings) - _MAX_VERIFIERS
    assert folded.panel_summary["verifiers"] == _MAX_VERIFIERS  # every call was inconclusive


def test_the_verifier_cap_spends_its_budget_on_the_most_severe_findings_first() -> None:
    """The cap's ordering is ``(severity rank, fingerprint)`` so scarce verifier calls go to
    ``critical`` before ``important``. Every other cap test uses ``_finding()``'s uniform
    ``critical`` default, which exercises only the fingerprint tiebreak — sorting by
    fingerprint ALONE would pass them. Mixing severities pins the rank component."""
    # The descriptions are chosen so the FINGERPRINT order fights the SEVERITY order: the
    # fingerprint is `file:description`, so "zz…" (critical) sorts AFTER "aa…" (important).
    # Without this the two orders coincide and a fingerprint-only sort passes vacuously.
    criticals = [_finding(f"zz crit {i:02d}", severity="critical") for i in range(_MAX_VERIFIERS)]
    importants = [_finding(f"aa imp {i:02d}", severity="important") for i in range(4)]
    # Interleaved on the wire, so ordering can only come from the sort, not arrival order.
    findings = [x for pair in zip(importants, criticals, strict=False) for x in pair]
    findings += criticals[len(importants) :]
    transport = FakeTransport(
        {"find:code": _findings_raw(*findings)},
        default=_verdict_raw("unmatchable", "refuted"),
    )

    run_review_panel(_work(_plan("find:code")), transport)

    verified = [w.prompt for w in transport.seen if (w.phase or "").startswith("verify:")]
    assert len(verified) == _MAX_VERIFIERS
    # The whole budget went to criticals; not one `important` displaced one.
    assert all("- severity: critical" in p for p in verified)
    assert {issue_fingerprint(f) for f in criticals} == {
        line.split("- fingerprint: ")[1].splitlines()[0]
        for p in verified
        for line in p.splitlines()
        if line.startswith("- fingerprint: ")
    }


def test_the_verify_prompt_is_mechanical_slot_substitution_only() -> None:
    plan = _plan("find:code")
    finding = _finding("off-by-one in the loop {not a slot}", file="loop.py", line=42)
    transport = FakeTransport(
        {"find:code": _findings_raw(finding)},
        default=_verdict_raw(issue_fingerprint(finding), "confirmed"),
    )

    run_review_panel(_work(plan), transport)

    prompt = next(w.prompt for w in transport.seen if w.phase == "verify:1")
    assert prompt.startswith("## VERIFY: try to refute this finding")
    assert f"- fingerprint: {issue_fingerprint(finding)}" in prompt
    assert "- severity: critical" in prompt
    assert "off-by-one in the loop {not a slot}" in prompt  # braces survive verbatim
    assert "### Where to look\nloop.py:42" in prompt
    # No slot left unfilled. Sound only because THIS finding's own text contains neither
    # token — see the test below for the case where it does.
    assert "{finding}" not in prompt and "{diff_hint}" not in prompt


def test_a_finding_quoting_a_slot_name_survives_verbatim_and_is_not_re_substituted() -> None:
    """Chained ``str.replace``s let a LATER slot's substitution re-scan text an EARLIER one
    injected, so a finding whose description quotes ``{diff_hint}`` had it silently rewritten
    into the where-to-look pointer. On-path, not hypothetical: this repo review-panels its own
    source, and a finder discussing these placeholders writes them verbatim. The single-pass
    render never revisits what it emitted, so BOTH slot names survive inside the finding."""
    finding = _finding(
        "the template's {diff_hint} slot is filled after {finding}, which corrupts it",
        file="review_panel.py",
        line=277,
    )
    transport = FakeTransport(
        {"find:code": _findings_raw(finding)},
        default=_verdict_raw(issue_fingerprint(finding), "confirmed"),
    )

    run_review_panel(_work(_plan("find:code")), transport)

    prompt = next(w.prompt for w in transport.seen if w.phase == "verify:1")
    # The description reaches the adversary exactly as the finder wrote it...
    assert (
        "- description: the template's {diff_hint} slot is filled after {finding}, "
        "which corrupts it"
    ) in prompt
    # ...and the real slots were still filled: the pointer is the finding's own file:line,
    # and it appears ONCE (the pre-fix chain also injected it into the description above).
    assert "### Where to look\nreview_panel.py:277" in prompt
    assert prompt.count("review_panel.py:277") == 1


# --- failure directions: the verifier fails OPEN toward blocking -------------------------

def _panel_with_verifier(raw: RawResult) -> tuple[dict, list]:
    finding = _finding("the bug")
    plan = _plan("find:code")
    transport = FakeTransport({"find:code": _findings_raw(finding), "verify:1": raw})
    result = run_review_panel(_work(plan), transport)
    return result.sub_results, list(result.sub_calls or ())


def test_verifier_error_leaves_the_finding_blocking() -> None:
    sub_results, sub_calls = _panel_with_verifier(
        RawResult(None, exit_code=1, error="boom", raw_output="")
    )
    assert sub_results["verdicts"] == []
    review, _, _ = synthesize(sub_results)
    assert review["approved"] is False and len(review["issues"]) == 1
    assert [c.phase for c in sub_calls] == ["find:code", "verify:1"]  # spend still attributed


def test_verifier_timeout_leaves_the_finding_blocking() -> None:
    sub_results, _ = _panel_with_verifier(
        RawResult(None, exit_code=124, error="timed out after 600s")
    )
    assert sub_results["verdicts"] == []
    assert synthesize(sub_results)[0]["approved"] is False


def test_verifier_schema_violation_leaves_the_finding_blocking() -> None:
    """Exit 0 but no structured output: the transport's schema-retry loop was exhausted."""
    sub_results, _ = _panel_with_verifier(RawResult(None, raw_output="prose, not JSON"))
    assert sub_results["verdicts"] == []
    assert synthesize(sub_results)[0]["approved"] is False


def test_verifier_with_an_unmatchable_fingerprint_contributes_no_verdict() -> None:
    """A refutation addressed to a fingerprint we did not ask about must not land on ANY
    finding — passing it through could demote a different one."""
    sub_results, _ = _panel_with_verifier(_verdict_raw("some-other-finding", "refuted"))
    assert sub_results["verdicts"] == []
    assert [n["notice"] for n in sub_results["notices"]] == ["verifier_inconclusive"]
    assert synthesize(sub_results)[0]["approved"] is False


def test_a_refutation_the_verifier_earned_does_demote_the_finding() -> None:
    """The other direction, so the failure-direction tests aren't vacuously green: a matched
    ``refuted`` verdict reaches the fold and lands in non_blocking."""
    finding = _finding("the bug")
    plan = _plan("find:code")
    transport = FakeTransport({
        "find:code": _findings_raw(finding),
        "verify:1": _verdict_raw(issue_fingerprint(finding), "refuted", "unreachable path"),
    })

    result = run_review_panel(_work(plan), transport)

    review, _, _ = synthesize(result.sub_results)
    assert review["approved"] is True and review["issues"] == []
    assert review["non_blocking"][0]["title"].startswith("refuted:")
    assert review["non_blocking"][0]["disposition"] == "file"


# --- failure direction: a finder failing kills the whole dispatch ------------------------

def _panel_with_failing_finder(raw: RawResult):
    plan = _plan("find:code", "find:spec", "find:tests")
    transport = FakeTransport({"find:code": _findings_raw(), "find:spec": raw},
                              default=_findings_raw())
    return run_review_panel(_work(plan), transport), transport


def test_a_finder_failing_terminally_fails_the_whole_dispatch() -> None:
    """Design test (g). No partial-panel verdicts: a missing lens is a missing review. One
    attempt is consumed and the engine's retry re-dispatches the FULL plan."""
    result, transport = _panel_with_failing_finder(
        RawResult(None, exit_code=1, error="claude exploded")
    )
    assert result.status is ResultStatus.FAILURE
    assert result.sub_results is None  # nothing partial reaches the fold
    assert transport.phases == ["find:code", "find:spec"]  # short-circuited, no find:tests
    assert "find:spec" in result.error and "claude exploded" in result.error
    assert [c.phase for c in result.sub_calls or ()] == ["find:code", "find:spec"]


def test_a_finders_schema_violation_fails_the_dispatch_as_a_schema_violation() -> None:
    result, _ = _panel_with_failing_finder(RawResult(None, raw_output="prose", schema_retries=2))
    assert result.status is ResultStatus.SCHEMA_VIOLATION
    assert result.schema_retries == 2  # the exhausted loop's spend rides the dispatch


def test_a_finder_timeout_and_rate_limit_classify_like_a_single_dispatch() -> None:
    timed_out, _ = _panel_with_failing_finder(
        RawResult(None, exit_code=124, error="timed out after 600s")
    )
    assert timed_out.status is ResultStatus.TIMEOUT
    limited, _ = _panel_with_failing_finder(
        RawResult(None, exit_code=1, error="429 rate limit exceeded")
    )
    assert limited.status is ResultStatus.RATE_LIMITED  # engine re-dispatches cheaper


# --- the assembled StageResult -----------------------------------------------------------

def test_the_dispatch_result_sums_usage_and_retries_and_threads_no_session() -> None:
    from orchestrator.schemas.work import TokenUsage

    plan = _plan("find:code", "find:spec")
    transport = FakeTransport({
        "find:code": RawResult({"findings": []}, TokenUsage(input=10, output=2),
                               session_ref="sess-a", schema_retries=1),
        "find:spec": RawResult({"findings": []}, TokenUsage(input=5, output=3, cache_read=7),
                               session_ref="sess-b", schema_retries=2),
    })

    result = run_review_panel(_work(plan), transport)

    assert result.token_usage.input == 15 and result.token_usage.output == 5
    assert result.token_usage.cache_read == 7
    assert result.schema_retries == 3
    # A finder's session must never become the task's next-stage session.
    assert result.session_ref is None
    assert [c.session_id for c in result.sub_calls or ()] == ["sess-a", "sess-b"]
    assert [c.schema_retries for c in result.sub_calls or ()] == [1, 2]


def test_the_runner_takes_the_panel_path_only_for_a_plan_bearing_item() -> None:
    plan = _plan("find:code")
    transport = FakeTransport({}, default=_findings_raw())
    runner = HeadlessClaudeRunner(transport)

    panel = runner.dispatch(_work(plan))
    assert panel.sub_calls is not None and panel.sub_results is not None

    plain = runner.dispatch(_work(plan).model_copy(update={"plan": None, "phase": None}))
    assert plain.sub_calls is None and plain.sub_results is None
    assert plain.structured_output == {"findings": []}  # the single-call path, untouched
    assert transport.seen[-1].phase is None


# --- attribution: a sub-call's own schema retries reach its own ledger row ----------------

def test_a_finders_schema_retries_ride_that_finders_ledger_row(tmp_path, monkeypatch) -> None:
    """#255 added ``SubCall.schema_retries``; this transport must POPULATE it. Asserted
    through the REAL ledger path over a REAL ``_schema_retry_loop`` — a hand-built SubCall
    would prove nothing about the wiring, and left at its 0 default every row would silently
    report zero, reopening the #70 under-attribution hole one level down."""
    findings_schema = {
        "type": "object", "required": ["findings"],
        "properties": {"findings": {"type": "array"}},
    }
    verdict_schema = {"type": "object", "required": ["fingerprint"],
                      "properties": {"fingerprint": {"type": "string"}}}
    fp = issue_fingerprint(_finding("real bug"))
    # find:code answers malformed twice before complying; find:spec is valid first try.
    scripted = [
        {"structured_output": {"nope": 1}, "session_id": "s1"},
        {"structured_output": {"nope": 1}, "session_id": "s1"},
        {"structured_output": {"findings": [_finding("real bug")]}, "session_id": "s1"},
        {"structured_output": {"findings": []}, "session_id": "s2"},
        {"structured_output": {"fingerprint": fp, "verdict": "confirmed", "reasoning": "r"}},
    ]
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        payload = scripted[min(len(calls) - 1, len(scripted) - 1)]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    schemas = {"review_findings": findings_schema, "review_verdict": verdict_schema}
    transport = claude_cli_transport(lambda ref: json.dumps(schemas[ref]))

    result = run_review_panel(_work(_plan("find:code", "find:spec")), transport)

    by_phase = {c.phase: c for c in result.sub_calls or ()}
    assert by_phase["find:code"].schema_retries == 2  # its OWN corrective turns…
    assert by_phase["find:spec"].schema_retries == 0  # …never a sibling's
    assert by_phase["verify:1"].schema_retries == 0

    rows = CostLedger(tmp_path / "stage-costs.jsonl").record_rows(result)
    assert {r["phase"]: r["schema_retries"] for r in rows} == {
        "find:code": 2, "find:spec": 0, "verify:1": 0,
    }
    # …and the ledger really wrote them (one row per model call, no aggregate).
    written = [json.loads(ln) for ln in
               (tmp_path / "stage-costs.jsonl").read_text().splitlines()]
    assert len(written) == 3 and {r["work_item_id"] for r in written} == {"wi-1"}


# --- end to end: dispatch -> sub_results -> engine fold -> canonical review.json ----------

def test_end_to_end_plan_bearing_dispatch_folds_into_canonical_review_json(
    tmp_path, project
) -> None:
    """Design acceptance: the panel's raw output crosses the seam once, and what the task
    records is the deterministic fold's canonical ``review.json`` — no synthesizer model."""
    eng = Engine(
        StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project,
        registry=build_registry(include_interactive=False),
        router=Router(execution_mode=ExecutionMode.HEADLESS,
                      orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1", review_workflow=True)
    eng.add_task("r1", "t1")

    from tests.conftest import make_result

    work = None
    while (w := eng.next_work("r1", "t1")) is not None:
        if w.stage is Stage.REVIEW:
            work = w
            break
        eng.record("r1", make_result(w))
    assert work is not None and work.plan is not None

    finding = _finding("unhandled None from the loader", file="orchestrator/engine.py")
    nit = _finding("rename this variable", severity="suggestion")
    transport = FakeTransport(
        {
            "find:code": _findings_raw(finding),
            "find:spec": _findings_raw(nit, improvement={"title": "add a fixture"}),
            "find:tests": _findings_raw(tests_meaningful=True),
        },
        default=_verdict_raw(issue_fingerprint(finding), "confirmed", "line 12 really is None"),
    )
    result = HeadlessClaudeRunner(transport).dispatch(work)
    eng.record("r1", result)

    # What the engine RECORDED is the fold's canonical review.json — never a runner verdict.
    logs = sorted((tmp_path / "stages" / "t1").glob("*-review.json"))
    stage_log = json.loads(logs[-1].read_text())
    review = stage_log["structured_output"]
    assert review["approved"] is False
    assert review["issues"] == [finding]  # the confirmed blocking finding, unchanged
    assert review["non_blocking"][0]["title"] == "rename this variable"
    assert review["tests_meaningful"] is True
    assert review["improvement"] == {"title": "add a fixture"}
    # …and the raw panel evidence is retained beside it.
    assert stage_log["sub_results"]["findings_by_lens"]["find:code"]["findings"] == [finding]

    # The verdict is the unchanged downstream reading a normal review.json: it rejects and
    # cascades a fix cycle, exactly as a single reviewer's rejection would.
    events = eng.store.read_events("r1")
    verdict = next(e for e in events if e["type"] == "review_verdict")
    assert verdict["kind"] == "rejected"
    assert any("unhandled None from the loader" in issue for issue in verdict["issues"])
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.IMPLEMENT  # the fix cycle

    # One ledger row per model call (3 finders + 1 verifier), no aggregate double-count.
    rows = [json.loads(ln) for ln in
            (tmp_path / "stage-costs.jsonl").read_text().splitlines()]
    review_rows = [r for r in rows if r["stage"] == "review"]
    assert [r["phase"] for r in review_rows] == [
        "find:code", "find:spec", "find:tests", "verify:1",
    ]
