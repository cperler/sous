"""Multi-agent REVIEW execution below the seam: find → dedupe → adversarial verify (#73
design §2, part 5/5).

A plan-bearing ``WorkItem`` (``work.plan``) is ONE dispatch that fans out into several model
sub-calls inside the runner: one blind finder per lens, then one adversarial verifier per
deduped blocking finding. It returns ONE ``StageResult`` carrying the raw, unfolded panel
output in ``sub_results`` and one ``SubCall`` per model call in ``sub_calls`` — the engine's
deterministic fold (``orchestrator.review_workflow.synthesize``) owns ``output``.

The headless×claude transport is the reference implementation, but nothing here is
claude-specific: the driver only needs a ``Transport`` (``WorkItem -> RawResult``), so the
codex transport or a fake in a test drives it identically, and the ``workflow_shim.js``
interactive twin (a follow-on batch) implements the SAME contract in JS.

**Build-time decisions this module makes** (design open question 1 + the unbounded-verifier
gap the issue asked to close):

* **Verify blocking-severity findings only.** ``suggestion`` findings cannot block (the
  severity gate already auto-approves an all-suggestion review), so a verifier call on one
  buys nothing; an absent or unrecognized severity IS verified, matching the fold's blocking
  default. Verifier calls are the cost driver, and this is where the money is.
* **A verifier ceiling (``_MAX_VERIFIERS``).** The design caps finders at ≤4 by construction
  but leaves verifiers linear in findings — a 30-finding review would fire 30 calls. The
  queue is ordered by (severity rank, fingerprint) so the cap is deterministic, and the
  findings past it are never silently dropped: they keep NO verdict, which leaves them
  BLOCKING, and a notice rides ``sub_results["notices"]`` into the stage log.

**Failure direction, everywhere: toward scrutiny.** A verifier that errors, times out, or
echoes an unmatchable fingerprint contributes NO verdict, so the fold leaves its finding
blocking — verification may only remove scrutiny it has affirmatively earned. Its
``SubCall`` is still emitted, so the spend stays attributed. A FINDER that fails terminally
(after its own schema-retry loop) is different: it short-circuits the whole panel and fails
the dispatch with that call's classified status, because a missing lens is a missing review,
not a lenient one. The engine's existing retry machinery re-dispatches the full plan.

**The runner-notice contract** (#268, declared engine-side in
``review_workflow.RUNNER_NOTICE_EXTRAS``). Everything this runner had to cap, drop, or give
up on rides ``sub_results["notices"]`` as ``{"notice": kind, "detail": prose}`` plus the
NUMERIC extras declared for that kind — today ``verifier_cap`` carries ``count`` (how many
blocking findings went unverified). ``detail`` is for humans only: the fold reads the
declared extras and never parses prose, so a wording change here can never silently change
a number engine-side. The engine normalizes this list, folds it into ``panel_summary``, and
emits one warning-grade ``review_panel_notice`` event per entry — so "this review only
verified 8 of 12 blocking findings" is visible from ``status`` without opening a stage log.
A kind the engine's table does not know is still carried through, so a NEW notice kind
here reaches the event stream before the engine learns to tally it.
"""

from __future__ import annotations

import re
import time

from orchestrator.review_workflow import FINGERPRINT_RULE, LENS_ORDER, issue_fingerprint
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import (
    FinderSpec,
    StageResult,
    SubCall,
    TokenUsage,
    WorkItem,
)

from .transport import RawResult, Transport, classify_raw, to_stage_result

# How many verifier sub-calls one dispatch may fire. Chosen at 8 because a single SURVIVING
# blocking finding already rejects the review: verification past a handful only edits the
# rejection list, it cannot change the verdict, so the marginal call buys nothing while the
# cost is linear. Findings past the cap keep no verdict and therefore stay blocking (the safe
# direction), and the drop is noticed, never silent.
_MAX_VERIFIERS = 8

# Severity ranking for the verifier queue — same order as the fold's, so the runner verifies
# the findings the engine will rank first. An absent/unrecognized severity is blocking (and
# sorts with `important`, the fold's default).
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "important": 1}
_SUGGESTION = "suggestion"
_DEFAULT_RANK = 1

_MAX_NOTICE_DETAIL = 200  # bounded like the fold's notices


def run_review_panel(work: WorkItem, transport: Transport) -> StageResult:
    """Execute ``work.plan`` as a panel of sub-calls and return ONE StageResult.

    Sequential by design — concurrency is a runner freedom, not a contract, and the sub-calls
    share one worktree and one per-task port block, so parallelism here is a real hazard for
    at most a few minutes of wall time (filed as deferred scope).
    """
    plan = work.plan
    if plan is None:  # pragma: no cover - the caller gates on `work.plan is not None`
        raise ValueError("run_review_panel requires a plan-bearing WorkItem")

    notices: list[dict[str, object]] = []
    sub_calls: list[SubCall] = []
    findings_by_lens: dict[str, dict] = {}

    for finder in plan.finders:
        raw, call = _dispatch_sub(work, transport, phase=finder.lens, prompt=finder.prompt,
                                  schema_ref=finder.schema_ref, agent=finder.agent)
        sub_calls.append(call)
        status = classify_raw(raw)
        if status is not ResultStatus.SUCCESS:
            # No partial-panel verdicts: a missing lens fails the dispatch, one attempt is
            # consumed, and the engine's retry re-dispatches the WHOLE plan.
            return _panel_failure(work, raw, status, finder, tuple(sub_calls))
        findings_by_lens[finder.lens] = raw.structured_output or {}

    queue, dedupe_notices = _verify_queue(findings_by_lens, plan.dedupe_rule)
    notices.extend(dedupe_notices)
    if len(queue) > _MAX_VERIFIERS:
        dropped = queue[_MAX_VERIFIERS:]
        notices.append(_notice(
            "verifier_cap",
            f"{len(queue)} blocking findings exceed the {_MAX_VERIFIERS}-verifier cap — "
            f"{len(dropped)} unverified (they stay BLOCKING): "
            + ", ".join(fp for fp, _ in dropped),
            count=len(dropped),
        ))
        queue = queue[:_MAX_VERIFIERS]

    verdicts: list[dict] = []
    for index, (fingerprint, finding) in enumerate(queue, start=1):
        phase = f"verify:{index}"
        raw, call = _dispatch_sub(
            work, transport, phase=phase,
            prompt=_verify_prompt(plan.verify_template, fingerprint, finding),
            schema_ref=plan.verify_schema_ref, agent=None,
        )
        sub_calls.append(call)
        verdict, problem = _verdict_of(raw, fingerprint)
        if verdict is None:
            # Errored / timed out / unmatchable fingerprint => NO verdict, so the fold leaves
            # this finding blocking. The spend is still attributed (the SubCall above).
            notices.append(_notice(
                "verifier_inconclusive", f"{phase} on {fingerprint}: {problem} — stays blocking"
            ))
            continue
        verdicts.append(verdict)

    sub_results: dict = {
        "findings_by_lens": findings_by_lens,
        "verdicts": verdicts,
        "notices": notices,
    }
    return to_stage_result(
        work,
        _panel_raw(sub_calls, findings_by_lens, verdicts),
        ResultStatus.SUCCESS,
        mode=ExecutionMode.HEADLESS,
        provider=Provider.CLAUDE,
        sub_results=sub_results,
        sub_calls=tuple(sub_calls),
    )


# --- sub-call dispatch -------------------------------------------------------------------


def _sub_item(
    work: WorkItem, *, phase: str, prompt: str, schema_ref: str, agent: str | None
) -> WorkItem:
    """One sub-call's WorkItem, derived from the dispatch's own.

    Hygiene is load-bearing, not tidiness:

    * ``session_ref`` is STRIPPED — resuming the stage's session would let every finder read
      the previous finders' turns, and finder blindness is the whole point of the panel.
    * ``plan`` is None so a sub-item can never re-enter the panel driver (no recursion).
    * ``checkpoint_tag``/``reset_to``/``salvage_anchor`` are stripped so the checkpoint
      wrapper cannot tag, reset, or salvage a worktree once per sub-call.
    * ``phase`` names this sub-call's stream file.

    ``id``/``content_hash`` are deliberately inherited: they identify the DISPATCH (which is
    what the engine leases and what the ledger rows share), and nothing below the seam reads
    them. A sub-item never crosses the seam."""
    return work.model_copy(update={
        "prompt": prompt,
        "schema_ref": schema_ref,
        "agent": agent,
        "phase": phase,
        "plan": None,
        "session_ref": None,
        "checkpoint_tag": None,
        "reset_to": None,
        "salvage_anchor": None,
    })


def _dispatch_sub(
    work: WorkItem, transport: Transport, *,
    phase: str, prompt: str, schema_ref: str, agent: str | None,
) -> tuple[RawResult, SubCall]:
    """Run one sub-call and return its raw result plus the ``SubCall`` attributing it.

    ``schema_retries`` is read off THIS call's own RawResult — the transport's
    ``_schema_retry_loop`` runs inside the sub-call, so its corrective turns belong to this
    row and to no other (design §4). Left at the default it would silently re-open the #70
    under-attribution hole one level down."""
    sub = _sub_item(work, phase=phase, prompt=prompt, schema_ref=schema_ref, agent=agent)
    started = time.monotonic()
    raw = transport(sub)
    duration = time.monotonic() - started
    return raw, SubCall(
        phase=phase,
        model=sub.model,
        usage=raw.usage,
        duration_s=round(duration, 3),
        session_id=raw.session_ref,
        stream_file=(raw.stream_files or {}).get("stream"),
        schema_retries=raw.schema_retries,
    )


# --- dedupe + the verifier queue ---------------------------------------------------------


def _lens_walk(findings_by_lens: dict[str, dict]) -> list[str]:
    """The lens order the fold itself walks: the known lenses in ``LENS_ORDER``, then any
    unknown lens sorted by name. Shared with the fold so first-occurrence-wins dedupe picks
    the SAME representative finding on both sides of the seam."""
    unknown = sorted(k for k in findings_by_lens if k not in LENS_ORDER)
    return [lens for lens in LENS_ORDER if lens in findings_by_lens] + unknown


def _findings_of(payload: dict) -> list[object]:
    raw = payload.get("findings")
    return list(raw) if isinstance(raw, list) else []


def _severity_rank(finding: object) -> int:
    if not isinstance(finding, dict):
        return _DEFAULT_RANK
    raw = finding.get("severity")
    value = str(raw).strip().lower() if raw is not None else ""
    return _SEVERITY_RANK.get(value, _DEFAULT_RANK)


def _is_blocking(finding: object) -> bool:
    """Everything except an EXPLICIT ``suggestion`` gets verified — the same direction the
    fold defaults an absent/unrecognized severity (blocking)."""
    if not isinstance(finding, dict):
        return True
    raw = finding.get("severity")
    return str(raw).strip().lower() != _SUGGESTION if raw is not None else True


def _verify_queue(
    findings_by_lens: dict[str, dict], dedupe_rule: str
) -> tuple[list[tuple[str, object]], list[dict[str, object]]]:
    """The deduped, ordered ``(fingerprint, finding)`` queue the verifiers work through.

    Dedupe honours the plan's ``dedupe_rule``: ``fingerprint-v1`` is
    ``review_workflow.issue_fingerprint`` — the ONE implementation, shared with the fold, so
    a second normalization can never drift from it. Any other rule means NO dedupe (verify
    everything: duplicate verification is waste, not a correctness loss) plus a notice, so a
    lane/engine rule mismatch is visible instead of silently collapsing the wrong findings.

    Ordering is (severity rank, fingerprint) — deterministic, so the verifier cap always bites
    the same findings for the same panel output."""
    notices: list[dict[str, object]] = []
    dedupe = dedupe_rule == FINGERPRINT_RULE
    if not dedupe:
        notices.append(_notice(
            "unknown_dedupe_rule",
            f"plan dedupe_rule {dedupe_rule!r} is not {FINGERPRINT_RULE!r} — findings were "
            "NOT deduped before verification (the engine still dedupes at synthesis)",
        ))
    seen: set[str] = set()
    queue: list[tuple[int, str, object]] = []
    for lens in _lens_walk(findings_by_lens):
        for finding in _findings_of(findings_by_lens[lens]):
            fingerprint = issue_fingerprint(finding)
            if dedupe:
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
            if _is_blocking(finding):
                queue.append((_severity_rank(finding), fingerprint, finding))
    queue.sort(key=lambda row: (row[0], row[1]))
    return [(fingerprint, finding) for _, fingerprint, finding in queue], notices


# --- the verifier sub-call ---------------------------------------------------------------


def _verify_prompt(template: str, fingerprint: str, finding: object) -> str:
    """Fill the ENGINE-AUTHORED ``verify_template``'s mechanical slots for one finding.

    Mechanical substitution only — never authorship (the ``_corrective_prompt`` precedent):
    the runner renders the finding it was handed and the file:line it already carries, and
    writes not one word of instruction. Not ``str.format``, so a finding containing braces
    cannot break the template.

    ONE pass, not chained ``str.replace``s: chaining lets a LATER placeholder's substitution
    re-scan text an EARLIER one just injected, so a finding whose description contains the
    literal ``{diff_hint}`` would have it silently rewritten (found by the #73 panel
    reviewing this very module — a finder discussing these placeholders writes them
    verbatim, so the trigger is on-path, not hypothetical). A single ``re.sub`` walks the
    template once and never revisits what it emitted, which is what makes the finding text
    survive VERBATIM in both directions rather than only for non-placeholder braces."""
    values = {"finding": _finding_block(fingerprint, finding), "diff_hint": _diff_hint(finding)}
    return _SLOT_RE.sub(lambda m: values[m.group(1)], template)


# Only the slots this runner fills. An unknown ``{...}`` in the template is left ALONE rather
# than KeyError-ing: the template is engine-authored, and a prompt with one stray brace beats
# a dispatch that dies at render time.
_SLOT_RE = re.compile(r"\{(finding|diff_hint)\}")


def _finding_block(fingerprint: str, finding: object) -> str:
    if not isinstance(finding, dict):
        return f"- fingerprint: {fingerprint}\n- description: {finding}"
    lines = [f"- fingerprint: {fingerprint}"]
    for key in ("severity", "file", "line", "description", "suggested_fix"):
        value = finding.get(key)
        if value is not None and str(value).strip():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _diff_hint(finding: object) -> str:
    """Where the verifier should look. The runner can only fill this MECHANICALLY from what
    the finding already names (a model-lane WorkItem carries no structural ``context``), so a
    finding without a file gets an honest "look at the change" pointer rather than an
    invented one."""
    if isinstance(finding, dict):
        file = str(finding.get("file") or "").strip()
        line = finding.get("line")
        if file and isinstance(line, int):
            return f"{file}:{line}"
        if file:
            return file
    return "the change under review in this working tree"


def _verdict_of(raw: RawResult, fingerprint: str) -> tuple[dict | None, str]:
    """``(verdict, problem)`` for one verifier sub-call: the verdict object to hand the fold,
    or ``(None, why)`` when this verifier earned nothing.

    Rejected: a failed/timed-out/schema-violating call, and — critically — a verdict whose
    echoed fingerprint is not the one this verifier was asked about. Passing a mismatched
    fingerprint through would let a refutation land on a DIFFERENT finding; dropping it leaves
    both blocking, which is the only safe direction."""
    status = classify_raw(raw)
    if status is not ResultStatus.SUCCESS:
        return None, f"verifier {status.value}: {(raw.error or 'no structured output')[:120]}"
    out = raw.structured_output or {}
    echoed = out.get("fingerprint")
    if not isinstance(echoed, str) or issue_fingerprint(echoed) != fingerprint:
        return None, f"verifier echoed an unmatchable fingerprint {str(echoed)[:80]!r}"
    return {
        "fingerprint": fingerprint,
        "verdict": out.get("verdict"),
        "reasoning": out.get("reasoning"),
    }, ""


# --- assembling the single StageResult ----------------------------------------------------


def _panel_raw(
    sub_calls: list[SubCall], findings_by_lens: dict[str, dict], verdicts: list[dict]
) -> RawResult:
    """The dispatch-level RawResult a completed panel reports.

    ``structured_output`` is None ON PURPOSE: for a plan-bearing review the engine's fold owns
    ``output``, and a runner-authored verdict is exactly the synthesizer-model hole the design
    closes. ``session_ref`` is None so no finder's session is ever threaded into the next
    stage. Usage and schema_retries are summed across the sub-calls for the dispatch-level
    view (the ledger still prices each sub-call's own row)."""
    return RawResult(
        None,
        usage=_sum_usage(sub_calls),
        raw_output=(
            f"[review panel] {len(findings_by_lens)} finder(s), "
            f"{sum(1 for c in sub_calls if c.phase.startswith('verify:'))} verifier(s); "
            f"{sum(len(_findings_of(p)) for p in findings_by_lens.values())} finding(s), "
            f"{len(verdicts)} verdict(s). The engine's fold owns review.json."
        ),
        exit_code=0,
        invocation=f"review panel ({len(sub_calls)} sub-calls)",
        stream_files=_panel_stream_files(sub_calls),
        schema_retries=sum(call.schema_retries for call in sub_calls),
    )


def _panel_failure(
    work: WorkItem, raw: RawResult, status: ResultStatus,
    finder: FinderSpec, sub_calls: tuple[SubCall, ...],
) -> StageResult:
    """The StageResult for a panel whose FINDER failed terminally: the failing sub-call's own
    error/raw_output/stream files (the evidence a post-mortem needs) plus every SubCall so far
    (the spend is attributed even though the dispatch failed). ``sub_results`` is deliberately
    None — a partial panel has no verdict to fold, and the fold must never see one."""
    files = dict(raw.stream_files) if raw.stream_files else {}
    files["sub_calls"] = _sub_call_files(sub_calls)
    failed = RawResult(
        None,
        usage=_sum_usage(list(sub_calls)),
        raw_output=raw.raw_output,
        exit_code=raw.exit_code,
        error=f"review panel finder {finder.lens} failed: {raw.error or 'no structured output'}"[
            :500
        ],
        invocation=raw.invocation or f"review panel finder {finder.lens}",
        raw_stderr=raw.raw_stderr,
        stream_files=files,
        schema_retries=sum(call.schema_retries for call in sub_calls),
    )
    return to_stage_result(
        work, failed, status,
        mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE, sub_calls=sub_calls,
    )


def _sub_call_files(sub_calls: tuple[SubCall, ...] | list[SubCall]) -> list[dict]:
    return [
        {"phase": call.phase, "stream": call.stream_file}
        for call in sub_calls if call.stream_file
    ]


def _panel_stream_files(sub_calls: list[SubCall]) -> dict | None:
    """``{"stream": <first sub-call's stream>, "sub_calls": [{phase, stream}, ...]}`` — the
    top-level ``stream`` keeps the pre-#73 shape every existing reader (the renderer, the
    tail CLI) expects, and the per-sub-call list is what a post-mortem of a panel actually
    needs. None when nothing was teed (a transport with no run dir)."""
    files = _sub_call_files(sub_calls)
    if not files:
        return None
    return {"stream": files[0]["stream"], "sub_calls": files}


def _sum_usage(sub_calls: list[SubCall]) -> TokenUsage:
    return TokenUsage(
        input=sum(c.usage.input for c in sub_calls),
        output=sum(c.usage.output for c in sub_calls),
        cache_read=sum(c.usage.cache_read for c in sub_calls),
        cache_write=sum(c.usage.cache_write for c in sub_calls),
    )


def _notice(kind: str, detail: str, **extras: int) -> dict[str, object]:
    """One panel notice — what the RUNNER dropped or refused to swallow silently, carried on
    ``sub_results["notices"]`` into the stage log and (since #268) into the event stream.

    ``detail`` is prose FOR HUMANS. Anything a consumer must compute on rides as a declared
    numeric ``extra`` instead: the engine-side contract
    (``review_workflow.RUNNER_NOTICE_EXTRAS``) reads only declared keys and must never regex
    a detail string to recover a number. Adding an extra therefore means declaring it on
    both sides of the seam — today only ``verifier_cap.count`` (findings left unverified
    past ``_MAX_VERIFIERS``)."""
    return {
        "notice": kind,
        "detail": detail if len(detail) <= _MAX_NOTICE_DETAIL
        else detail[:_MAX_NOTICE_DETAIL] + " … [truncated]",
        **extras,
    }
