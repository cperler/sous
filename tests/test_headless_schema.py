"""Headless×claude --json-schema wiring (bring-up: the lane never sent a schema, so every
stage was a SCHEMA_VIOLATION). Mirrors the codex schema-provider seam for the claude CLI."""

from __future__ import annotations

import json
import subprocess

from adapters.execution.runners import _schema_json_provider, build_registry
from adapters.execution.transport import (
    _json_object_from_text,
    _validate_shape,
    claude_cli_transport,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem


def _work(schema_ref: str = "intake") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.INTAKE, prompt="do it",
        schema_ref=schema_ref, model="claude-haiku-4-5", created_at="now",
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )


def _stub_json_run(calls: list, payload: dict | None = None):
    def fake_run(argv, **kw):
        calls.append(argv)
        body = json.dumps(payload if payload is not None else {"structured_output": {"ok": True}})
        return subprocess.CompletedProcess(argv, 0, stdout=body, stderr="")
    return fake_run


# --- transport passes --json-schema when a path provider is given -------------------


def test_transport_sends_inline_json_schema_when_provider_returns_one(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls))
    inline = '{"type":"object","required":["ok"]}'
    transport = claude_cli_transport(lambda ref: inline)

    transport(_work())

    assert "--json-schema" in calls[0]
    i = calls[0].index("--json-schema")
    assert calls[0][i + 1] == inline  # the schema JSON itself, inline — not a path


def test_transport_omits_json_schema_without_a_provider(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls))

    claude_cli_transport()(_work())  # no schema provider

    assert "--json-schema" not in calls[0]


def test_transport_omits_json_schema_when_provider_returns_none(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls))

    claude_cli_transport(lambda ref: None)(_work())  # provider knows no schema for this ref

    assert "--json-schema" not in calls[0]


# --- _schema_json_provider serializes inline + caches -------------------------------


def test_schema_json_provider_serializes_inline_and_caches() -> None:
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    calls: list[str] = []

    def schema_for(ref: str) -> dict | None:
        calls.append(ref)
        return schema if ref == "intake" else None

    json_for = _schema_json_provider(schema_for)

    s1 = json_for("intake")
    assert s1 is not None and json.loads(s1) == schema  # inline JSON string, not a path
    assert json_for("intake") == s1  # cached
    assert calls == ["intake"]  # schema_for consulted once
    assert json_for("nonexistent") is None  # unknown ref -> no schema, no arg


# --- build_registry wires the provider into a real (schema-enforcing) headless runner --


def test_build_registry_wires_headless_schema_into_the_transport(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    reg = build_registry(
        include_interactive=False,
        headless_schema_provider=lambda ref: schema,
    )
    runner = reg.resolve(_work().lane_policy)
    result = runner.dispatch(_work())

    # The headless runner actually shelled claude with --json-schema, and the structured
    # payload came back (SUCCESS, not SCHEMA_VIOLATION).
    assert calls and "--json-schema" in calls[0]
    assert result.status.value == "success"
    assert result.structured_output == {"ok": True}


# --- result-as-JSON fallback (model answered in prose, not the structured tool) ------


def test_transport_recovers_fenced_json_from_result(monkeypatch) -> None:
    calls: list = []
    # No structured_output key; the JSON object is fenced text in `result`.
    envelope = {"result": '```json\n{"approved": true, "issues": []}\n```',
                "session_id": "s1"}
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls, payload=envelope))

    raw = claude_cli_transport(lambda ref: '{"type":"object"}')(_work("review"))

    assert raw.structured_output == {"approved": True, "issues": []}


def test_transport_recovers_bare_json_object_from_result(monkeypatch) -> None:
    calls: list = []
    envelope = {"result": '{"ok": true}'}
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls, payload=envelope))

    raw = claude_cli_transport()(_work())

    assert raw.structured_output == {"ok": True}


def test_transport_leaves_genuine_prose_unstructured(monkeypatch) -> None:
    calls: list = []
    envelope = {"result": "I reviewed the PR and it looks good to me."}
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls, payload=envelope))

    raw = claude_cli_transport()(_work())

    assert raw.structured_output is None  # prose stays a schema_violation upstream


# --- _json_object_from_text: the realistic prose-wrapped shapes a model emits -----------


def test_json_object_recovery_handles_the_common_shapes() -> None:
    obj = {"approved": True, "issues": []}
    cases = {
        "bare": '{"approved": true, "issues": []}',
        "fenced": '```json\n{"approved": true, "issues": []}\n```',
        "preamble+fence": 'Here is the result:\n```json\n{"approved": true, "issues": []}\n```',
        "trailing prose": '{"approved": true, "issues": []}. Let me know if you need changes.',
        "tagged fence no newline": '```json {"approved": true, "issues": []} ```',
    }
    for label, text in cases.items():
        assert _json_object_from_text(text) == obj, f"failed to recover: {label}"

    # backticks INSIDE a string value must not confuse the brace scanner
    assert _json_object_from_text('{"detail": "```"}') == {"detail": "```"}
    # conservative: genuine prose and a bare array recover nothing (object-shaped only)
    assert _json_object_from_text("Looks good, approving.") is None
    assert _json_object_from_text("[1, 2, 3]") is None
    assert _json_object_from_text("{ not json }") is None


# --- _validate_shape: recovered/tool objects must satisfy the stage schema --------------


def test_validate_shape_rejects_wrong_shape_but_passes_valid() -> None:
    schema = '{"type":"object","required":["approved","issues"]}'
    assert _validate_shape({"approved": True, "issues": []}, schema) == {"approved": True, "issues": []}
    assert _validate_shape({"foo": 1}, schema) is None  # missing required keys -> rejected
    # a malformed schema must not veto real work
    assert _validate_shape({"foo": 1}, "not a schema") == {"foo": 1}


def test_transport_shape_gates_a_wrong_shape_tool_answer(monkeypatch) -> None:
    calls: list = []
    # structured_output present but missing the required review keys.
    envelope = {"structured_output": {"foo": 1}}
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls, payload=envelope))
    provider = lambda ref: '{"type":"object","required":["approved","issues"]}'  # noqa: E731

    raw = claude_cli_transport(provider)(_work("review"))

    assert raw.structured_output is None  # wrong shape -> SCHEMA_VIOLATION, not false SUCCESS


def test_transport_recovers_and_shape_accepts_prose_wrapped_answer(monkeypatch) -> None:
    calls: list = []
    envelope = {"result": 'Sure:\n```json\n{"approved": true, "issues": []}\n```'}
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls, payload=envelope))
    provider = lambda ref: '{"type":"object","required":["approved","issues"]}'  # noqa: E731

    raw = claude_cli_transport(provider)(_work("review"))

    assert raw.structured_output == {"approved": True, "issues": []}


def test_build_registry_injected_transport_beats_schema_provider() -> None:
    # An explicitly injected transport wins (tests wrap their own); the provider is ignored.
    sentinel_calls: list = []

    def fake_transport(work):
        sentinel_calls.append(work.id)
        from adapters.execution.transport import RawResult
        return RawResult({"ok": True}, exit_code=0, invocation="fake")

    reg = build_registry(
        include_interactive=False,
        headless_transport=fake_transport,
        headless_schema_provider=lambda ref: {"x": 1},
    )
    reg.resolve(_work().lane_policy).dispatch(_work())
    assert sentinel_calls == ["wi-1"]
