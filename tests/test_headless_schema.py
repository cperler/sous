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
    assert "--append-system-prompt" not in calls[0]


def test_transport_appends_json_only_directive_with_schema(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_json_run(calls))

    claude_cli_transport(lambda ref: '{"type":"object"}')(_work())

    # --json-schema is best-effort on `claude -p`, so a system-prompt directive forces
    # the JSON object as the final output (so an agentic stage doesn't end in prose).
    assert "--append-system-prompt" in calls[0]
    directive = calls[0][calls[0].index("--append-system-prompt") + 1]
    assert "ONLY a single" in directive and "JSON object" in directive


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


# --- #282: the argv the real CLI parses -----------------------------------------------
# Asserted at the ARGV level. The bug that shipped with #256 was invisible to every test in
# this file because they all stub `subprocess.run` and never let the real binary parse
# `--json-schema` — so the payload's *shape* was never checked, only its plumbing.

def test_schema_json_provider_strips_meta_keys_the_cli_rejects() -> None:
    """`claude -p --json-schema` fails closed on a top-level `$schema`/`$id` — it resolves
    them as a `$ref` and errors "no schema with key or ref …", killing the dispatch before
    any model call. Canonical stage schemas are Draft 2020-12 docs and all carry `$schema`,
    so without this strip the headless lane cannot dispatch ANY stage (#282). The interactive
    lane has always stripped these in `workflow_shim.js::sanitizeSchema`."""
    canonical = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.invalid/review.json",
        "title": "review",
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
    }
    emitted = json.loads(_schema_json_provider(lambda ref: canonical)("review"))

    assert "$schema" not in emitted
    assert "$id" not in emitted
    # Stripping must not weaken the contract — everything else survives verbatim.
    assert emitted["type"] == "object"
    assert emitted["required"] == ["approved"]
    assert emitted["properties"] == {"approved": {"type": "boolean"}}
    assert emitted["title"] == "review"


def test_every_canonical_stage_schema_survives_the_provider() -> None:
    """The real schemas through the real provider: none may reach the CLI carrying a
    meta-key. Guards the whole stage surface, so a newly added schema cannot reintroduce
    #282 by being written the same (correct) way as every existing one."""
    from pathlib import Path

    from orchestrator.schemas.stage_schemas import load_stage_schema

    provider = _schema_json_provider(load_stage_schema)
    refs = sorted(p.stem for p in Path("orchestrator/schemas/stages").glob("*.json"))
    assert refs, "no stage schemas found — this guard would be vacuous"
    for ref in refs:
        emitted = json.loads(provider(ref))
        assert "$schema" not in emitted, f"{ref}.json would be rejected by --json-schema"
        assert "$id" not in emitted, f"{ref}.json would be rejected by --json-schema"


def test_claude_cli_argv_carries_a_meta_key_free_schema() -> None:
    """End of the wire: whatever lands after `--json-schema` in the argv the transport builds
    must be a schema the CLI accepts."""
    from orchestrator.schemas.stage_schemas import load_stage_schema

    calls: list = []
    transport = claude_cli_transport(_schema_json_provider(load_stage_schema))
    import adapters.execution.transport as T

    orig = T.subprocess.run
    T.subprocess.run = _stub_json_run(calls, {"structured_output": {"approved": True}})
    try:
        transport(_work(schema_ref="review"))
    finally:
        T.subprocess.run = orig

    argv = calls[0]
    assert "--json-schema" in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "$schema" not in schema and "$id" not in schema
