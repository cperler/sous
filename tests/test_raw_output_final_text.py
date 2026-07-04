"""#93: ``RawResult.raw_output`` is the model's readable FINAL TEXT, not the raw JSONL event
stream.

Since #66 the headless transports stream ``--output-format stream-json`` / codex ``--json`` and
teed the whole event stream. The stream was ALSO landing in ``raw_output``, which is what the
human-facing per-stage .md ``## Commentary``, the failure-learning output tail, and schema-retry
corrective prompts all show — so every stage's Commentary was raw JSONL noise duplicating the
retained ``.stream.jsonl`` next door. These tests lock that raw_output now carries the final text
(with last-assistant-text and bounded-tail fallbacks), on both lanes, and that the failure-
learning tail reads sensibly.
"""

from __future__ import annotations

import json
from pathlib import Path

import adapters.execution.transport as T
from adapters.execution.transport import (
    claude_cli_transport,
    codex_cli_transport,
    to_stage_result,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import (
    claude_final_text,
    codex_final_text,
    looks_like_event_stream,
    readable_text_from_stream,
    stream_tail_note,
)

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _wi(*, stage=Stage.IMPLEMENT, attempt=0, task_id="#42") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task_id, stage=stage, prompt="p",
        schema_ref="implement", model="m", lane_policy=_HEADLESS, created_at="now",
        attempt=attempt,
    )


def _codex_wi() -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="gpt-5-codex", lane_policy=_CODEX, created_at="now",
    )


# --- claude streaming lane ------------------------------------------------------------------


def _patch_claude(monkeypatch, stdout: str, stderr: str = "", returncode: int = 0) -> None:
    def fake(argv, *, timeout, cwd, tee_path, env=None):
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(stdout, encoding="utf-8")
        return returncode, stdout, stderr
    monkeypatch.setattr(T, "_run_teed", fake)


def _claude_stream_with_result(result_text: str, assistant_text: str = "thinking out loud") -> str:
    return (
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
        + json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": assistant_text}]}}) + "\n"
        + json.dumps({"type": "result", "subtype": "success", "session_id": "s1",
                      "result": result_text, "usage": {}}) + "\n"
    )


def test_claude_streaming_raw_output_is_result_text_not_stream(tmp_path, monkeypatch) -> None:
    stream = _claude_stream_with_result("The final human-readable answer.")
    _patch_claude(monkeypatch, stream)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.raw_output == "The final human-readable answer."
    # the raw JSONL event stream is NOT in raw_output (it lives in the teed .stream.jsonl)
    assert '"type"' not in raw.raw_output
    assert not looks_like_event_stream(raw.raw_output)
    # and the full stream still went to disk, verbatim
    assert (tmp_path / raw.stream_files["stream"]).read_text() == stream


def test_claude_streaming_falls_back_to_last_assistant_text(tmp_path, monkeypatch) -> None:
    # A result event with no `result` prose (e.g. the model ended via the structured tool):
    # raw_output falls back to the last assistant text block, not the stream.
    stream = (
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text", "text": "first turn"}]}}) + "\n"
        + json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "the last thing I said"}]}}) + "\n"
        + json.dumps({"type": "result", "subtype": "success", "session_id": "s1",
                      "structured_output": {"ok": True}, "usage": {}}) + "\n"
    )
    _patch_claude(monkeypatch, stream)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.raw_output == "the last thing I said"
    assert raw.structured_output == {"ok": True}


def test_claude_streaming_bounded_tail_note_on_textless_failure(tmp_path, monkeypatch) -> None:
    # A hard failure whose stream carries no readable text at all: raw_output is a bounded tail
    # of the stream, prefixed with a one-line pointer to the retained full stream.
    stream = "\n".join(json.dumps({"type": "system", "n": i}) for i in range(50)) + "\n"
    _patch_claude(monkeypatch, stream, stderr="boom", returncode=1)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.exit_code == 1
    assert raw.raw_output.startswith("[no final text — stream tail; full stream: ")
    assert "stages/42/implement-attempt0.stream.jsonl" in raw.raw_output.splitlines()[0]
    assert len(raw.raw_output) < len(stream) + 200  # bounded, not the whole stream


def test_claude_streaming_no_result_event_note(tmp_path, monkeypatch) -> None:
    # Exit 0 but a partial/killed stream with no `result` event AND no assistant text → the
    # dispatch fails honestly and raw_output is the tail note, never the raw stream.
    stream = json.dumps({"type": "system", "subtype": "init"}) + "\n"
    _patch_claude(monkeypatch, stream)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.error == "no result event in stream-json output"
    assert raw.raw_output.startswith("[no final text — stream tail;")


def test_claude_single_shot_raw_output_unchanged(tmp_path, monkeypatch) -> None:
    # The legacy single-shot `--output-format json` path (no run dir) keeps raw_output = stdout,
    # which there IS the readable JSON envelope, not an event stream. Don't regress it.
    envelope = json.dumps({"result": "answer", "session_id": "s1", "usage": {}})

    class _Proc:
        returncode = 0
        stdout = envelope
        stderr = ""

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _Proc())
    raw = claude_cli_transport()(_wi())  # no run_log_root → single-shot path
    assert raw.raw_output == envelope


# --- codex lane -----------------------------------------------------------------------------


def _codex_stream(agent_text: str | None) -> str:
    lines = [json.dumps({"type": "thread.started", "thread_id": "th-1"})]
    if agent_text is not None:
        lines.append(json.dumps({"type": "item.completed",
                                 "item": {"id": "i0", "type": "agent_message", "text": agent_text}}))
    lines.append(json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 1, "output_tokens": 1}}))
    return "\n".join(lines) + "\n"


def _patch_codex(monkeypatch, stdout: str, *, last_message: str | None = None,
                 returncode: int = 0) -> None:
    def fake(argv, *, timeout, cwd, tee_path, env=None):
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(stdout, encoding="utf-8")
        if last_message is not None and "--output-last-message" in argv:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                last_message, encoding="utf-8"
            )
        return returncode, stdout, ""
    monkeypatch.setattr(T, "_run_teed", fake)


def test_codex_raw_output_is_agent_message_text(tmp_path, monkeypatch) -> None:
    _patch_codex(monkeypatch, _codex_stream("codex final summary"),
                 last_message=json.dumps({"ok": True}))
    raw = codex_cli_transport(run_log_root=tmp_path)(_codex_wi())

    assert raw.raw_output == "codex final summary"
    assert raw.structured_output == {"ok": True}  # the JSON last-message stays the machine record
    assert not looks_like_event_stream(raw.raw_output)


def test_codex_raw_output_falls_back_to_prose_last_message(tmp_path, monkeypatch) -> None:
    # No agent_message in the stream, and the --output-last-message file is prose (not the
    # structured JSON object): raw_output is that prose, not the event stream.
    _patch_codex(monkeypatch, _codex_stream(None), last_message="prose wrap-up, no JSON here")
    raw = codex_cli_transport(run_log_root=tmp_path)(_codex_wi())

    assert raw.raw_output == "prose wrap-up, no JSON here"


def test_codex_raw_output_tail_note_on_textless_failure(tmp_path, monkeypatch) -> None:
    stream = _codex_stream(None)
    _patch_codex(monkeypatch, stream, returncode=1)
    raw = codex_cli_transport(run_log_root=tmp_path)(_codex_wi())

    assert raw.exit_code == 1
    assert raw.raw_output.startswith("[no final text — stream tail;")


# --- stream_probe extraction helpers --------------------------------------------------------


def test_looks_like_event_stream_discriminates_prose_from_jsonl() -> None:
    jsonl = "\n".join(json.dumps({"type": "x", "i": i}) for i in range(5))
    assert looks_like_event_stream(jsonl)
    assert not looks_like_event_stream("Just some prose.\nSecond line.\nThird line.")
    assert not looks_like_event_stream('{"type":"x"}')  # too few lines
    # a lone JSON object embedded in prose is not a stream
    assert not looks_like_event_stream('The answer is:\n{"a": 1}\ndone')


def test_readable_text_from_stream_extracts_either_lane() -> None:
    claude = _claude_stream_with_result("claude answer")
    codex = _codex_stream("codex answer")
    assert readable_text_from_stream(claude) == "claude answer"
    assert readable_text_from_stream(codex) == "codex answer"
    assert claude_final_text("nonsense\nnot json") is None
    assert codex_final_text("nonsense\nnot json") is None


def test_stream_tail_note_is_bounded_and_points_at_the_file() -> None:
    note = stream_tail_note("x" * 5000, "stages/1/implement-attempt0.stream.jsonl", max_chars=100)
    first = note.splitlines()[0]
    assert "stages/1/implement-attempt0.stream.jsonl" in first
    assert len(note) < 300  # bounded tail, not the whole 5000-char stream


# --- engine-level: the failure-learning tail is readable, not JSONL -------------------------


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project,
                  breaker_threshold=9)


def test_failure_learning_tail_is_readable_final_text(tmp_path, project, monkeypatch) -> None:
    # Drive a real streamed FAILURE through the transport, feed its StageResult to the engine,
    # and confirm the failure-learning output tail shows the model's readable text — not the
    # raw JSONL event stream that used to pollute it.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(3):  # intake, scope, implement
        from tests.conftest import make_result
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST

    stream = (
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text",
                                             "text": "Tests failed: AssertionError expected 3 got 2"}]}}) + "\n"
        + json.dumps({"type": "result", "is_error": True, "usage": {}}) + "\n"
    )
    _patch_claude(monkeypatch, stream, stderr="1 test failed", returncode=1)
    raw = claude_cli_transport(run_log_root=tmp_path)(w)
    result = to_stage_result(w, raw, ResultStatus.FAILURE,
                             mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    eng.record("r1", result)

    learning = eng.store.load_task("r1", "t1").learnings[-1]
    assert "AssertionError expected 3 got 2" in learning  # the readable text survives
    assert '"type"' not in learning  # no raw JSONL event noise in the learning tail
