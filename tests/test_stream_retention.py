"""#56: retain the FULL raw provider stream + stderr per stage.

The old bash system kept each stage's whole ``stream.jsonl`` and full ``.stderr`` on disk;
the rebuild parsed the stream for usage then dropped it (truncating stderr to 500 chars), so
a post-mortem of a weird model call lost its primary evidence. ``stream_teeing_transport``
tees the whole stdout/stderr to files under the run's per-stage log dir and references the
saved paths on the RawResult (→ StageResult → stage-log payload). Best-effort: a tee failure
never breaks the call. Interactive/ENGINE lanes carry no provider stream and are skipped.
"""

from __future__ import annotations

import adapters.execution.transport as T
from adapters.execution.transport import (
    RawResult,
    stream_teeing_transport,
    to_stage_result,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


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
