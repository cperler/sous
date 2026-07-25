"""The typed-effort convention on the FROZEN work-plane models (#161/#202).

WorkItem/StageResult are ``frozen`` (not ``validate_assignment`` like the status
models — see tests/test_status_assignment.py), so their guarantee lands at CONSTRUCTION
time instead of at assignment. #161/#202 tightened their ``effort`` field from
``str | None`` to ``Effort | None`` and their ``model`` field to the OPEN ``ModelId``
newtype. These tests pin the three guarantees that keep the migration safe:

1. constructing with a bare ``effort`` string coerces to the Effort member,
2. an invalid ``effort`` string raises ValidationError AT construction (not later),
3. stored JSON AND compute_content_hash are byte-identical to the pre-migration shape
   (StrEnum serializes as its value; ModelId is a str at runtime — no SCHEMA_VERSION bump).
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from orchestrator.schemas.enums import (
    Effort,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import (
    FinderSpec,
    LanePolicy,
    LaneUsed,
    ReviewPlan,
    StageResult,
    SubCall,
    TokenUsage,
    WorkItem,
    compute_content_hash,
)

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _plan(*lenses: str) -> ReviewPlan:
    return ReviewPlan(
        finders=tuple(
            FinderSpec(lens=lens, prompt=f"lens {lens}", agent=None, schema_ref="review_findings")
            for lens in lenses
        ),
        verify_template="refute: {finding}",
        verify_schema_ref="review_verdict",
        dedupe_rule="fingerprint-v1",
    )


def _work(effort: str | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="claude-opus-4-8", created_at="now",
        lane_policy=H, effort=effort,
    )


def _result(effort: str | None = None) -> StageResult:
    return StageResult(
        work_item_id="wi-1", content_hash="h", run_id="r1", task_id="t1",
        stage=Stage.IMPLEMENT, model="claude-opus-4-8", effort=effort,
        status=ResultStatus.SUCCESS,
        lane_used=LaneUsed(
            execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE,
            invocation="agent(model=claude-opus-4-8)",
        ),
        completed_at="now",
    )


# --- 1. a bare effort string coerces to the enum at construction ----------------

def test_workitem_bare_effort_string_coerces_to_enum() -> None:
    w = _work(effort="high")
    assert w.effort is Effort.HIGH  # the member, not the bare string
    assert _work().effort is None  # effort-less dispatch stays constructible


def test_stageresult_bare_effort_string_coerces_to_enum() -> None:
    r = _result(effort="low")
    assert r.effort is Effort.LOW
    assert _result().effort is None


# --- 2. an invalid effort string raises at construction --------------------------

def test_invalid_effort_string_raises_at_construction() -> None:
    with pytest.raises(ValidationError):
        _work(effort="turbo")
    with pytest.raises(ValidationError):
        _result(effort="xhigh")


def test_model_id_stays_open_so_a_retired_id_still_loads() -> None:
    """ModelId is an OPEN newtype, NOT a closed enum: an id the roster has since retired
    must still construct/load (the backward-compat regression #161 kept open)."""
    w = WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="p",
        schema_ref="implement", model="claude-legacy-since-retired", created_at="now",
        lane_policy=H,
    )
    assert w.model == "claude-legacy-since-retired"
    assert WorkItem.model_validate(w.model_dump(mode="json")).model == w.model


# --- 3. stored JSON and content_hash are byte-identical to the pre-migration shape

def test_workitem_json_round_trips_with_string_values() -> None:
    w = _work(effort="high")
    dumped = w.model_dump(mode="json")
    assert dumped["effort"] == "high"  # the raw string, not an enum repr
    assert isinstance(dumped["effort"], str)
    assert dumped["model"] == "claude-opus-4-8"
    assert isinstance(dumped["model"], str)
    # Full fidelity: load(dump) re-dumps to the identical document.
    assert WorkItem.model_validate(dumped).model_dump(mode="json") == dumped
    assert WorkItem.model_validate(dumped).effort is Effort.HIGH  # load coerces back


def test_stageresult_json_round_trips_with_string_values() -> None:
    r = _result(effort="medium")
    dumped = r.model_dump(mode="json")
    assert dumped["effort"] == "medium"
    assert isinstance(dumped["effort"], str)
    assert dumped["model"] == "claude-opus-4-8"
    assert WorkItem.model_validate(_work().model_dump(mode="json")).effort is None
    assert StageResult.model_validate(dumped).model_dump(mode="json") == dumped
    assert StageResult.model_validate(dumped).effort is Effort.MEDIUM


def test_content_hash_is_byte_identical_to_the_pre_migration_shape() -> None:
    """The Effort/ModelId retype must not perturb a dispatch's identity key: the hash of
    a bare-string call equals the hash computed from the raw pre-migration string parts."""
    legacy_blob = "\x1f".join(
        ["implement", "p", "implement", "claude-opus-4-8", "headless:claude", "0", "high"]
    )
    legacy = hashlib.sha256(legacy_blob.encode("utf-8")).hexdigest()
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
        model="claude-opus-4-8", lane_policy=H, attempt=0, effort="high",
    ) == legacy
    # And the Effort-member call hashes identically to the bare-string call.
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
        model="claude-opus-4-8", lane_policy=H, attempt=0, effort=Effort.HIGH,
    ) == legacy


# --- #73: plan is CONTENT (folded into the hash), byte-identical when plan-less ----

def test_plan_less_workitem_is_byte_identical_to_pre_change() -> None:
    """A dispatch with no plan attached hashes AND serializes byte-identically to the
    pre-#73 shape: plan defaults to None and contributes nothing to content_hash (NOT via
    an exclusion — the append is guarded on `plan is not None`, like `effort`), and its
    None field round-trips cleanly. This is the plan-less-path-stays-byte-identical guard."""
    # The hash equals the pre-#73 formula (no plan part in the blob).
    legacy_blob = "\x1f".join(["implement", "do it", "implement", "claude-opus-4-8",
                               "headless:claude", "0"])
    legacy = hashlib.sha256(legacy_blob.encode("utf-8")).hexdigest()
    w = _work()
    assert w.plan is None
    assert w.content_hash == legacy
    # compute_content_hash with plan=None equals the no-plan-arg call.
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="do it", schema_ref="implement",
        model="claude-opus-4-8", lane_policy=H, attempt=0, plan=None,
    ) == legacy
    # JSON round-trip is loss-free with plan=None present.
    dumped = w.model_dump(mode="json")
    assert dumped["plan"] is None
    assert WorkItem.model_validate(dumped).model_dump(mode="json") == dumped


def test_two_finder_sets_yield_different_content_hashes() -> None:
    """The load-bearing #254 guarantee: two ReviewPlans with different finder sets are
    DIFFERENT work, so they hash differently. A plan-bearing hash also differs from the
    plan-less one (the plan part is genuinely folded in)."""
    base = dict(stage=Stage.REVIEW, prompt="p", schema_ref="review",
                model="claude-opus-4-8", lane_policy=H, attempt=0)
    h_none = compute_content_hash(**base)
    h_code = compute_content_hash(**base, plan=_plan("find:code"))
    h_code_spec = compute_content_hash(**base, plan=_plan("find:code", "find:spec"))
    assert h_none != h_code  # a plan changes identity
    assert h_code != h_code_spec  # a different finder SET changes identity
    # Same finder set -> same hash (identity is a pure function of the plan content).
    assert h_code == compute_content_hash(**base, plan=_plan("find:code"))


def test_workitem_with_plan_json_round_trips() -> None:
    """A plan-bearing WorkItem serializes and reloads with full fidelity, and create()
    derives content_hash from the plan (so the stored hash matches a re-computation)."""
    plan = _plan("find:code", "find:tests")
    w = WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.REVIEW, prompt="p",
        schema_ref="review", model="claude-opus-4-8", created_at="now",
        lane_policy=H, plan=plan,
    )
    assert w.plan == plan
    assert w.content_hash == compute_content_hash(
        stage=Stage.REVIEW, prompt="p", schema_ref="review", model="claude-opus-4-8",
        lane_policy=H, attempt=0, plan=plan,
    )
    dumped = w.model_dump(mode="json")
    assert WorkItem.model_validate(dumped).model_dump(mode="json") == dumped
    assert WorkItem.model_validate(dumped).plan == plan


def test_stageresult_with_sub_results_and_sub_calls_round_trips() -> None:
    """A plan-bearing dispatch's StageResult carries raw sub_results + per-sub-call
    SubCalls with full JSON fidelity; both default to None on an ordinary result."""
    assert _result().sub_results is None
    assert _result().sub_calls is None
    r = _result().model_copy(update={
        "sub_results": {"findings_by_lens": {"find:code": []}, "verdicts": []},
        "sub_calls": (
            SubCall(phase="find:code", model="claude-opus-4-8",
                    usage=TokenUsage(input=10, output=5), duration_s=1.5,
                    session_id="s1", stream_file="stages/t/review-attempt0.find:code.stream.jsonl"),
        ),
    })
    dumped = r.model_dump(mode="json")
    assert StageResult.model_validate(dumped).model_dump(mode="json") == dumped
    assert StageResult.model_validate(dumped).sub_calls[0].phase == "find:code"


def test_review_findings_fingerprint_matches_review_issue() -> None:
    """A review_findings finding reuses review.json's issue-object vocabulary exactly, so
    Engine._issue_fingerprint over it equals the fingerprint of the equivalent review.json
    issue object (the invariant that lets the synthesis fold re-dedupe unchanged, #73)."""
    from orchestrator.engine import Engine

    review_issue = {
        "severity": "important",
        "file": "orchestrator/engine.py",
        "line": 42,
        "description": "off-by-one in the retry ceiling",
        "suggested_fix": "use <= not <",
    }
    findings_finding = dict(review_issue)  # same vocabulary; a finder emits this shape
    assert Engine._issue_fingerprint(findings_finding) == Engine._issue_fingerprint(review_issue)
    # Fingerprint keys only on file:description — cosmetic field differences don't perturb it.
    assert Engine._issue_fingerprint(
        {"file": "orchestrator/engine.py", "description": "off-by-one in the retry ceiling"}
    ) == Engine._issue_fingerprint(review_issue)
