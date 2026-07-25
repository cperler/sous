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
    LanePolicy,
    LaneUsed,
    StageResult,
    WorkItem,
    compute_content_hash,
)

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _work(effort: str | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="claude-opus-5", created_at="now",
        lane_policy=H, effort=effort,
    )


def _result(effort: str | None = None) -> StageResult:
    return StageResult(
        work_item_id="wi-1", content_hash="h", run_id="r1", task_id="t1",
        stage=Stage.IMPLEMENT, model="claude-opus-5", effort=effort,
        status=ResultStatus.SUCCESS,
        lane_used=LaneUsed(
            execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE,
            invocation="agent(model=claude-opus-5)",
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
    assert dumped["model"] == "claude-opus-5"
    assert isinstance(dumped["model"], str)
    # Full fidelity: load(dump) re-dumps to the identical document.
    assert WorkItem.model_validate(dumped).model_dump(mode="json") == dumped
    assert WorkItem.model_validate(dumped).effort is Effort.HIGH  # load coerces back


def test_stageresult_json_round_trips_with_string_values() -> None:
    r = _result(effort="medium")
    dumped = r.model_dump(mode="json")
    assert dumped["effort"] == "medium"
    assert isinstance(dumped["effort"], str)
    assert dumped["model"] == "claude-opus-5"
    assert WorkItem.model_validate(_work().model_dump(mode="json")).effort is None
    assert StageResult.model_validate(dumped).model_dump(mode="json") == dumped
    assert StageResult.model_validate(dumped).effort is Effort.MEDIUM


def test_content_hash_is_byte_identical_to_the_pre_migration_shape() -> None:
    """The Effort/ModelId retype must not perturb a dispatch's identity key: the hash of
    a bare-string call equals the hash computed from the raw pre-migration string parts."""
    legacy_blob = "\x1f".join(
        ["implement", "p", "implement", "claude-opus-5", "headless:claude", "0", "high"]
    )
    legacy = hashlib.sha256(legacy_blob.encode("utf-8")).hexdigest()
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
        model="claude-opus-5", lane_policy=H, attempt=0, effort="high",
    ) == legacy
    # And the Effort-member call hashes identically to the bare-string call.
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
        model="claude-opus-5", lane_policy=H, attempt=0, effort=Effort.HIGH,
    ) == legacy
