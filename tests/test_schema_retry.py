"""Headless×claude schema-validate-and-retry loop (#32).

When a ``claude -p`` call returns exit-0 but its structured output fails the stage schema,
the transport retries the SAME call up to ``max_schema_retries`` with a corrective follow-up
naming the exact validation errors — a cheap, targeted fix instead of burning a whole stage
attempt. The retry PREFERS resuming the same session; it falls back to a fresh call that
embeds the model's own prior invalid output. Mirrors the fake-subprocess patterns in
``test_headless_schema`` / ``test_session_continuity``.
"""

from __future__ import annotations

import json
import subprocess

from adapters.execution.runners import build_registry
from adapters.execution.transport import RawResult, claude_cli_transport, to_stage_result
from orchestrator.cost_ledger import CostLedger
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

# `ok` is required and boolean — the contract the fakes below satisfy or violate.
_SCHEMA_DICT = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
_SCHEMA = json.dumps(_SCHEMA_DICT)


def _work(session_ref: str | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.INTAKE, prompt="do it",
        schema_ref="intake", model="claude-haiku-4-5", created_at="now", session_ref=session_ref,
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    )


def _proc(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


def _queue_fake(calls: list, payloads: list[dict]):
    """A subprocess.run fake that returns the next queued envelope per call (last one sticks)."""
    def fake_run(argv, **kw):
        calls.append(argv)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return _proc(payload)
    return fake_run


def _prompt_of(argv: list[str]) -> str:
    return argv[argv.index("-p") + 1]


# --- the happy path: valid first try, no retry, no extra calls ---------------------------

def test_valid_first_try_zero_retries_no_extra_calls(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        _queue_fake(calls, [{"structured_output": {"ok": True}, "session_id": "s1"}]))

    raw = claude_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.structured_output == {"ok": True}
    assert raw.schema_retries == 0
    assert len(calls) == 1  # no corrective call


# --- invalid then valid: one retry, success, schema_retries recorded ---------------------

def test_invalid_then_valid_retries_once_and_succeeds(monkeypatch) -> None:
    calls: list = []
    payloads = [
        {"structured_output": {"wrong": 1}, "session_id": "s1"},  # missing required `ok`
        {"structured_output": {"ok": True}, "session_id": "s1"},  # corrected
    ]
    monkeypatch.setattr(subprocess, "run", _queue_fake(calls, payloads))

    raw = claude_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.structured_output == {"ok": True}
    assert raw.schema_retries == 1
    assert len(calls) == 2


def test_corrective_prompt_carries_the_validation_path_and_message(monkeypatch) -> None:
    calls: list = []
    payloads = [
        {"structured_output": {"wrong": 1}, "session_id": "s1"},
        {"structured_output": {"ok": True}, "session_id": "s1"},
    ]
    monkeypatch.setattr(subprocess, "run", _queue_fake(calls, payloads))

    claude_cli_transport(lambda ref: _SCHEMA)(_work())

    corrective = _prompt_of(calls[1])
    # the specific validation error (jsonschema's own message) AND the schema are quoted back,
    # plus the "return ONLY corrected JSON" instruction.
    assert "'ok' is a required property" in corrective
    assert '"required"' in corrective and '"ok"' in corrective  # the schema excerpt
    assert "ONLY a single corrected JSON object" in corrective


# --- session continuation is preferred when a session id is available --------------------

def test_retry_resumes_the_session_when_one_is_available(monkeypatch) -> None:
    calls: list = []
    payloads = [
        {"structured_output": {"wrong": 1}, "session_id": "sess-abc"},
        {"structured_output": {"ok": True}, "session_id": "sess-abc"},
    ]
    monkeypatch.setattr(subprocess, "run", _queue_fake(calls, payloads))

    claude_cli_transport(lambda ref: _SCHEMA)(_work())

    # the corrective call resumed the session the first call reported (continuation preferred)
    assert "--resume" in calls[1]
    assert calls[1][calls[1].index("--resume") + 1] == "sess-abc"
    # continuation => the model keeps its own context, so we do NOT re-embed the prior output
    assert "previous (invalid) output" not in _prompt_of(calls[1])


def test_retry_without_a_session_falls_back_to_a_fresh_call_embedding_prior_output(monkeypatch) -> None:
    calls: list = []
    payloads = [
        {"structured_output": {"wrong": 1}},          # no session_id -> nothing to resume
        {"structured_output": {"ok": True}},
    ]
    monkeypatch.setattr(subprocess, "run", _queue_fake(calls, payloads))

    raw = claude_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.structured_output == {"ok": True}
    assert raw.schema_retries == 1
    corrective = _prompt_of(calls[1])
    assert "--resume" not in calls[1]                 # fresh call, not a resume
    assert "previous (invalid) output" in corrective  # embeds the model's own prior output
    assert '"wrong"' in corrective


# --- persistent failure: honest SCHEMA_VIOLATION after max retries, errors in the output --

def test_persistent_invalid_fails_after_max_retries_with_errors_in_output(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        _queue_fake(calls, [{"structured_output": {"wrong": 1}, "session_id": "s1"}]))

    raw = claude_cli_transport(lambda ref: _SCHEMA, max_schema_retries=2)(_work())

    assert raw.structured_output is None            # -> SCHEMA_VIOLATION upstream (fails as before)
    assert raw.schema_retries == 2
    assert len(calls) == 3                          # 1 original + 2 corrective retries
    assert "validation still failing after 2" in raw.raw_output
    assert "'ok' is a required property" in raw.raw_output  # specific, for the retry learnings


def test_max_schema_retries_is_configurable(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        _queue_fake(calls, [{"structured_output": {"wrong": 1}}]))

    raw = claude_cli_transport(lambda ref: _SCHEMA, max_schema_retries=0)(_work())

    assert raw.schema_retries == 0 and len(calls) == 1  # no retries budgeted
    assert raw.structured_output is None


# --- transport errors are never spent on a schema retry ----------------------------------

def test_transport_error_is_not_a_schema_retry(monkeypatch) -> None:
    calls: list = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    raw = claude_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.exit_code == 1 and raw.schema_retries == 0
    assert len(calls) == 1  # a non-zero exit is not a schema problem — no corrective call


# --- end to end through the runner: SCHEMA_VIOLATION + schema_retries on the result -------

def test_runner_reports_schema_violation_and_retry_count(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run",
                        _queue_fake(calls, [{"structured_output": {"wrong": 1}, "session_id": "s1"}]))

    reg = build_registry(include_interactive=False, headless_schema_provider=lambda ref: _SCHEMA_DICT)
    result = reg.resolve(_work().lane_policy).dispatch(_work())

    assert result.status is ResultStatus.SCHEMA_VIOLATION
    assert result.schema_retries == 2  # the loop's spend is visible on the engine-facing result


# --- schema_retries rides RawResult -> StageResult -> the cost-ledger row ----------------

def test_schema_retries_flows_to_the_cost_ledger_row(tmp_path) -> None:
    raw = RawResult({"ok": True}, schema_retries=2, invocation="claude -p (fake)")
    sr = to_stage_result(_work(), raw, ResultStatus.SUCCESS,
                         mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    assert sr.schema_retries == 2

    row = CostLedger(tmp_path / "stage-costs.jsonl").record(sr)
    assert row["schema_retries"] == 2
