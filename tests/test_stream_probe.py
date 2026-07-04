"""#66: in-flight stream sensing + the streaming (in-flight) tee.

``orchestrator.stream_probe`` turns a partially-written headless stream file into a cheap
live snapshot (events seen, current activity, tail); ``adapters.execution.transport``'s
``_run_teed`` streams the provider's stdout to that file LINE-BY-LINE as it arrives (so an
in-flight stage is tailable) instead of #56's write-after-the-call. These tests pin both.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import adapters.execution.transport as T
from adapters.execution.transport import RawResult, claude_cli_transport, stream_teeing_transport
from orchestrator.schemas.enums import ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stream_probe import (
    find_current_stream,
    follow_stream,
    probe_stream,
    read_tail,
    stages_dir,
    stream_basename,
    stream_filename,
    stream_relpath,
)

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _wi(*, stage=Stage.IMPLEMENT, attempt=0, task_id="#42") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id=task_id, stage=stage, prompt="p",
        schema_ref="implement", model="m", lane_policy=_HEADLESS, created_at="now",
        attempt=attempt,
    )


# --- the probe --------------------------------------------------------------------------

_ASSISTANT_BASH = (
    '{"type":"assistant","message":{"content":['
    '{"type":"text","text":"let me run the tests"},'
    '{"type":"tool_use","name":"Bash","input":{"command":"uv run pytest -q"}}]}}'
)
_ASSISTANT_EDIT = (
    '{"type":"assistant","message":{"content":['
    '{"type":"tool_use","name":"Edit","input":{"file_path":"orchestrator/engine.py"}}]}}'
)


def test_probe_complete_stream_counts_events_and_reads_activity(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"type":"system","subtype":"init","session_id":"s1"}\n'
        + _ASSISTANT_BASH + "\n" + _ASSISTANT_EDIT + "\n"
        + '{"type":"result","subtype":"success","result":"{}"}\n'
    )
    snap = probe_stream(p)
    assert snap["events_seen"] == 4
    # current_activity is the LAST tool_use (the Edit), with its key arg.
    assert snap["current_activity"] == {"tool": "Edit", "detail": "orchestrator/engine.py"}
    assert isinstance(snap["last_event_at"], float)
    assert snap["recent_tail"][-1].startswith('{"type":"result"')


def test_probe_tolerates_a_partial_trailing_line(tmp_path) -> None:
    # An in-progress write: the last line is a truncated JSON object (no newline, unbalanced).
    p = tmp_path / "s.jsonl"
    p.write_text(_ASSISTANT_BASH + "\n" + '{"type":"assistant","message":{"content":[{"type":"too')
    snap = probe_stream(p)
    assert snap["events_seen"] == 1  # only the complete line counts; the partial is skipped
    assert snap["current_activity"] == {"tool": "Bash", "detail": "uv run pytest -q"}
    assert len(snap["recent_tail"]) == 2  # the raw tail still includes the partial line


def test_probe_empty_file_is_a_zero_snapshot(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("")
    snap = probe_stream(p)
    assert snap["events_seen"] == 0
    assert snap["current_activity"] is None
    assert snap["recent_tail"] == []
    assert isinstance(snap["last_event_at"], float)


def test_probe_missing_file_is_none(tmp_path) -> None:
    assert probe_stream(tmp_path / "nope.jsonl") is None


def test_probe_tail_and_lines_are_bounded(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    huge = "x" * 5000
    p.write_text(
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        f'"input":{{"command":"{huge}"}}}}]}}}}\n'
    )
    snap = probe_stream(p, tail_lines=5, max_line_chars=100)
    # extracted detail is bounded (<= 200), and the raw tail line is bounded to max_line_chars
    assert len(snap["current_activity"]["detail"]) <= 200
    assert len(snap["recent_tail"][0]) == 100


def test_probe_tail_lines_zero_omits_tail(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(_ASSISTANT_BASH + "\n")
    snap = probe_stream(p, tail_lines=0)
    assert snap["recent_tail"] == []
    assert snap["events_seen"] == 1  # still counts events + activity


def test_probe_codex_msg_shape_and_item_shape(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"id":"1","msg":{"type":"exec_command_begin","command":"pytest"}}\n'
        '{"type":"item.completed","item":{"type":"command_execution","command":"git commit"}}\n'
    )
    snap = probe_stream(p)
    assert snap["events_seen"] == 2
    # last recognized activity is the item.* command_execution
    assert snap["current_activity"] == {"tool": "command_execution", "detail": "git commit"}


def test_probe_unrecognized_events_degrade_to_count_and_tail(tmp_path) -> None:
    # An unknown/opaque event shape (codex degraded mode): still counted + tailed, no activity.
    p = tmp_path / "s.jsonl"
    p.write_text('{"foo":"bar"}\n{"baz":1}\n')
    snap = probe_stream(p)
    assert snap["events_seen"] == 2
    assert snap["current_activity"] is None
    assert len(snap["recent_tail"]) == 2


# --- naming + find + tail helpers -------------------------------------------------------

def test_stream_relpath_matches_the_tee_naming() -> None:
    assert stream_relpath("#42", "implement", 0) == "stages/42/implement-attempt0.stream.jsonl"
    assert stream_relpath("#42", "review", 2) == "stages/42/review-attempt2.stream.jsonl"


def test_stream_basename_appends_a_retry_suffix_only_when_retrying() -> None:
    # #70: the first call (retry 0) keeps the bare name; a schema-retry sub-call gets .retry<K>.
    assert stream_basename("implement", 0) == "implement-attempt0"
    assert stream_basename("implement", 0, 0) == "implement-attempt0"  # explicit 0 == no suffix
    assert stream_basename("implement", 0, 1) == "implement-attempt0.retry1"
    assert stream_basename("review", 2, 3) == "review-attempt2.retry3"
    assert stream_relpath("#42", "implement", 0, 1) == \
        "stages/42/implement-attempt0.retry1.stream.jsonl"


def test_find_current_stream_prefers_highest_retry_within_an_attempt(tmp_path) -> None:
    # #70: with a base + retry sub-call file present, the probe/tail follows the newest sub-call.
    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    (d / stream_filename("implement", 0)).write_text("a\n")
    (d / stream_filename("implement", 0, 1)).write_text("b\n")
    (d / stream_filename("implement", 0, 2)).write_text("c\n")
    assert find_current_stream(tmp_path, "#42", "implement").name == \
        "implement-attempt0.retry2.stream.jsonl"


def test_find_current_stream_newer_attempt_beats_a_prior_attempts_retry(tmp_path) -> None:
    # A higher ATTEMPT still wins over a prior attempt's retry sub-call (attempt sorts first).
    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    (d / stream_filename("implement", 0, 2)).write_text("old-retry\n")
    (d / stream_filename("implement", 1)).write_text("new-attempt\n")
    assert find_current_stream(tmp_path, "#42", "implement").name == \
        "implement-attempt1.stream.jsonl"


def test_find_current_stream_prefers_highest_attempt_for_a_stage(tmp_path) -> None:
    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    (d / stream_filename("implement", 0)).write_text("a\n")
    (d / stream_filename("implement", 1)).write_text("b\n")
    found = find_current_stream(tmp_path, "#42", "implement")
    assert found.name == "implement-attempt1.stream.jsonl"


def test_find_current_stream_without_stage_picks_most_recent(tmp_path) -> None:
    d = stages_dir(tmp_path, "#42")
    d.mkdir(parents=True)
    old = d / stream_filename("scope", 0)
    old.write_text("a\n")
    new = d / stream_filename("implement", 0)
    new.write_text("b\n")
    # make `new` unambiguously newer
    import os
    os.utime(old, (1, 1))
    assert find_current_stream(tmp_path, "#42").name == "implement-attempt0.stream.jsonl"


def test_find_current_stream_none_when_no_dir_or_no_files(tmp_path) -> None:
    assert find_current_stream(tmp_path, "#99") is None
    stages_dir(tmp_path, "#99").mkdir(parents=True)
    assert find_current_stream(tmp_path, "#99") is None  # dir exists but empty


def test_read_tail_bounds_and_missing(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("l1\nl2\nl3\nl4\n")
    assert read_tail(p, lines=2) == ["l3", "l4"]
    assert read_tail(tmp_path / "gone.jsonl") is None


def test_follow_stream_prints_initial_tail_then_new_lines(tmp_path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("a\nb\n")
    lines: list[str] = []
    calls = {"n": 0}

    def sleeper(_interval: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # grow the file on the first poll only
            with p.open("a", encoding="utf-8") as fh:
                fh.write("c\nd\n")

    follow_stream(p, emit=lines.append, sleeper=sleeper, lines=1, poll_interval=0.0, max_polls=2)
    assert lines[0] == "b"  # initial tail = last 1 line
    assert lines[1:] == ["c", "d"]  # the appended lines, once


# --- the streaming (in-flight) tee: _run_teed -------------------------------------------

def test_run_teed_streams_bytes_before_the_subprocess_completes(tmp_path) -> None:
    tee = tmp_path / "s.jsonl"
    # print a line, flush, then sleep — so the first line is on disk well before exit.
    script = "import sys,time; print('first', flush=True); time.sleep(0.8); print('second')"
    box: dict = {}

    def go() -> None:
        box["rc"], box["out"], box["err"] = T._run_teed(
            [sys.executable, "-c", script], timeout=10, cwd=None, tee_path=tee
        )

    t = threading.Thread(target=go)
    t.start()
    saw_early = False
    deadline = time.time() + 4
    while time.time() < deadline:
        if tee.exists() and "first" in tee.read_text() and t.is_alive():
            saw_early = True  # bytes present in the file WHILE the subprocess still runs
            break
        time.sleep(0.02)
    t.join(timeout=6)
    assert saw_early, "streamed bytes should appear before the subprocess completes"
    assert box["rc"] == 0
    assert box["out"].splitlines() == ["first", "second"]  # full stdout captured too
    assert tee.read_text().splitlines() == ["first", "second"]  # and fully teed


def test_run_teed_raises_timeout_and_kills_a_hang(tmp_path) -> None:
    script = "import time; time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        T._run_teed([sys.executable, "-c", script], timeout=0.3, cwd=None,
                    tee_path=tmp_path / "s.jsonl")


def test_run_teed_captures_stderr(tmp_path) -> None:
    script = "import sys; print('out'); print('err', file=sys.stderr)"
    rc, out, err = T._run_teed([sys.executable, "-c", script], timeout=10, cwd=None,
                               tee_path=tmp_path / "s.jsonl")
    assert rc == 0 and out.strip() == "out" and err.strip() == "err"


# --- the claude transport streaming path ------------------------------------------------

_STREAM_JSON = (
    '{"type":"system","subtype":"init","session_id":"sess-1"}\n'
    + _ASSISTANT_BASH + "\n"
    + '{"type":"result","subtype":"success","session_id":"sess-1",'
    '"usage":{"input_tokens":10,"output_tokens":5},"result":"{\\"committed\\": true}"}\n'
)


def test_claude_streaming_path_uses_stream_json_and_parses_result_event(
    tmp_path, monkeypatch
) -> None:
    seen: dict = {}

    def fake_run_teed(argv, *, timeout, cwd, tee_path, env=None):
        seen["argv"] = argv
        seen["env"] = env
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(_STREAM_JSON, encoding="utf-8")
        return 0, _STREAM_JSON, ""

    monkeypatch.setattr(T, "_run_teed", fake_run_teed)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert "stream-json" in seen["argv"] and "--verbose" in seen["argv"]
    assert raw.structured_output == {"committed": True}  # recovered from the result event
    assert raw.session_ref == "sess-1"
    assert raw.usage.input == 10 and raw.usage.output == 5
    assert raw.stream_files["stream"] == "stages/42/implement-attempt0.stream.jsonl"


def test_claude_streaming_path_no_result_event_is_a_transport_error(tmp_path, monkeypatch) -> None:
    partial = _ASSISTANT_BASH + "\n"  # a killed stream: assistant turns but no result event

    def fake_run_teed(argv, *, timeout, cwd, tee_path, env=None):
        Path(tee_path).parent.mkdir(parents=True, exist_ok=True)
        Path(tee_path).write_text(partial, encoding="utf-8")
        return 0, partial, ""

    monkeypatch.setattr(T, "_run_teed", fake_run_teed)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())
    assert raw.structured_output is None
    assert "no result event" in raw.error
    assert raw.stream_files["stream"].endswith(".stream.jsonl")  # evidence still stamped


def test_claude_default_path_stays_single_shot_json(monkeypatch) -> None:
    # No run_log_root → the proven --output-format json path (subprocess.run), unchanged.
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout='{"structured_output":{"ok":true}}',
                                           stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = claude_cli_transport()(_wi())
    assert "json" in seen["argv"] and "stream-json" not in seen["argv"]
    assert raw.structured_output == {"ok": True}
    assert raw.stream_files is None


def test_stream_teeing_wrapper_passes_through_an_already_streamed_result(tmp_path) -> None:
    # The real transport streamed live and stamped stream_files; the wrapper must NOT re-tee.
    def inner(work):
        return RawResult({"ok": True}, raw_output="x\n",
                         stream_files={"stream": "stages/42/implement-attempt0.stream.jsonl"})

    raw = stream_teeing_transport(inner, tmp_path)(_wi())
    assert raw.stream_files == {"stream": "stages/42/implement-attempt0.stream.jsonl"}
    assert not (tmp_path / "stages").exists()  # wrapper did not write anything
