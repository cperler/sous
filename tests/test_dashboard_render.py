"""Console board rendering (#525): golden layouts plus the rules behind them.

The golden tests render the mid-run and finished fixtures at both widths and compare the
WHOLE board against the expected text inline, so a reviewer reads the layout here instead of
reconstructing it from substring asserts. The fixtures are real runs (Engine + FakeProject
in a ``runs/<id>/`` store root); only what varies between machines or with routing and
pricing defaults is pinned afterwards — the tmp path, the clock, model ids, the ledger, and
the recent-event list (whose sentences ``test_dashboard_snapshot_fields`` already covers).
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

from orchestrator import dashboard
from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import dashboard_snapshot, render_dashboard, render_watch
from orchestrator.dashboard_render import WIDE_MIN_COLUMNS, console_width
from orchestrator.engine import Engine
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir
from tests.conftest import FakeProject, make_result

NOW = "2026-09-30T12:00:00+00:00"
WIDE = 120
COMPACT = 80


# --- fixtures -------------------------------------------------------------------------


def _engine(run_root) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    project = FakeProject()
    project.task_source.issue_url = (  # type: ignore[attr-defined]
        lambda tid: f"https://github.com/x/y/issues/{tid}"
    )
    return Engine(StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), project)


def _factory(run_root, project_ref=None) -> Engine:
    return _engine(run_root)


def _intake(eng, run_id, task_id, **kw) -> None:
    eng.add_task(run_id, task_id, **kw)
    eng.record(run_id, make_result(eng.next_work(run_id, task_id)))


def _ledger(run_root, run_id: str, rows: list[tuple]) -> None:
    """Replace the run's ledger with fixed rows: (task, stage, cost, metered)."""
    (run_root / "stage-costs.jsonl").write_text("".join(
        json.dumps({"run_id": run_id, "task_id": t, "stage": s, "cost_usd": c,
                    "metered": m, "input_tokens": 1500, "output_tokens": 300}) + "\n"
        for t, s, c, m in rows
    ))


def _mid_run(tmp_path):
    """t1 running SCOPE with a live stream, t2 blocked at the human gate after SCOPE, t3
    waiting on t1. t2's SCOPE ran twice and the retry went unmetered."""
    rr = tmp_path / "mid"
    eng = _engine(rr)
    eng.create_run("mid")
    _intake(eng, "mid", "t1")
    assert eng.next_work("mid", "t1").stage is Stage.SCOPE
    d = stages_dir(rr, "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scope-attempt0.stream.jsonl").write_text(json.dumps({
        "type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]},
    }) + "\n")
    _intake(eng, "mid", "t2")
    eng.record("mid", make_result(eng.next_work("mid", "t2"), structured_output={
        "feasible": False, "blocked_reason": "needs an API that does not exist", "plan": []}))
    eng.add_task("mid", "t3", depends_on=["t1"])
    _ledger(rr, "mid", [
        ("t1", "intake", 0.0, True),
        ("t2", "intake", 0.0, True),
        ("t2", "scope", 0.42, True),
        ("t2", "scope", 0.0, False),
    ])
    return rr


def _finished_run(tmp_path):
    rr = tmp_path / "done"
    eng = _engine(rr)
    eng.create_run("done")
    eng.add_task("done", "t1")
    while (w := eng.next_work("done", "t1")) is not None:
        eng.record("done", make_result(w))
    _ledger(rr, "done", [
        ("t1", "intake", 0.0, True),
        ("t1", "scope", 0.31, True),
        ("t1", "implement", 1.90, True),
        ("t1", "test", 0.12, True),
        ("t1", "deliver", 0.20, True),
        ("t1", "review", 0.55, True),
    ])
    return rr


_RECENT = {
    "mid": [
        ("t1: started SCOPE on claude-opus-5-5", 300),
        ("t2: finished SCOPE ($0.42)", 240),
        ("t2: blocked at SCOPE, needs a human decision (needs an API that does not exist)",
         240),
    ],
    "done": [
        ("t1: finished REVIEW ($0.55)", 7400),
        ("t1: completed, PR https://github.com/x/y/pull/1234", 7300),
        ("run finished (completed)", 7300),
    ],
}


def _pinned(tmp_path) -> dict:
    """The snapshot of every run under ``tmp_path`` with the machine- and default-dependent
    values fixed, so the rendered board is byte-stable."""
    snap = dashboard_snapshot(
        tmp_path,
        engine_factory=_factory,
        clock=time.time,
        usage_reader=lambda: {"five_hour_pct": 42.0, "seven_day_pct": 17.0},
    )
    text = json.dumps(snap).replace(str(tmp_path), "/runs")
    snap = json.loads(re.sub(r"claude-[a-z]+-\d+(?:-\d+)*", "claude-opus-5-5", text))
    snap["header"]["generated_at"] = NOW
    now = datetime.fromisoformat(NOW).timestamp()
    for row in snap["runs"]:
        row["last_event_age_s"] = 240.0 if row["run_id"] == "mid" else 7300.0
        for task in row["tasks"]:
            if task["stage_age_s"] is not None:
                task["stage_age_s"] = 754.0
            if task["activity"]["seconds_since_event"] is not None:
                task["activity"]["seconds_since_event"] = 3.0
        row["recent_events"] = [
            {"ts": datetime.fromtimestamp(now - ago, tz=UTC).isoformat(),
             "sentence": sentence}
            for sentence, ago in _RECENT[row["run_id"]]
        ]
    return snap


def _board(snap, width) -> str:
    return render_dashboard(snap, width=width)


# --- golden boards --------------------------------------------------------------------


MID_WIDE = """\
ATTENTION — 1 item needs you  ·  1 run: 1 running

── needs you ──
  ! mid t2  BLOCKED at SCOPE, needs your decision — needs an API that does not exist
      approve — release the gate and let the run continue
        orchestrator --root /runs/mid --run mid approve --task t2 --by "$USER" --note scope_not_feasible_held
      reject — close the task as infeasible (fill in the reason)
        orchestrator --root /runs/mid --run mid reject --task t2 --by "$USER" --reason '<why>'
      abandon — the dispatch is dead; finalize the task (fill in the reason)
        orchestrator --root /runs/mid --run mid abandon --task t2 --reason '<why>'

── running now ──
  mid (fake) — driver: no headless driver (driven task by task from the CLI, or not started)
    t1  Fake t1  —  SCOPE for 12m · opus-5-5 · stream active: Bash: pytest -q

── progress ──
  fake
    mid  running · 0/3 done · 1 needs you · last event 4m ago
      t1  running    Fake t1
          SCOPE running · IMPLEMENT next · TEST - · DELIVER - · REVIEW -
      t2  needs you  Fake t2 — waiting on your decision
          SCOPE done · IMPLEMENT next · TEST - · DELIVER - · REVIEW -
      t3  queued     Fake t3 — waiting on t1
          SCOPE next · IMPLEMENT - · TEST - · DELIVER - · REVIEW -
  legend: - not started

── cost ──
  spend: ≥$0.4200 (1 unmetered call(s) of unknown cost excluded) across 1 run(s)
  usage (account): 5h window 42% used (58% left) · 7d window 17% used (83% left)
  mid  ≥$0.4200 · 4 calls, 1 unmetered (cost unknown) · 6.0k tokens in, 1.2k out
    t1  $0.0000         INTAKE $0.0000
    t2  ≥$0.4200        INTAKE $0.0000 · SCOPE ≥$0.4200

── recent ──
   5m ago  mid  t1: started SCOPE on claude-opus-5-5
   4m ago  mid  t2: finished SCOPE ($0.42)
   4m ago  mid  t2: blocked at SCOPE, needs a human decision (needs an API that does not exist)"""


MID_COMPACT = """\
ATTENTION — 1 item needs you  ·  1 run: 1 running

── needs you ──
  ! mid t2  BLOCKED at SCOPE, needs your decision — needs an API that does not exist
      orchestrator --root /runs/mid --run mid approve --task t2 --by "$USER" --note scope_not_feasible_held
      orchestrator --root /runs/mid --run mid reject --task t2 --by "$USER" --reason '<why>'
      orchestrator --root /runs/mid --run mid abandon --task t2 --reason '<why>'

── running now ──
  mid (fake) — driver: no headless driver (driven task by task from the CLI, or…
    t1  Fake t1  —  SCOPE for 12m · opus-5-5 · stream active

── progress ──
  fake · mid  running · 0/3 done · 1 needs you · last event 4m ago

── cost ──
  spend: ≥$0.4200 (1 unmetered call(s) of unknown cost excluded) across 1 run(s)
  usage (account): 5h 42% used · 7d 17% used
  mid  ≥$0.4200

── recent ──
   5m ago  mid  t1: started SCOPE on claude-opus-5-5
   4m ago  mid  t2: finished SCOPE ($0.42)
   4m ago  mid  t2: blocked at SCOPE, needs a human decision (needs an API that…"""


DONE_WIDE = """\
ALL QUIET — 1 run: 1 done

── running now ──
  nothing running

── progress ──
  fake
    done  done · 1/1 done · last event 2h ago
      t1  done       Fake t1 — PR https://github.com/x/y/pull/1234
          SCOPE done · IMPLEMENT done · TEST done · DELIVER done · REVIEW done

── cost ──
  spend: $3.0800 across 1 run(s)
  usage (account): 5h window 42% used (58% left) · 7d window 17% used (83% left)
  done  $3.0800 · 6 calls · 9.0k tokens in, 1.8k out
    t1  $3.0800         INTAKE $0.0000 · SCOPE $0.3100 · IMPLEMENT $1.9000 · TEST $0.1200 · DELIVER $0.2000
                        REVIEW $0.5500

── recent ──
   2h ago  done  t1: finished REVIEW ($0.55)
   2h ago  done  t1: completed, PR https://github.com/x/y/pull/1234
   2h ago  done  run finished (completed)"""


DONE_COMPACT = """\
ALL QUIET — 1 run: 1 done

── running now ──
  nothing running

── progress ──
  fake · done  done · 1/1 done · last event 2h ago

── cost ──
  spend: $3.0800 across 1 run(s)
  usage (account): 5h 42% used · 7d 17% used
  done  $3.0800

── recent ──
   2h ago  done  t1: finished REVIEW ($0.55)
   2h ago  done  t1: completed, PR https://github.com/x/y/pull/1234
   2h ago  done  run finished (completed)"""


def test_golden_mid_run_wide(tmp_path) -> None:
    _mid_run(tmp_path)
    assert _board(_pinned(tmp_path), WIDE) == MID_WIDE


def test_golden_mid_run_compact(tmp_path) -> None:
    _mid_run(tmp_path)
    assert _board(_pinned(tmp_path), COMPACT) == MID_COMPACT


def test_golden_finished_wide(tmp_path) -> None:
    _finished_run(tmp_path)
    assert _board(_pinned(tmp_path), WIDE) == DONE_WIDE


def test_golden_finished_compact(tmp_path) -> None:
    _finished_run(tmp_path)
    assert _board(_pinned(tmp_path), COMPACT) == DONE_COMPACT


# --- layout rules ---------------------------------------------------------------------


def test_width_threshold_picks_the_layout(tmp_path) -> None:
    _mid_run(tmp_path)
    snap = _pinned(tmp_path)
    # The per-stage strip only appears on the wide board.
    assert "SCOPE running · IMPLEMENT next" not in _board(snap, WIDE_MIN_COLUMNS - 1)
    assert "SCOPE running · IMPLEMENT next" in _board(snap, WIDE_MIN_COLUMNS)
    # No width at all (a caller that cannot tell) gets the wide board.
    assert render_dashboard(snap) == _board(snap, WIDE)


def test_console_width_overrides() -> None:
    assert console_width(80) == 80
    assert console_width(80, wide=True) == WIDE_MIN_COLUMNS
    assert console_width(160, wide=True) == 160  # already wide: keep the real width
    assert console_width(160, compact=True) == WIDE_MIN_COLUMNS - 1
    assert console_width(60, compact=True) == 60


def test_legend_only_when_its_symbol_is_used(tmp_path) -> None:
    _finished_run(tmp_path)
    board = _board(_pinned(tmp_path), WIDE)
    assert " - " not in board and "legend" not in board


def test_stalled_stream_reads_as_stalled_with_its_age(tmp_path) -> None:
    _mid_run(tmp_path)
    snap = _pinned(tmp_path)
    t1 = snap["runs"][0]["tasks"][0]
    t1["activity"] = {"state": "stalled", "seconds_since_event": 960.0, "line": "Bash: x"}
    assert "SCOPE for 12m · opus-5-5 · stream stalled 16m" in _board(snap, WIDE)
    t1["activity"] = {"state": "none", "seconds_since_event": None, "line": None}
    assert "SCOPE for 12m · opus-5-5 · no stream" in _board(snap, WIDE)


def test_task_between_stages_is_not_running_now(tmp_path) -> None:
    """A task whose last stage recorded and whose next has not dispatched is ``running`` to
    the engine, but nothing is executing — it belongs in progress, not in running now."""
    eng = _engine(tmp_path / "r1")
    eng.create_run("r1")
    _intake(eng, "r1", "t1")
    board = _board(dashboard_snapshot(tmp_path, engine_factory=_factory), WIDE)
    running_now = board.split("── running now ──")[1].split("── progress ──")[0]
    assert running_now.strip() == "nothing running"


def test_failed_task_needs_you_with_its_cause_and_a_status_command(tmp_path) -> None:
    rr = tmp_path / "f"
    eng = _engine(rr)
    eng.create_run("f")
    _intake(eng, "f", "t1")
    while (w := eng.next_work("f", "t1")) is not None:
        if w.stage is Stage.IMPLEMENT:
            eng.record("f", make_result(w, status=ResultStatus.FAILURE, error="tests exploded"))
        else:
            eng.record("f", make_result(w))
    snap = dashboard_snapshot(tmp_path, engine_factory=_factory)
    assert eng.store.load_task("f", "t1").state.value == "failed"

    failed = [a for a in snap["attention"] if a["kind"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["stage"] == "implement"
    assert failed[0]["reason"] == "tests exploded"
    assert "failed:t1" in snap["runs"][0]["flags"]

    board = _board(snap, WIDE)
    assert "! f t1  FAILED at IMPLEMENT — tests exploded" in board
    assert f"orchestrator --root {rr} --run f status" in board
    assert "IMPLEMENT failed" in board  # the stage strip


def test_unmetered_stage_cost_is_never_a_confident_figure(tmp_path) -> None:
    rr = tmp_path / "u"
    eng = _engine(rr)
    eng.create_run("u")
    _intake(eng, "u", "t1")
    _ledger(rr, "u", [("t1", "scope", 0.0, False), ("t1", "scope", 0.0, False)])
    board = _board(dashboard_snapshot(tmp_path, engine_factory=_factory), WIDE)
    assert "SCOPE n/a (unmetered)" in board
    # The run and the task are wholly unmetered too: unknown, never a bare $0.
    assert "u  n/a (unmetered) · 2 calls, 2 unmetered (cost unknown)" in board
    assert "t1  n/a (unmetered)" in board
    assert "SCOPE $0.0000" not in board


def test_degraded_rows_name_their_cause_in_progress_and_needs_you(tmp_path) -> None:
    rr = tmp_path / "bad"
    _engine(rr).create_run("bad")
    (rr / "status-bad.json").write_text("{ not json")
    board = _board(dashboard_snapshot(tmp_path, engine_factory=_factory), COMPACT)
    assert "! bad  UNREADABLE status — no status-*.json in the run dir parses" in board
    assert f"look in: {rr}" in board
    assert "fake · bad  <unreadable status>" in board


# --- width reaches the renderer ---------------------------------------------------------


def test_render_watch_rereads_a_callable_width_each_repaint(tmp_path) -> None:
    _mid_run(tmp_path)
    widths = iter([WIDE, COMPACT])
    renders: list[str] = []
    render_watch(
        tmp_path, emit=renders.append, sleeper=lambda _s: None, clear=lambda: None,
        max_iters=2, width=lambda: next(widths), engine_factory=_factory,
    )
    assert "IMPLEMENT next" in renders[0]  # wide: stage strip present
    assert "IMPLEMENT next" not in renders[1]  # compact after the "resize"


def test_cli_dashboard_width_flags(tmp_path, monkeypatch, capsys) -> None:
    import os
    import shutil

    from orchestrator.cli import main

    _engine(tmp_path / "r1").create_run("r1")
    seen: list[int | None] = []
    real = dashboard.render_dashboard

    def spy(snap, *, width=None):
        seen.append(width)
        return real(snap, width=width)

    monkeypatch.setattr(dashboard, "render_dashboard", spy)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **k: os.terminal_size((80, 24)))
    base = ["--root", str(tmp_path), "--project", "tests.fakeproject", "dashboard"]
    assert main(base) == 0
    assert main([*base, "--wide"]) == 0
    assert main([*base, "--compact"]) == 0
    assert seen == [80, WIDE_MIN_COLUMNS, 80]
    capsys.readouterr()
