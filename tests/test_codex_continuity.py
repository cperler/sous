"""Codex parity: session continuity + schema-validate-and-retry (#9 / #21).

Mirrors ``test_session_continuity`` (claude) and ``test_schema_retry`` (the loop) for the
codex transport. The installed ``codex`` CLI exposes ``codex exec resume <id>``; the first
call's thread id (``thread.started`` on the ``--json`` stream) is captured and reused, a
stale id cold-starts once, provider-tagging keeps a claude ref out of codex, and the shared
schema-retry loop validates/retries codex output exactly as the claude path does.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from adapters.execution.transport import (
    RawResult,
    codex_cli_transport,
    is_provider_unavailable,
    to_stage_result,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore

# `ok` is required + boolean — the contract the codex fakes satisfy or violate.
_SCHEMA_DICT = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
_SCHEMA = json.dumps(_SCHEMA_DICT)

_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _work(session_ref: str | None = None) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="gpt-5-codex", created_at="now", session_ref=session_ref,
        lane_policy=_CODEX,  # no cwd -> no git-common-dir grant probe (keeps the fake clean)
    )


def _events(thread_id: str = "th-1") -> str:
    """A minimal ``codex exec --json`` stdout stream: the early ``thread.started`` carrying the
    session/thread id, then a ``turn.completed`` with usage — the real event shapes."""
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 10, "output_tokens": 5}}),
    ])


def _queue_codex_fake(calls: list, payloads: list[dict]):
    """A ``subprocess.run`` fake for the codex transport: records argv, writes the queued
    structured output to the ``--output-last-message`` file (as real codex does), and returns
    the queued stdout events / exit code (last payload sticks)."""
    def fake_run(argv, **kw):
        calls.append(list(argv))
        p = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if "--output-last-message" in argv and p.get("structured") is not None:
            target = argv[argv.index("--output-last-message") + 1]
            Path(target).write_text(json.dumps(p["structured"]), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, p.get("returncode", 0), stdout=p.get("stdout", ""), stderr=p.get("stderr", "")
        )
    return fake_run


# --- session capture + resume ------------------------------------------------------------

def test_session_id_captured_from_jsonl_events(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": {"ok": True}, "stdout": _events("th-abc")}]))

    raw = codex_cli_transport()(_work())

    assert raw.session_ref == "th-abc"  # the thread id, reported back for the engine to chain
    assert calls[0][:2] == ["codex", "exec"] and "resume" not in calls[0]  # a fresh cold start


def test_second_dispatch_resumes_the_session(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": {"ok": True}, "stdout": _events("th-abc")}]))

    raw = codex_cli_transport()(_work(session_ref="th-abc"))

    # resume form: `codex exec resume <id> ...`
    assert calls[0][:4] == ["codex", "exec", "resume", "th-abc"]
    assert "resume th-abc" in raw.invocation
    assert raw.session_ref == "th-abc"


def test_stale_resume_falls_back_cold_once(monkeypatch) -> None:
    calls: list = []
    payloads = [
        # resume rejected: codex's own "no rollout found for thread id ..." (exit 1)
        {"structured": None, "returncode": 1,
         "stderr": "Error: thread/resume: thread/resume failed: no rollout found for thread id th-x"},
        # cold retry succeeds
        {"structured": {"ok": True}, "stdout": _events("th-new")},
    ]
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(calls, payloads))

    raw = codex_cli_transport()(_work(session_ref="th-x"))

    assert len(calls) == 2
    assert calls[0][:4] == ["codex", "exec", "resume", "th-x"]  # attempted resume
    assert "resume" not in calls[1]  # then a cold start, same dispatch
    assert raw.exit_code == 0 and raw.session_ref == "th-new"


def test_non_session_resume_error_fails_normally(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": None, "returncode": 1, "stderr": "Error: model not available"}]))

    raw = codex_cli_transport()(_work(session_ref="th-x"))

    assert len(calls) == 1  # a non-session error is NOT retried cold (mirrors the claude path)
    assert raw.exit_code == 1 and "model not available" in raw.error


# --- #80: the real provider refusal lives on the stdout event stream, not stderr ---------

# The exact live-20260704 codex stream for task #68: a 400 "model is not supported" that codex
# reports ONLY via the `error`/`turn.failed` events; stderr carried just the deprecation banner.
_MODEL_UNSUPPORTED_400 = json.dumps({
    "type": "error", "status": 400,
    "error": {"type": "invalid_request_error",
              "message": "The 'gpt-5-codex' model is not supported when using Codex "
                         "with a ChatGPT account."},
})
_LIVE_UNSUPPORTED_STREAM = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "019f2d7f-f83d"}),
    json.dumps({"type": "item.completed", "item": {
        "id": "item_0", "type": "error",
        "message": "Model metadata for `gpt-5-codex` not found. Defaulting to fallback metadata; "
                   "this can degrade performance and cause issues."}}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "error", "message": _MODEL_UNSUPPORTED_400}),
    json.dumps({"type": "turn.failed", "error": {"message": _MODEL_UNSUPPORTED_400}}),
])
_LIVE_UNSUPPORTED_STDERR = ("warning: `--full-auto` is deprecated; use `--sandbox "
                            "workspace-write` instead.\nReading additional input from stdin...")


def test_codex_surfaces_stream_failure_cause_into_error(monkeypatch) -> None:
    """The 400 'model is not supported' lands on the event stream; the innocuous deprecation
    warning is all stderr carries. The transport must surface the STREAM cause onto
    ``RawResult.error`` so the verdict/ledger see the real refusal (#80)."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(calls, [{
        "structured": None, "returncode": 1,
        "stdout": _LIVE_UNSUPPORTED_STREAM, "stderr": _LIVE_UNSUPPORTED_STDERR,
    }]))

    raw = codex_cli_transport()(_work())

    assert raw.exit_code == 1
    # the real cause is surfaced, NOT the deprecation banner
    assert "model is not supported" in raw.error
    assert "--full-auto" not in raw.error
    # and it classifies as the provider being out, not a task failure
    assert is_provider_unavailable(raw)


def test_codex_error_falls_back_to_stderr_without_a_stream_cause(monkeypatch) -> None:
    """When the stream carries no failure event, the stderr excerpt is still used (unchanged
    behavior) — the stream-cause preference only kicks in when there IS a cause to prefer."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(calls, [{
        "structured": None, "returncode": 1, "stdout": "", "stderr": "boom: tests failed",
    }]))

    raw = codex_cli_transport()(_work())

    assert raw.exit_code == 1 and raw.error == "boom: tests failed"
    assert not is_provider_unavailable(raw)


# --- provider-tagging: a claude ref is never fed to codex --------------------------------

def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def test_claude_session_ref_is_not_reused_for_a_codex_stage(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", provider_tag="codex")  # IMPLEMENT/TEST route to codex

    intake = eng.next_work("r1", "t1")
    eng.record("r1", _make(eng, intake))  # engine lane, no session
    scope = eng.next_work("r1", "t1")
    assert scope.lane_policy.provider is Provider.CLAUDE
    eng.record("r1", _make(eng, scope, session_ref="claude-sess"))  # claude mints a ref

    implement = eng.next_work("r1", "t1")
    assert implement.lane_policy.provider is Provider.CODEX
    # the claude conversation id means nothing to `codex exec resume` — it must NOT ride along
    assert implement.session_ref is None


def test_same_provider_codex_ref_is_reused(tmp_path, project) -> None:
    from tests.conftest import make_result

    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", provider_tag="codex")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.record("r1", make_result(eng.next_work("r1", "t1"), session_ref="c-scope"))  # scope(claude)
    implement = eng.next_work("r1", "t1")
    assert implement.lane_policy.provider is Provider.CODEX
    assert implement.session_ref is None  # claude ref blocked
    # codex implement now mints its own ref -> the next codex stage (test) reuses it
    eng.record("r1", make_result(implement, session_ref="c-impl"))
    test = eng.next_work("r1", "t1")
    assert test.lane_policy.provider is Provider.CODEX
    assert test.session_ref == "c-impl"  # codex ref chains to the next codex stage


def _make(eng, work, *, session_ref=None):
    from tests.conftest import make_result
    return make_result(work, session_ref=session_ref)


# --- schema-validate-and-retry (mirrors test_schema_retry, codex transport) --------------

def test_valid_first_try_zero_retries_no_extra_calls(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": {"ok": True}, "stdout": _events()}]))

    raw = codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.structured_output == {"ok": True}
    assert raw.schema_retries == 0
    assert len(calls) == 1


def test_invalid_then_valid_retries_once_resuming_the_session(monkeypatch) -> None:
    calls: list = []
    payloads = [
        {"structured": {"wrong": 1}, "stdout": _events("th-1")},  # missing required `ok`
        {"structured": {"ok": True}, "stdout": _events("th-1")},  # corrected
    ]
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(calls, payloads))

    raw = codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.structured_output == {"ok": True}
    assert raw.schema_retries == 1
    assert len(calls) == 2
    # the corrective retry RESUMED the session the first call reported (warm continuity)
    assert calls[1][:4] == ["codex", "exec", "resume", "th-1"]
    corrective = calls[1][-1]  # the trailing prompt arg
    assert "'ok' is a required property" in corrective


def test_persistent_invalid_fails_after_max_retries_with_errors_in_output(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": {"wrong": 1}, "stdout": _events("th-1")}]))

    raw = codex_cli_transport(lambda ref: _SCHEMA, max_schema_retries=2)(_work())

    assert raw.structured_output is None          # -> SCHEMA_VIOLATION upstream
    assert raw.schema_retries == 2
    assert len(calls) == 3                         # 1 original + 2 corrective retries
    assert "validation still failing after 2" in raw.raw_output
    assert "'ok' is a required property" in raw.raw_output


def test_transport_error_is_not_a_schema_retry(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": None, "returncode": 1, "stderr": "boom"}]))

    raw = codex_cli_transport(lambda ref: _SCHEMA)(_work())

    assert raw.exit_code == 1 and raw.schema_retries == 0
    assert len(calls) == 1


def test_output_schema_flag_sent_when_schema_wired(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _queue_codex_fake(
        calls, [{"structured": {"ok": True}, "stdout": _events()}]))

    codex_cli_transport(lambda ref: _SCHEMA)(_work())

    # codex's own final-shape enforcement — the analog of claude's --json-schema
    assert "--output-schema" in calls[0]


def test_schema_retries_flows_to_the_cost_ledger_row(tmp_path) -> None:
    raw = RawResult({"ok": True}, schema_retries=2, invocation="codex exec (fake)")
    sr = to_stage_result(_work(), raw, ResultStatus.SUCCESS,
                         mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    assert sr.schema_retries == 2
    row = CostLedger(tmp_path / "stage-costs.jsonl").record(sr)
    assert row["schema_retries"] == 2
