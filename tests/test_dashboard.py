"""Cross-session dashboard (#6): the attention-first board over ALL runs.

The old bash monitor's core value was "what needs me?" across every concurrent + historical
orchestrator. These tests pin the rebuild's data assembly + rendering: run discovery ordering,
the per-run row for each interesting state (running w/ in-flight, paused w/ reason,
blocked-on-human surfaced, budget fraction/exhausted, completed-with-rejections), the attention
band ordering + all-quiet header, corrupt/partial status → an unreadable row (never a crash),
the --all/--limit filtering, the sleep-free --watch loop, and probe-failure resilience.

Runs are built the real way — Engine + FakeProject, each in its own runs/<id>/ store root — so
the assembly runs against genuine status/events/ledger files, not synthetic dicts.
"""

from __future__ import annotations

import json
import os

from orchestrator.cost_ledger import CostLedger
from orchestrator.dashboard import (
    dashboard_snapshot,
    discover_runs,
    render_dashboard,
    render_watch,
)
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from orchestrator.stream_probe import stages_dir
from tests.conftest import FakeProject, make_result

# --- builders -------------------------------------------------------------------------


def _engine(run_root, **kw) -> Engine:
    run_root.mkdir(parents=True, exist_ok=True)
    return Engine(
        StatusStore(run_root), CostLedger(run_root / "stage-costs.jsonl"), FakeProject(), **kw
    )


def _factory(**kw):
    return lambda run_root: _engine(run_root, **kw)


def _drive_intake(eng, run_id, task_id="t1"):
    eng.create_run(run_id)
    eng.add_task(run_id, task_id)
    eng.record(run_id, make_result(eng.next_work(run_id, task_id)))  # deterministic intake


def _touch(run_root, run_id, mtime: float) -> None:
    """Pin a run doc's mtime so recency ordering is deterministic."""
    os.utime(run_root / f"status-{run_id}.json", (mtime, mtime))


def _snapshot(root, **kw):
    kw.setdefault("engine_factory", _factory())
    kw.setdefault("clock", lambda: 1_000_000.0)
    return dashboard_snapshot(root, **kw)


# --- discovery ------------------------------------------------------------------------


def test_discover_orders_by_recency(tmp_path) -> None:
    for name, mt in [("r-old", 100.0), ("r-mid", 200.0), ("r-new", 300.0)]:
        rr = tmp_path / name
        eng = _engine(rr)
        eng.create_run(name)
        _touch(rr, name, mt)
    assert discover_runs(tmp_path) == ["r-new", "r-mid", "r-old"]


def test_discover_skips_non_run_dirs(tmp_path) -> None:
    (tmp_path / "not-a-run").mkdir()  # empty dir, no status files
    rr = tmp_path / "r1"
    _engine(rr).create_run("r1")
    assert discover_runs(tmp_path) == ["r1"]


# --- per-run rows ---------------------------------------------------------------------


def test_running_run_surfaces_inflight_activity(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    _drive_intake(eng, "r1")
    w = eng.next_work("r1", "t1")  # dispatch scope; leaves the task RUNNING
    assert w.stage is Stage.SCOPE
    # Simulate a live provider stream so include_activity lifts a real activity line.
    d = stages_dir(rr, "t1")
    d.mkdir(parents=True, exist_ok=True)
    (d / "scope-attempt0.stream.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}) + "\n"
    )

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["run_id"] == "r1"
    assert row["state"] == "running"
    inflight = row["inflight"]
    assert len(inflight) == 1
    assert inflight[0]["task_id"] == "t1"
    assert inflight[0]["stage"] == "scope"
    assert "Bash" in inflight[0]["line"] and "pytest -q" in inflight[0]["line"]
    # A healthy running run is not attention.
    assert row["attention"] is False


def test_paused_run_surfaces_reason(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    _drive_intake(eng, "r1")
    eng.pause_run("r1", "batch circuit breaker: 3 consecutive failures")

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["state"] == "paused"
    assert "paused" in row["flags"]
    assert row["attention"] is True
    paused = [a for a in snap["attention"] if a["kind"] == "paused"]
    assert len(paused) == 1
    assert "circuit breaker" in paused[0]["reason"]


def test_blocked_on_human_surfaced_in_attention(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    _drive_intake(eng, "r1")
    w = eng.next_work("r1", "t1")  # scope
    eng.record("r1", make_result(w, structured_output={
        "feasible": False, "blocked_reason": "needs an API that does not exist", "plan": []}))

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["state"] == "running"  # blocked_on_human keeps the run open (non-terminal)
    blocked = [a for a in snap["attention"] if a["kind"] == "blocked_on_human"]
    assert len(blocked) == 1
    assert blocked[0]["task_id"] == "t1"
    assert blocked[0]["reason"] == "needs an API that does not exist"


def test_stale_task_flagged(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    _drive_intake(eng, "r1")
    eng.next_work("r1", "t1")  # RUNNING, outstanding lease
    # stale_after_s=-1 → any age counts as stale for a non-terminal task.
    snap = _snapshot(tmp_path, stale_after_s=-1)
    stale = [a for a in snap["attention"] if a["kind"] == "stale"]
    assert [s["task_id"] for s in stale] == ["t1"]
    assert any(f.startswith("stale:") for f in snap["runs"][0]["flags"])


def test_budget_fraction_reported(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    eng.create_run("r1", budget_usd=10.0)
    eng.add_task("r1", "t1")
    # Append a metered spend row directly (interactive rows are $0/unmetered).
    (rr / "stage-costs.jsonl").write_text(
        json.dumps({"metered": True, "cost_usd": 4.0, "model": "x", "run_id": "r1"}) + "\n"
    )
    snap = _snapshot(tmp_path)
    budget = snap["runs"][0]["budget"]
    assert budget["fraction"] == 0.4
    assert budget["exhausted"] is False
    # Not exhausted → not an attention item.
    assert not [a for a in snap["attention"] if a["kind"] == "budget_exhausted"]


def test_budget_exhausted_is_attention(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    eng.create_run("r1", budget_usd=2.0)
    eng.add_task("r1", "t1")
    (rr / "stage-costs.jsonl").write_text(
        json.dumps({"metered": True, "cost_usd": 5.0, "model": "x", "run_id": "r1"}) + "\n"
    )
    snap = _snapshot(tmp_path)
    assert "budget-exhausted" in snap["runs"][0]["flags"]
    exhausted = [a for a in snap["attention"] if a["kind"] == "budget_exhausted"]
    assert len(exhausted) == 1


def test_completed_with_rejections_state(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.hold_for_approval("r1", "t1", what="needs a human call")
    eng.reject("r1", "t1", rejected_by="craig", reason="not worth doing")

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["state"] == "completed_with_rejections"
    assert row["terminal"] is True
    out = render_dashboard(snap)
    assert "completed_with_rejections" in out


# --- attention band + header ----------------------------------------------------------


def test_all_quiet_header_when_nothing_needs_attention(tmp_path) -> None:
    # One completed run + one healthy running run → no attention.
    ok = tmp_path / "done"
    eng = _engine(ok)
    eng.create_run("done")
    eng.add_task("done", "t1")
    while (w := eng.next_work("done", "t1")) is not None:
        eng.record("done", make_result(w))

    run = tmp_path / "live"
    eng2 = _engine(run)
    _drive_intake(eng2, "live")  # intaken, no stage in flight → healthy, non-attention

    snap = _snapshot(tmp_path)
    assert snap["header"]["all_quiet"] is True
    out = render_dashboard(snap)
    assert out.splitlines()[0].startswith("ALL QUIET")


def test_attention_band_orders_blocked_and_paused_above_stale(tmp_path) -> None:
    # A stale run (recent), a paused run, and a blocked-on-human run.
    s = tmp_path / "stale"
    es = _engine(s)
    _drive_intake(es, "stale")
    es.next_work("stale", "t1")
    _touch(s, "stale", 900.0)  # most recent → would sort first WITHOUT the severity rank

    p = tmp_path / "paused"
    ep = _engine(p)
    _drive_intake(ep, "paused")
    ep.pause_run("paused", "human paused")
    _touch(p, "paused", 500.0)

    b = tmp_path / "blocked"
    eb = _engine(b)
    _drive_intake(eb, "blocked")
    eb.record("blocked", make_result(eb.next_work("blocked", "t1"), structured_output={
        "feasible": False, "blocked_reason": "cannot", "plan": []}))
    _touch(b, "blocked", 100.0)

    snap = _snapshot(tmp_path, stale_after_s=-1)
    kinds = [a["kind"] for a in snap["attention"]]
    assert kinds.index("blocked_on_human") < kinds.index("stale")
    assert kinds.index("paused") < kinds.index("stale")
    out = render_dashboard(snap)
    assert "── needs you ──" in out
    assert out.splitlines()[0].startswith("ATTENTION")


# --- corrupt / partial status ---------------------------------------------------------


def test_corrupt_run_doc_is_unreadable_row_not_crash(tmp_path) -> None:
    rr = tmp_path / "r1"
    eng = _engine(rr)
    eng.create_run("r1")
    (rr / "status-r1.json").write_text("{ this is not json")  # clobber the run doc

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["unreadable"] is True
    assert row["state"] == "<unreadable>"
    assert row["attention"] is True
    out = render_dashboard(snap)  # must not raise
    assert "<unreadable status>" in out


def test_partial_task_doc_becomes_unreadable_row(tmp_path) -> None:
    # Run doc is fine, but a task doc is corrupt → engine.status() raises → unreadable row.
    rr = tmp_path / "r1"
    eng = _engine(rr)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    (rr / "status-r1-t1.json").write_text("{ half-written")

    snap = _snapshot(tmp_path)
    row = snap["runs"][0]
    assert row["unreadable"] is True
    assert "<unreadable status>" in render_dashboard(snap)


# --- filtering ------------------------------------------------------------------------


def _make_completed(tmp_path, run_id, mtime):
    rr = tmp_path / run_id
    eng = _engine(rr)
    eng.create_run(run_id)
    eng.add_task(run_id, "t1")
    while (w := eng.next_work(run_id, "t1")) is not None:
        eng.record(run_id, make_result(w))
    _touch(rr, run_id, mtime)


def _make_running(tmp_path, run_id, mtime):
    rr = tmp_path / run_id
    eng = _engine(rr)
    _drive_intake(eng, run_id)
    _touch(rr, run_id, mtime)


def test_limit_and_all_filtering(tmp_path) -> None:
    _make_running(tmp_path, "live-a", 1000.0)
    _make_running(tmp_path, "live-b", 1001.0)
    for i in range(7):
        _make_completed(tmp_path, f"done-{i}", 100.0 + i)

    # Default: all 2 non-terminal + the 5 most-recent terminal = 7.
    default = _snapshot(tmp_path)
    assert default["header"]["shown"] == 7
    assert default["header"]["total_discovered"] == 9

    # --all: everything.
    everything = _snapshot(tmp_path, show_all=True)
    assert everything["header"]["shown"] == 9

    # --limit truncates the (attention-first, recency) list.
    limited = _snapshot(tmp_path, show_all=True, limit=4)
    assert limited["header"]["shown"] == 4


# --- watch loop -----------------------------------------------------------------------


def test_watch_renders_twice_then_stops(tmp_path) -> None:
    _make_running(tmp_path, "r1", 100.0)
    renders: list[str] = []
    calls = {"n": 0}

    def sleeper(_secs):
        calls["n"] += 1
        if calls["n"] >= 2:  # allow two renders, then interrupt
            raise KeyboardInterrupt

    # clear is a no-op so `renders` holds exactly the board prints.
    render_watch(
        tmp_path, emit=renders.append, sleeper=sleeper, clear=lambda: None,
        engine_factory=_factory(), clock=lambda: 1_000_000.0,
    )
    assert len(renders) == 2
    assert all(r.startswith(("ALL QUIET", "ATTENTION")) for r in renders)


def test_watch_bounded_by_max_iters(tmp_path) -> None:
    _make_running(tmp_path, "r1", 100.0)
    renders: list[str] = []
    slept: list[float] = []
    render_watch(
        tmp_path, emit=renders.append, sleeper=slept.append, clear=lambda: None,
        interval=30, max_iters=3, engine_factory=_factory(), clock=lambda: 1_000_000.0,
    )
    assert len(renders) == 3
    assert slept == [30, 30]  # slept between renders, not after the last


# --- resilience -----------------------------------------------------------------------


def test_usage_probe_failure_still_renders(tmp_path) -> None:
    _make_running(tmp_path, "r1", 100.0)

    def boom():
        raise RuntimeError("probe exploded")

    snap = _snapshot(tmp_path, usage_reader=boom)
    assert snap["header"]["usage"] is None
    out = render_dashboard(snap)
    assert "usage: unavailable" in out
