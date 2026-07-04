"""#56: retain the FULL raw provider stream + stderr per stage.

The old bash system kept each stage's whole ``stream.jsonl`` and full ``.stderr`` on disk;
the rebuild parsed the stream for usage then dropped it (truncating stderr to 500 chars), so
a post-mortem of a weird model call lost its primary evidence. ``stream_teeing_transport``
tees the whole stdout/stderr to files under the run's per-stage log dir and references the
saved paths on the RawResult (→ StageResult → stage-log payload). Best-effort: a tee failure
never breaks the call. Interactive/ENGINE lanes carry no provider stream and are skipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import adapters.execution.transport as T
from adapters.execution.transport import (
    RawResult,
    claude_cli_transport,
    codex_cli_transport,
    stream_teeing_transport,
    to_stage_result,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stream_probe import find_current_stream

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _wi(*, stage=Stage.IMPLEMENT, attempt=0, task_id="#42") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task_id, stage=stage, prompt="p",
        schema_ref="implement", model="m", lane_policy=_HEADLESS, created_at="now",
        attempt=attempt,
    )


def test_tees_full_stdout_and_stderr_and_references_paths(tmp_path) -> None:
    stream = '{"type":"assistant"}\n{"type":"result","usage":{}}\n'
    stderr = "a warning line\nanother\n"

    def inner(work):
        return RawResult({"ok": True}, raw_output=stream, raw_stderr=stderr)

    teed = stream_teeing_transport(inner, tmp_path)
    raw = teed(_wi())

    # paths are recorded RELATIVE to the run root, under the SAME stages/<task>/ dir
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.stream.jsonl"
    assert raw.stream_files["stderr"] == "stages/42/implement-attempt0.stderr.log"
    # and the files hold the FULL, untruncated content (no gzip — plain, greppable)
    assert (tmp_path / raw.stream_files["stream"]).read_text() == stream
    assert (tmp_path / raw.stream_files["stderr"]).read_text() == stderr
    # the model result is untouched by teeing
    assert raw.structured_output == {"ok": True}


def test_stream_files_flow_onto_the_stage_result(tmp_path) -> None:
    def inner(work):
        return RawResult({"ok": True}, raw_output="s\n", raw_stderr="e\n")

    raw = stream_teeing_transport(inner, tmp_path)(_wi())
    result = to_stage_result(_wi(), raw, ResultStatus.SUCCESS,
                             mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    assert result.stream_files == raw.stream_files
    assert result.stream_files["stream"].endswith(".stream.jsonl")


def test_attempts_do_not_collide(tmp_path) -> None:
    def inner(work):
        return RawResult(None, raw_output=f"attempt-{work.attempt}\n", raw_stderr="")

    teed = stream_teeing_transport(inner, tmp_path)
    r0 = teed(_wi(attempt=0))
    r1 = teed(_wi(attempt=1))
    assert r0.stream_files["stream"].endswith("implement-attempt0.stream.jsonl")
    assert r1.stream_files["stream"].endswith("implement-attempt1.stream.jsonl")
    # each attempt's evidence survives independently (no clobber)
    assert (tmp_path / r0.stream_files["stream"]).read_text() == "attempt-0\n"
    assert (tmp_path / r1.stream_files["stream"]).read_text() == "attempt-1\n"


def test_no_provider_stream_skips_cleanly(tmp_path) -> None:
    # a deterministic ENGINE/interactive result carries neither stdout nor stderr.
    def inner(work):
        return RawResult({"passed": True})  # raw_output None, raw_stderr None

    raw = stream_teeing_transport(inner, tmp_path)(_wi(stage=Stage.TEST))
    assert raw.stream_files is None
    assert not (tmp_path / "stages").exists()  # nothing written


def test_tee_failure_never_breaks_the_call(tmp_path, monkeypatch) -> None:
    def inner(work):
        return RawResult({"ok": True}, raw_output="s\n", raw_stderr="e\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(T, "_tee_streams", boom)
    raw = stream_teeing_transport(inner, tmp_path)(_wi())
    # the model result still comes back — retaining evidence is never worth a failed call
    assert raw.structured_output == {"ok": True}
    # and the failure is recorded (not silently swallowed) for the audit trail
    assert "stream tee failed" in raw.stream_files["error"]


def test_none_root_is_a_passthrough(tmp_path) -> None:
    sentinel = RawResult({"ok": True}, raw_output="s\n", raw_stderr="e\n")
    raw = stream_teeing_transport(lambda w: sentinel, None)(_wi())
    assert raw is sentinel and raw.stream_files is None


# --- #70: each schema-retry SUB-CALL keeps its own stream (no clobber within one dispatch) --
# Before #70, every schema-retry sub-call in one dispatch teed to the SAME
# ``<stage>-attempt<N>.stream.jsonl``, so only the FINAL call's stream survived — a post-mortem
# of a retry chain lost what the model originally emitted. Now the first call keeps the bare name
# and each corrective sub-call gets a ``.retry<K>`` file; ``stream_files`` keeps ``stream``/
# ``stderr`` = the final call's files plus ``retries`` for the superseded chain.

# `ok` is required + boolean — the contract the fakes below satisfy or violate.
_SCHEMA = json.dumps({"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}})


def _claude_stream(structured: dict, session: str = "s1") -> str:
    """A minimal claude ``stream-json`` stdout: an init event + the final ``result`` event
    carrying the structured output (what ``_last_result_event`` parses)."""
    return (
        json.dumps({"type": "system", "subtype": "init", "session_id": session}) + "\n"
        + json.dumps({"type": "result", "subtype": "success", "session_id": session,
                      "structured_output": structured, "usage": {}}) + "\n"
    )


def _fake_teed_claude(monkeypatch, calls: list[tuple[str, str]]) -> list[str]:
    """Fake ``_run_teed`` for the claude streaming path: writes each queued ``(stdout, stderr)``
    to its ``tee_path`` (so a per-sub-call stream file exists on disk) and returns it (last
    sticks). Records the tee_path basenames in call order so a test can assert the naming."""
    names: list[str] = []

    def fake(argv, *, timeout, cwd, tee_path, env=None):
        stdout, stderr = calls[min(len(names), len(calls) - 1)]
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(stdout, encoding="utf-8")
        names.append(Path(tee_path).name)
        return 0, stdout, stderr

    monkeypatch.setattr(T, "_run_teed", fake)
    return names


def test_streaming_no_retry_is_a_single_unsuffixed_pair(tmp_path, monkeypatch) -> None:
    # Lock the 99% case: a valid-first-try dispatch tees exactly one stream/stderr pair under
    # the SAME unsuffixed names as before — no ``.retry`` file, no ``retries`` key, no churn.
    _fake_teed_claude(monkeypatch, [(_claude_stream({"ok": True}), "a warning\n")])
    raw = claude_cli_transport(lambda ref: _SCHEMA, run_log_root=tmp_path)(_wi())

    assert raw.schema_retries == 0
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.stream.jsonl"
    assert raw.stream_files["stderr"] == "stages/42/implement-attempt0.stderr.log"
    assert "retries" not in raw.stream_files
    assert not list((tmp_path / "stages" / "42").glob("*.retry*"))  # no retry file written


def test_streaming_invalid_then_valid_retains_both_sub_call_streams(tmp_path, monkeypatch) -> None:
    _fake_teed_claude(monkeypatch, [
        (_claude_stream({"wrong": 1}), ""),   # call 1: invalid (missing required `ok`)
        (_claude_stream({"ok": True}), ""),   # call 2 (retry1): corrected
    ])
    raw = claude_cli_transport(lambda ref: _SCHEMA, run_log_root=tmp_path)(_wi())

    assert raw.schema_retries == 1 and raw.structured_output == {"ok": True}
    # primary = the FINAL call's file; the superseded first call rides ``retries``.
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.retry1.stream.jsonl"
    assert [p["stream"] for p in raw.stream_files["retries"]] == [
        "stages/42/implement-attempt0.stream.jsonl"
    ]
    # the two files hold the two DISTINCT sub-call streams (no clobber)
    base = tmp_path / "stages" / "42" / "implement-attempt0.stream.jsonl"
    retry1 = tmp_path / "stages" / "42" / "implement-attempt0.retry1.stream.jsonl"
    assert '"wrong"' in base.read_text() and '"ok"' in retry1.read_text()


def test_streaming_two_retries_retains_three_sub_call_streams(tmp_path, monkeypatch) -> None:
    _fake_teed_claude(monkeypatch, [
        (_claude_stream({"wrong": 1}), ""),
        (_claude_stream({"still": "bad"}), ""),
        (_claude_stream({"ok": True}), ""),
    ])
    raw = claude_cli_transport(
        lambda ref: _SCHEMA, max_schema_retries=2, run_log_root=tmp_path
    )(_wi())

    assert raw.schema_retries == 2
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.retry2.stream.jsonl"
    assert [p["stream"] for p in raw.stream_files["retries"]] == [
        "stages/42/implement-attempt0.stream.jsonl",
        "stages/42/implement-attempt0.retry1.stream.jsonl",
    ]
    # three distinct stream files on disk — the whole chain's evidence survives
    on_disk = sorted(p.name for p in (tmp_path / "stages" / "42").glob("*.stream.jsonl"))
    assert on_disk == [
        "implement-attempt0.retry1.stream.jsonl",
        "implement-attempt0.retry2.stream.jsonl",
        "implement-attempt0.stream.jsonl",
    ]


def test_probe_picks_the_highest_retry_suffix_live_file(tmp_path, monkeypatch) -> None:
    # ``orchestrator tail`` / the probe must follow the LATEST live sub-call, i.e. the highest
    # ``.retry<K>`` file, not the superseded base file.
    _fake_teed_claude(monkeypatch, [
        (_claude_stream({"wrong": 1}), ""),
        (_claude_stream({"ok": True}), ""),
    ])
    claude_cli_transport(lambda ref: _SCHEMA, run_log_root=tmp_path)(_wi())

    found = find_current_stream(tmp_path, "#42", "implement")
    assert found.name == "implement-attempt0.retry1.stream.jsonl"


def test_streaming_retry_tee_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    # A tee hiccup on the RETRY sub-call (its stream file couldn't be written) must not break the
    # call — ``_run_teed`` keeps capturing and returns the stream, so the dispatch still succeeds
    # and the retry path is still recorded (never-break guarantee holds on retries too).
    streams = [_claude_stream({"wrong": 1}), _claude_stream({"ok": True})]
    names: list[str] = []

    def fake(argv, *, timeout, cwd, tee_path, env=None):
        i = len(names)
        names.append(Path(tee_path).name)
        if i == 0:  # first call tees fine
            Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
            Path(tee_path).write_text(streams[0], encoding="utf-8")
        # retry sub-call: simulate a tee that could not be written (no file), still return stdout
        return 0, streams[min(i, len(streams) - 1)], ""

    monkeypatch.setattr(T, "_run_teed", fake)
    raw = claude_cli_transport(lambda ref: _SCHEMA, run_log_root=tmp_path)(_wi())

    assert raw.structured_output == {"ok": True} and raw.schema_retries == 1
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.retry1.stream.jsonl"
    assert not (tmp_path / "stages" / "42" / "implement-attempt0.retry1.stream.jsonl").exists()


# --- codex parity: the same per-sub-call retention on the codex streaming lane --------------

def _codex_wi() -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="gpt-5-codex", lane_policy=_CODEX, created_at="now",
    )


def _codex_events(thread: str = "th-1") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]) + "\n"


def test_codex_streaming_retry_retains_both_sub_call_streams(tmp_path, monkeypatch) -> None:
    payloads = [
        {"structured": {"wrong": 1}, "stdout": _codex_events()},  # call 1: invalid
        {"structured": {"ok": True}, "stdout": _codex_events()},  # call 2 (retry1): corrected
    ]
    names: list[str] = []

    def fake(argv, *, timeout, cwd, tee_path, env=None):
        p = payloads[min(len(names), len(payloads) - 1)]
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(p["stdout"], encoding="utf-8")
        # real codex writes the structured output to the --output-last-message file
        if "--output-last-message" in argv and p.get("structured") is not None:
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps(p["structured"]), encoding="utf-8"
            )
        names.append(Path(tee_path).name)
        return 0, p["stdout"], ""

    monkeypatch.setattr(T, "_run_teed", fake)
    raw = codex_cli_transport(lambda ref: _SCHEMA, run_log_root=tmp_path)(_codex_wi())

    assert raw.schema_retries == 1 and raw.structured_output == {"ok": True}
    assert raw.stream_files["stream"] == "stages/t1/implement-attempt0.retry1.stream.jsonl"
    assert [p["stream"] for p in raw.stream_files["retries"]] == [
        "stages/t1/implement-attempt0.stream.jsonl"
    ]
    assert names == [  # the tee wrote a distinct file per sub-call
        "implement-attempt0.stream.jsonl", "implement-attempt0.retry1.stream.jsonl"
    ]
