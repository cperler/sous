"""Codex `--output-schema` strictness (live-20260704-2 #68 fix-forward).

OpenAI's structured-output validator 400s (`invalid_json_schema`) unless every object node
carries `additionalProperties: false`. The transport therefore (a) hands codex a
strict-transformed COPY of the stage schema, and (b) if codex still rejects the schema file
itself, drops the nudge for the dispatch instead of burning attempts on an unfixable 400 —
postamble + schema-retry loop remain the guarantee.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from adapters.execution.transport import (
    RawResult,
    _codex_schema_rejected,
    _codex_strict_schema,
    codex_cli_transport,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema
from orchestrator.schemas.work import LanePolicy, WorkItem

_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)

_SCHEMA = json.dumps({
    "type": "object",
    "required": ["ok"],
    "properties": {
        "ok": {"type": "boolean"},
        "nested": {"type": "object", "properties": {"x": {"type": "string"}}},
        "list": {"type": "array", "items": {"type": "object", "properties": {}}},
        "either": {"anyOf": [{"type": "object", "properties": {}}, {"type": "string"}]},
    },
    "$defs": {"leaf": {"type": "object", "properties": {"y": {"type": "integer"}}}},
})

# The exact live 400 from run live-20260704-2 task #68 (implement attempt 0).
_LIVE_400 = json.dumps({
    "type": "error",
    "message": json.dumps({
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_json_schema",
            "message": "Invalid schema for response_format 'codex_output_schema': In "
                       "context=(), 'additionalProperties' is required to be supplied "
                       "and to be false.",
            "param": "text.format.schema",
        },
        "status": 400,
    }),
})


def _work() -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="gpt-5.5", created_at="now", lane_policy=_CODEX,
    )


def _fake(calls: list, schema_bodies: list, payloads: list[dict]):
    """Records argv + the on-disk schema file content per call (the temp file is gone after
    the call returns, so it must be read inside the fake), then plays the queued payload."""
    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "--output-schema" in argv:
            schema_bodies.append(Path(argv[argv.index("--output-schema") + 1]).read_text())
        else:
            schema_bodies.append(None)
        p = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if "--output-last-message" in argv and p.get("structured") is not None:
            target = argv[argv.index("--output-last-message") + 1]
            Path(target).write_text(json.dumps(p["structured"]), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, p.get("returncode", 0), stdout=p.get("stdout", ""), stderr=p.get("stderr", "")
        )
    return fake_run


# --- the strict transform ------------------------------------------------------------------

def test_strict_transform_stamps_every_object_node() -> None:
    out = json.loads(_codex_strict_schema(_SCHEMA))
    assert out["additionalProperties"] is False
    assert out["properties"]["nested"]["additionalProperties"] is False
    assert out["properties"]["list"]["items"]["additionalProperties"] is False
    assert out["properties"]["either"]["anyOf"][0]["additionalProperties"] is False
    assert out["$defs"]["leaf"]["additionalProperties"] is False
    # non-object nodes untouched
    assert "additionalProperties" not in out["properties"]["ok"]


def test_strict_transform_on_real_stage_schemas() -> None:
    for ref in ("intake", "scope", "implement", "test", "deliver", "review"):
        schema = resolve_stage_schema(ref)
        assert schema is not None
        out = json.loads(_codex_strict_schema(json.dumps(schema)))
        assert out["additionalProperties"] is False


def test_strict_transform_passes_through_unparseable() -> None:
    assert _codex_strict_schema("not json{") == "not json{"


def test_schema_rejected_marker_is_narrow() -> None:
    hit = RawResult(None, exit_code=1, raw_output=_LIVE_400)
    assert _codex_schema_rejected(hit)
    # success or a genuine failure never reads as schema-rejection
    assert not _codex_schema_rejected(RawResult({"ok": True}, exit_code=0, raw_output=_LIVE_400))
    assert not _codex_schema_rejected(RawResult(None, exit_code=1, error="tests failed"))


# --- the transport behavior ----------------------------------------------------------------

def test_codex_receives_strict_transformed_schema(monkeypatch) -> None:
    calls: list = []
    bodies: list = []
    monkeypatch.setattr(subprocess, "run", _fake(calls, bodies, [{"structured": {"ok": True}}]))

    codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert "--output-schema" in calls[0]
    sent = json.loads(bodies[0])
    assert sent["additionalProperties"] is False  # the nudge got the strict COPY
    assert json.loads(_SCHEMA).get("additionalProperties") is None  # original untouched


def test_schema_rejection_degrades_to_no_schema_not_a_failure(monkeypatch) -> None:
    """The live #68 shape: codex 400s on the schema file itself → the dispatch retries once
    WITHOUT the nudge and succeeds; the 400 never becomes a stage failure."""
    calls: list = []
    bodies: list = []
    monkeypatch.setattr(subprocess, "run", _fake(calls, bodies, [
        {"returncode": 1, "stdout": _LIVE_400},
        {"structured": {"ok": True}},
    ]))

    raw = codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.exit_code == 0 and raw.structured_output == {"ok": True}
    assert "--output-schema" in calls[0]
    assert "--output-schema" not in calls[1]  # degraded: nudge dropped for the re-call


def test_genuine_failure_does_not_trigger_schema_degrade(monkeypatch) -> None:
    calls: list = []
    bodies: list = []
    monkeypatch.setattr(subprocess, "run", _fake(calls, bodies, [
        {"returncode": 1, "stdout": "", "stderr": "tests failed hard"},
    ]))

    raw = codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.exit_code == 1
    assert len(calls) == 1  # no silent extra call on a genuine failure
