"""Audit-field parity lint (#164): the cost-ledger row and the per-stage JSON log must
agree on which model-call attribution fields they surface.

``_ATTRIBUTION_FIELDS`` is the single canonical set (owned by ``cost_ledger``). Both audit
write paths — ``CostLedger.record`` (the ledger row) and the two ``write_stage_log``
payloads assembled in the engine (the normal ``_record_result`` path and the ``abandon``
synthetic-result path) — must carry every field under the same top-level key. This test
drives all three real write paths and asserts each output contains the whole set, so the
next missing-field omission (the #151 ``effort`` gap regressing) fails at CI time instead
of silently diverging.
"""

from __future__ import annotations

import json

from orchestrator.cost_ledger import _ATTRIBUTION_FIELDS, CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.work import SubCall, TokenUsage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def test_attribution_fields_is_a_nonempty_curated_set() -> None:
    # A guard against the constant being emptied to a vacuous pass: parity of "nothing" is
    # trivially true. The #151 field must always be in the set it was filed to protect.
    assert _ATTRIBUTION_FIELDS
    assert "effort" in _ATTRIBUTION_FIELDS


def test_cost_ledger_row_surfaces_every_attribution_field(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    row = eng.ledger.rows()[-1]
    assert row.keys() >= _ATTRIBUTION_FIELDS


def test_every_sub_call_ledger_row_surfaces_every_attribution_field(tmp_path, project) -> None:
    """#73 §4: a plan-bearing dispatch writes one row PER SUB-CALL — each of those rows is a
    model-call attribution record in its own right, so the parity set must hold on every one
    of them (a sub-call row missing `effort`/`model`/`cost_usd` is the #151 gap, per call)."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    result = make_result(eng.next_work("r1", "t1")).model_copy(
        update={
            "sub_calls": (
                SubCall(phase="find:code", model="claude-opus-5",
                        usage=TokenUsage(input=100, output=10), duration_s=2.0),
                SubCall(phase="verify:0", model="claude-sonnet-5",
                        usage=TokenUsage(input=50, output=5), duration_s=1.0),
            )
        }
    )
    eng.record("r1", result)

    rows = eng.ledger.rows()
    assert [r["phase"] for r in rows] == ["find:code", "verify:0"]
    for row in rows:
        assert row.keys() >= _ATTRIBUTION_FIELDS


def test_normal_stage_log_surfaces_every_attribution_field(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake -> _record_result

    log = eng.store.read_stage_logs("t1")[-1]
    assert log.keys() >= _ATTRIBUTION_FIELDS
    # And the ledger row for the same stage agrees, field-for-field on the shared set.
    row = eng.ledger.rows()[-1]
    assert row.keys() >= _ATTRIBUTION_FIELDS


def test_abandon_stage_log_surfaces_every_attribution_field(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake completes
    work = eng.next_work("r1", "t1")  # scope dispatched, lease held, not recorded
    assert work.stage is Stage.SCOPE
    task = eng.abandon("r1", "t1", reason="orphaned")  # synthetic-result write_stage_log path

    seq = task.stage_counter
    log = json.loads((tmp_path / "stages" / "t1" / f"{seq:02d}-scope.json").read_text())
    assert log.keys() >= _ATTRIBUTION_FIELDS
