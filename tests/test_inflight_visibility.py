"""#66: surfacing in-flight activity through Engine.status(include_activity=True) and the
``watch --activity`` line (with the distinct stream-stall note).

The probe/tee mechanics are covered in test_stream_probe; here we pin the SURFACING:
status stays lean by default and attaches a lean activity snapshot only when opted in and
only when a live stream exists, and the watch loop renders/flags activity.
"""

from __future__ import annotations

from orchestrator.alerting import activity_lines, watch
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir, stream_filename
from tests.conftest import make_result

_TOOL_USE = (
    '{"type":"assistant","message":{"content":['
    '{"type":"tool_use","name":"Edit","input":{"file_path":"a.py"}}]}}\n'
)


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


def _write_stream(root, task_id, stage_value, attempt, text) -> None:
    d = stages_dir(root, task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / stream_filename(stage_value, attempt)).write_text(text, encoding="utf-8")


# --- Engine.status(include_activity=...) ------------------------------------------------

def test_status_activity_is_opt_in_and_lean(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake (deterministic)
    w = eng.next_work("r1", "t1")  # dispatch the next model stage → task RUNNING on it
    _write_stream(tmp_path, "t1", w.stage.value, w.attempt, _TOOL_USE)

    # default: no activity key, and the snapshot is byte-for-byte the old shape.
    plain = eng.status("r1")
    assert "activity" not in plain["tasks"]["t1"]

    # opt-in: activity attached, lean (no raw tail), with the parsed current activity.
    act = eng.status("r1", include_activity=True)["tasks"]["t1"]["activity"]
    assert act["current_activity"] == {"tool": "Edit", "detail": "a.py"}
    assert act["events_seen"] == 1
    assert "recent_tail" not in act  # status stays small — the tail lives in the `tail` CLI
    assert isinstance(act["seconds_since_event"], float)


def test_status_activity_absent_when_no_stream(tmp_path, project) -> None:
    # A running task whose current stage has NO stream file (interactive/ENGINE lane, or
    # nothing teed yet) simply gets no activity — never a crash, never an empty stub.
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    eng.next_work("r1", "t1")  # RUNNING, but we write no stream
    act = eng.status("r1", include_activity=True)["tasks"]["t1"]
    assert "activity" not in act


def test_status_activity_not_attached_to_terminal_tasks(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    # even if a stale stream file lingers, a completed task carries no activity
    _write_stream(tmp_path, "t1", "review", 0, _TOOL_USE)
    done = eng.status("r1", include_activity=True)["tasks"]["t1"]
    assert done["state"] == "completed"
    assert "activity" not in done


# --- activity_lines (pure) --------------------------------------------------------------

def _running_task(stage, *, since, tool="Bash", detail="uv run pytest", events=7) -> dict:
    return {
        "state": "running", "current_stage": stage, "stale": False,
        "activity": {
            "current_activity": {"tool": tool, "detail": detail},
            "events_seen": events, "seconds_since_event": since, "last_event_at": 1.0,
        },
    }


def test_activity_lines_normal_and_stall_and_skip() -> None:
    status = {"run_id": "r1", "run_state": "running", "tasks": {
        "A": _running_task("implement", since=4.0),
        "B": _running_task("review", since=600.0, tool="Read", detail="x.py"),
        "C": {"state": "running", "current_stage": "scope"},  # no activity → skipped
    }}
    lines = activity_lines(status, stall_after_s=300)
    a_line = next(line for line in lines if line.startswith("[A]"))
    b_line = next(line for line in lines if line.startswith("[B]"))
    assert "implement: Bash: uv run pytest" in a_line and "7 events" in a_line
    assert "last 4.0s ago" in a_line
    assert "STREAM STALLED" in b_line and "600.0s" in b_line and "STREAM STALLED" not in a_line
    assert all(not line.startswith("[C]") for line in lines)


def test_activity_lines_empty_without_activity() -> None:
    assert activity_lines({"tasks": {"A": {"state": "running", "current_stage": "x"}}}) == []


# --- watch --activity loop (fake sleeper, growing → frozen) -----------------------------

class _ScriptedEngine:
    def __init__(self, snapshots: list[dict]) -> None:
        self._snapshots = snapshots
        self.notified: list[tuple[str, dict]] = []
        self.activity_flags: list[bool] = []

    def status(self, run_id: str, *, stale_after_s: int = 1800,
               include_activity: bool = False) -> dict:
        self.activity_flags.append(include_activity)
        return self._snapshots.pop(0)

    def emit_notification(self, run_id: str, kind: str, payload: dict) -> None:
        self.notified.append((kind, payload))


def _snap(tasks: dict, run_state: str = "running") -> dict:
    return {"run_id": "r1", "run_state": run_state, "tasks": tasks}


def test_watch_activity_emits_live_line_then_stall_note() -> None:
    # Poll 1: stream growing (4s). Poll 2: stream frozen (>stall). Poll 3: terminal.
    eng = _ScriptedEngine([
        _snap({"A": _running_task("implement", since=4.0)}),
        _snap({"A": _running_task("implement", since=600.0)}),
        _snap({}, "completed"),
    ])
    slept: list[int] = []
    lines: list[str] = []
    final = watch(eng, "r1", interval=5, sleeper=slept.append, emit=lines.append,
                  activity=True, stall_after_s=300)

    assert final["run_state"] == "completed"
    assert any("implement: Bash: uv run pytest" in line for line in lines)  # live activity line
    assert any("STREAM STALLED" in line for line in lines)  # earlier-than-staleness note
    assert slept == [5, 5]
    assert all(eng.activity_flags)  # watch requested include_activity every poll


def test_watch_without_activity_does_not_request_it() -> None:
    eng = _ScriptedEngine([_snap({}, "completed")])
    watch(eng, "r1", interval=5, sleeper=lambda _s: None, emit=lambda _line: None)
    assert eng.activity_flags == [False]  # opt-in stays off by default
