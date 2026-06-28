"""Canonical stage schemas + schema_for wiring (closes the codex-validation gap)."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from adapters.execution.codex import CodexRunner
from adapters.execution.transport import RawResult
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.stage_schemas import load_stage_schema, resolve_stage_schema
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import STAGE_SPECS

CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _wi(ref: str) -> WorkItem:
    return WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.TEST, prompt="p",
                           schema_ref=ref, model="gpt-5-codex", lane_policy=CODEX, created_at="t")


def test_every_stage_ref_has_a_valid_schema() -> None:
    # Every stage's schema_ref resolves to a well-formed Draft 2020-12 schema.
    for spec in STAGE_SPECS.values():
        schema = load_stage_schema(spec.schema_ref)
        assert schema is not None, f"missing schema for {spec.schema_ref}"
        assert schema["title"] == spec.schema_ref
        Draft202012Validator.check_schema(schema)  # raises if malformed
    assert load_stage_schema("does-not-exist") is None


def test_local_override_wins(tmp_path) -> None:
    (tmp_path / "test.json").write_text(json.dumps({"type": "object", "required": ["custom"]}))
    overridden = resolve_stage_schema("test", local_dir=tmp_path)
    assert overridden["required"] == ["custom"]
    # an un-overridden ref falls back to the canonical engine schema
    assert resolve_stage_schema("intake", local_dir=tmp_path)["title"] == "intake"


def test_adapters_expose_schema_for() -> None:
    from adapters.project.heysoo import get_config as heysoo
    from adapters.project.selfhost import get_config as selfhost
    for cfg in (heysoo(), selfhost()):
        assert cfg.schema_for("review")["title"] == "review"
        assert cfg.schema_for("unknown") is None


# The payoff: codex full-validation now runs against the REAL canonical schema.
def test_codex_validates_against_canonical_schema() -> None:
    runner = CodexRunner(
        transport=lambda w: RawResult(structured_output={"passed": True, "failures": []}),
        schema_provider=load_stage_schema,  # the engine's canonical schemas
    )
    # missing the required `tests_meaningful` -> SCHEMA_VIOLATION (schema-enforced test gate)
    assert runner.dispatch(_wi("test")).status is ResultStatus.SCHEMA_VIOLATION


def test_codex_accepts_schema_conformant_output() -> None:
    runner = CodexRunner(
        transport=lambda w: RawResult(
            structured_output={"passed": True, "failures": [], "tests_meaningful": True}
        ),
        schema_provider=load_stage_schema,
    )
    assert runner.dispatch(_wi("test")).status is ResultStatus.SUCCESS


def test_codex_rejects_wrong_type_against_canonical() -> None:
    runner = CodexRunner(
        transport=lambda w: RawResult(
            structured_output={"passed": "yes", "failures": [], "tests_meaningful": True}
        ),  # passed must be boolean
        schema_provider=load_stage_schema,
    )
    assert runner.dispatch(_wi("test")).status is ResultStatus.SCHEMA_VIOLATION


def test_default_outputs_satisfy_their_schemas() -> None:
    """The test fixtures' default stage outputs validate against the canonical schemas
    (keeps the fixtures and the shipped contracts in lock-step)."""
    from tests.conftest import _default_output
    for stage in Stage:
        schema = load_stage_schema(STAGE_SPECS[stage].schema_ref)
        errors = list(Draft202012Validator(schema).iter_errors(_default_output(stage)))
        assert not errors, f"{stage.value} default output violates its schema: {errors}"
