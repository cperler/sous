"""The typed-field assignment convention on the status models (#172).

``validate_assignment=True`` (shared ``_StatusModel`` base in schemas/status.py) makes
enum-typed fields self-coercing at ASSIGNMENT time — the piece the #147
(StageRecord.effort) and #161 (Task.effort_pin) migrations each had to hand-roll with
explicit ``Effort(...)`` wraps and ``.value`` extractions. These tests pin the
convention's three guarantees so future string -> enum migrations stay mechanical:

1. assigning a bare string coerces to the enum member,
2. an invalid string raises AT the assignment (not later, on load),
3. stored JSON is byte-identical to the pre-convention shape (StrEnum serializes
   as its value — no SCHEMA_VERSION bump).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.schemas.enums import Effort, RunState, Stage, StageStatus, TaskState
from orchestrator.schemas.status import Run, StageRecord, Task


def _task() -> Task:
    return Task(task_id="t1", run_id="r1", created_at="t0", updated_at="t0")


# --- 1. string assignment coerces to the enum -----------------------------------

def test_stage_record_effort_assignment_coerces_str_to_enum() -> None:
    rec = StageRecord()
    rec.effort = "high"
    assert rec.effort is Effort.HIGH  # the member, not the bare string
    rec.effort = None  # effort-less dispatch stays assignable
    assert rec.effort is None


def test_task_effort_pin_assignment_coerces_str_to_enum() -> None:
    task = _task()
    task.effort_pin = "low"
    assert task.effort_pin is Effort.LOW
    assert task.effort_pin == "low"  # StrEnum still compares as its value


def test_state_fields_coerce_on_assignment_too() -> None:
    """The convention is model-wide, not effort-specific: every enum-typed status
    field self-coerces at the write site."""
    rec = StageRecord()
    rec.status = "running"
    assert rec.status is StageStatus.RUNNING
    task = _task()
    task.state = "completed"
    assert task.state is TaskState.COMPLETED
    run = Run(run_id="r1", created_at="t0", updated_at="t0")
    run.state = "running"
    assert run.state is RunState.RUNNING


# --- 2. an invalid string raises at the assignment -------------------------------

def test_invalid_effort_string_raises_at_assignment() -> None:
    rec = StageRecord()
    with pytest.raises(ValidationError):
        rec.effort = "turbo"
    assert rec.effort is None  # the failed write never landed
    task = _task()
    with pytest.raises(ValidationError):
        task.effort_pin = "xhigh"
    assert task.effort_pin is None


def test_deliver_fold_skips_malformed_pr_values_instead_of_crashing() -> None:
    """The _absorb_outputs pr_* fold stays TOLERANT under the convention: a malformed
    model-produced value (pr_number="") is dropped at the write instead of raising out
    of record() — and instead of landing silently and corrupting the stored doc, which
    was the pre-#172 behavior."""
    from orchestrator.state_machine import _absorb_outputs
    from tests.test_context_plane import make_result_stub

    task = _task()
    _absorb_outputs(task, make_result_stub(
        Stage.DELIVER, {"pr_number": "", "pr_url": "https://example.test/pr/7"},
    ))
    assert task.pr_number is None  # malformed value skipped, not stored, no raise
    assert task.pr_url == "https://example.test/pr/7"  # the valid sibling still folds


def test_deliver_fold_returns_drop_notice_for_malformed_pr_value() -> None:
    """#201: dropping a malformed pr_* value is no longer SILENT — _absorb_outputs
    returns a bounded drop notice (field + offending value + reason) per drop so the
    engine can emit a warning-grade audit event. The valid sibling still folds and
    contributes no notice."""
    from orchestrator.state_machine import _absorb_outputs
    from tests.test_context_plane import make_result_stub

    task = _task()
    notices = _absorb_outputs(task, make_result_stub(
        Stage.DELIVER, {"pr_number": "", "pr_url": "https://example.test/pr/7"},
    ))
    assert task.pr_number is None  # dropped value stays unset
    assert task.pr_url == "https://example.test/pr/7"  # valid sibling still folds
    assert len(notices) == 1  # exactly one drop, for the malformed field
    (notice,) = notices
    assert notice["field"] == "pr_number"
    assert notice["value"] == "''"  # repr keeps the empty-string type visible
    assert notice["reason"]  # a non-empty, bounded reason string
    assert set(notice) == {"field", "value", "reason"}


def test_deliver_fold_returns_no_notice_when_all_pr_values_valid() -> None:
    """#201: a clean DELIVER fold drops nothing, so the notice list is empty (the engine
    emits no pr_field_dropped event)."""
    from orchestrator.state_machine import _absorb_outputs
    from tests.test_context_plane import make_result_stub

    task = _task()
    notices = _absorb_outputs(task, make_result_stub(
        Stage.DELIVER, {"pr_number": 42, "pr_url": "https://example.test/pr/42"},
    ))
    assert notices == []
    assert task.pr_number == 42
    assert task.pr_url == "https://example.test/pr/42"


# --- 3. stored JSON is byte-identical (StrEnum serializes as its value) ----------

def test_status_json_round_trips_with_string_values() -> None:
    """A pinned/stamped enum persists as its plain string — the stored shape is
    unchanged by the convention, so pre-#172 docs load and re-save byte-identically."""
    task = _task()
    task.effort_pin = "low"
    task.stages[next(iter(task.stages))].effort = "high"
    dumped = task.model_dump(mode="json")
    assert dumped["effort_pin"] == "low"  # the raw string, not an enum repr
    assert isinstance(dumped["effort_pin"], str)
    # Full fidelity: load(dump) re-dumps to the identical document.
    assert Task.model_validate(dumped).model_dump(mode="json") == dumped
    assert Task.model_validate(dumped).effort_pin is Effort.LOW  # load coerces back
