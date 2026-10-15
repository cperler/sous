"""#556: a headless stage that already printed its ``result`` must not run on to the timeout.

``claude -p`` stays alive after its terminal ``result`` event while the agent has background
shells / Monitors outstanding (and re-invokes the model when they report), so the harness used
to wait for an exit that never came and record a succeeded stage as a TIMEOUT. These tests drive
``_run_teed`` with real (fast) fake subprocesses: one that answers and then lingers holding a
child on stdout, one that wakes up and answers again, the resume preamble that must NOT arm the
grace, and the timeout/EOF edges.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from adapters.execution import transport as T
from adapters.execution.transport import (
    RawResult,
    _schema_retry_loop,
    _settles_claude_stage,
    classify_raw,
    claude_cli_transport,
)
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem


def _result(**kw: object) -> str:
    ev: dict[str, object] = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 12,
        "session_id": "sess-556", "usage": {"input_tokens": 7, "output_tokens": 3},
        "structured_output": {"committed": True},
    }
    ev.update(kw)
    return json.dumps(ev)


_PREAMBLE = _result(num_turns=0, result="", structured_output=None)
_PREAMBLE = json.dumps({k: v for k, v in json.loads(_PREAMBLE).items()
                        if k != "structured_output"})


@pytest.fixture(autouse=True)
def _fast_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "POST_RESULT_GRACE_S", 0.3)
    monkeypatch.setattr(T, "_TERM_GRACE_S", 1.0)
    monkeypatch.setattr(T, "_EOF_DRAIN_S", 0.3)


def _script(body: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(body)]


def _dead(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _linger_script(pid_file: Path, *lines: str) -> list[str]:
    """Print ``lines``, spawn a child that INHERITS stdout (the backgrounded pytest), record its
    pid, then idle as the CLI does while it waits on that child."""
    printed = "".join(f"print({line!r}, flush=True)\n" for line in lines)
    return _script(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        + printed
        + "time.sleep(60)\n"
    )


def test_lingering_process_is_terminated_after_its_result_and_its_child_too(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    notices: list[dict[str, object]] = []
    started = time.monotonic()
    rc, out, _ = T._run_teed(
        _linger_script(pid_file, '{"type":"system","subtype":"init"}', _result()),
        timeout=30, cwd=None, tee_path=tmp_path / "s.jsonl",
        settle=_settles_claude_stage, notices=notices,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10, "must not wait out the 30s timeout once the result is in"
    assert _result() in out  # the answer is kept
    assert (tmp_path / "s.jsonl").read_text().splitlines()[-1] == _result()  # and teed
    assert rc != 0  # the raw returncode is the kill's; the transport interprets it
    [notice] = notices
    assert notice["notice"] == "provider_terminated_after_result"
    assert notice["reason"] == "idle_after_result"
    assert notice["result_events"] == 1
    assert int(notice["descendants_signalled"]) >= 1  # type: ignore[call-overload]
    assert _dead(int(pid_file.read_text())), "the background child holding stdout must die"


def test_zero_turn_resume_preamble_does_not_arm_the_grace(tmp_path) -> None:
    # A resumed session prints an empty zero-turn `result` FIRST (flushed by the previous
    # session's leftover task notification). Arming on it would kill the stage before its work.
    script = _script(f"""
        import time
        print({_PREAMBLE!r}, flush=True)
        time.sleep(1.0)  # >> the 0.3s grace: real work, silent on stdout
        print({_result()!r}, flush=True)
    """)
    notices: list[dict[str, object]] = []
    rc, out, _ = T._run_teed(script, timeout=30, cwd=None, tee_path=tmp_path / "s.jsonl",
                             settle=_settles_claude_stage, notices=notices)
    assert rc == 0 and notices == []
    assert out.splitlines() == [_PREAMBLE, _result()]


def test_settle_predicate_shapes() -> None:
    assert not _settles_claude_stage(_PREAMBLE)
    assert _settles_claude_stage(_result())
    assert _settles_claude_stage(_result(num_turns=0))  # structured_output present
    assert _settles_claude_stage(json.dumps({"type": "result", "is_error": True, "num_turns": 0}))
    assert not _settles_claude_stage('{"type":"assistant","message":{"content":"result"}}')
    assert not _settles_claude_stage("not json but mentions \"result\"")


def test_a_wake_up_turn_is_left_to_finish_and_its_later_result_is_kept(tmp_path) -> None:
    # The CLI wakes on a background-task notification and streams a new turn. Output activity
    # keeps resetting the grace, so the turn completes and its fresh `result` is captured.
    later = _result(num_turns=3, structured_output={"committed": True, "note": "suite done"})
    script = _script(f"""
        import time
        print({_result()!r}, flush=True)
        for _ in range(8):  # 8 x 0.1s of wake-up events: never 0.3s idle
            time.sleep(0.1)
            print('{{"type":"system","subtype":"task_updated"}}', flush=True)
        print({later!r}, flush=True)
        time.sleep(60)
    """)
    notices: list[dict[str, object]] = []
    started = time.monotonic()
    rc, out, _ = T._run_teed(script, timeout=30, cwd=None, tee_path=tmp_path / "s.jsonl",
                             settle=_settles_claude_stage, notices=notices)
    assert time.monotonic() - started < 10
    assert out.splitlines()[-1] == later
    assert T._last_result_event(out)["structured_output"]["note"] == "suite done"  # type: ignore[index]
    [notice] = notices
    assert notice["result_events"] == 2


def test_timeout_after_a_result_returns_the_result_instead_of_raising(tmp_path) -> None:
    # Still busy (never idle) when the stage timeout fires — but it has already answered.
    script = _script(f"""
        import time
        print({_result()!r}, flush=True)
        while True:
            time.sleep(0.05)
            print('{{"type":"system","subtype":"task_updated"}}', flush=True)
    """)
    notices: list[dict[str, object]] = []
    _, out, _ = T._run_teed(script, timeout=1.0, cwd=None, tee_path=tmp_path / "s.jsonl",
                            settle=_settles_claude_stage, notices=notices)
    assert _result() in out
    assert [n["reason"] for n in notices] == ["timeout_after_result"]


def test_timeout_without_a_result_still_raises_and_kills_the_tree(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        T._run_teed(_linger_script(pid_file, '{"type":"system","subtype":"init"}'),
                    timeout=1.0, cwd=None, tee_path=tmp_path / "s.jsonl",
                    settle=_settles_claude_stage, notices=[])
    assert "init" in str(exc.value.output)  # #319: partial stdout still rides the exception
    assert _dead(int(pid_file.read_text()))


def test_without_settle_a_lingering_result_is_not_terminated(tmp_path) -> None:
    # Codex (and any caller that passes no predicate) keeps the pre-#556 behavior: the result
    # line alone does not end the call; only the timeout does.
    with pytest.raises(subprocess.TimeoutExpired):
        T._run_teed(_linger_script(tmp_path / "c.pid", _result()), timeout=1.0, cwd=None,
                    tee_path=tmp_path / "s.jsonl")
    assert _dead(int((tmp_path / "c.pid").read_text()))


def test_an_orphan_holding_stdout_after_exit_cannot_wedge_the_call(tmp_path) -> None:
    # The process exits, but a detached grandchild keeps the pipe open, so EOF never comes.
    # Before #556 the read loop blocked forever (the watchdog only killed the dead parent).
    pid_file = tmp_path / "orphan.pid"
    script = _script(f"""
        import subprocess, sys
        c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                             start_new_session=True)
        open({str(pid_file)!r}, 'w').write(str(c.pid))
        print('done', flush=True)
    """)
    notices: list[dict[str, object]] = []
    try:
        started = time.monotonic()
        rc, out, _ = T._run_teed(script, timeout=30, cwd=None, tee_path=tmp_path / "s.jsonl",
                                 notices=notices)
        assert time.monotonic() - started < 10
        assert rc == 0 and out == "done\n"
        assert [n["notice"] for n in notices] == ["provider_stdout_held_after_exit"]
    finally:
        os.kill(int(pid_file.read_text()), 9)


def test_descendant_snapshot_never_includes_the_harness() -> None:
    # The harness is the provider's parent, never its descendant — and even asked about its
    # own parent's tree, the snapshot must not name this process.
    assert os.getpid() not in T._descendant_pids(os.getppid())


# --- the claude transport end to end -----------------------------------------------------

def _wi() -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#42", stage=Stage.IMPLEMENT, prompt="p",
        schema_ref="implement", model="m", created_at="now", attempt=0,
        lane_policy=LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE),
    ).model_copy(update={"timeout_s": 30})


def _route_teed(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    real = T._run_teed
    monkeypatch.setattr(T, "_run_teed", lambda _argv, **kw: real(argv, **kw))


def test_claude_transport_records_a_lingering_answered_stage_as_success(
    tmp_path, monkeypatch
) -> None:
    _route_teed(monkeypatch, _linger_script(tmp_path / "c.pid", _result()))
    raw = claude_cli_transport(run_log_root=tmp_path / "run")(_wi())
    assert classify_raw(raw) is ResultStatus.SUCCESS
    assert raw.exit_code == 0 and raw.error is None
    assert raw.structured_output == {"committed": True}
    assert raw.session_ref == "sess-556"
    assert raw.usage.input == 7 and raw.usage.output == 3  # spend still recorded
    assert [n["notice"] for n in raw.execution_notices] == ["provider_terminated_after_result"]


def test_claude_transport_keeps_an_error_result_an_error_after_termination(
    tmp_path, monkeypatch
) -> None:
    # A lingering `is_error` envelope (the CLI's own limit notice) must still classify as the
    # rate limit it is, not be laundered into a success by the kill's rewritten exit code.
    limit = _result(is_error=True, subtype="error_during_execution", num_turns=1,
                    result="You've hit your session limit · resets 3:50pm")
    limit = json.dumps({k: v for k, v in json.loads(limit).items()
                        if k != "structured_output"})
    _route_teed(monkeypatch, _linger_script(tmp_path / "c.pid", limit))
    raw = claude_cli_transport(run_log_root=tmp_path / "run")(_wi())
    assert raw.exit_code == 1
    assert classify_raw(raw) is ResultStatus.RATE_LIMITED
    assert raw.execution_notices


def test_schema_retry_keeps_a_superseded_sub_calls_notices() -> None:
    notice = {"notice": "provider_terminated_after_result"}
    first = RawResult({"wrong": 1}, execution_notices=(notice,), session_ref="s")
    schema = json.dumps({"type": "object", "required": ["committed"],
                         "properties": {"committed": {"type": "boolean"}}})

    def call(work: WorkItem, session_ref: str | None, retry: int) -> RawResult:
        return RawResult({"committed": True})

    raw = _schema_retry_loop(first, _wi(), schema, 2, call)
    assert raw.structured_output == {"committed": True}
    assert raw.execution_notices == (notice,)
