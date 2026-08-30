"""Observability honesty (audit gaps 5/9): unmetered interactive calls stop rendering
as a confident $0.0000, reports carry wall time again, and status() flags stale tasks
so a dead run announces itself instead of waiting to be discovered."""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import render_cost_summary
from orchestrator.schemas.enums import ExecutionMode, Provider
from orchestrator.schemas.work import TokenUsage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _drive_one_stage(eng, *, tokens=None, mode=None, provider=None):
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake (engine lane)
    w = eng.next_work("r1", "t1")  # scope — the first model stage
    eng.record("r1", make_result(w, tokens=tokens, mode=mode, provider=provider))


def test_interactive_zero_token_call_is_unmetered(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _drive_one_stage(
        eng, tokens=TokenUsage(input=0, output=0),
        mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE,
    )
    rows = eng.ledger.rows()
    interactive = [r for r in rows if r["lane"] == "interactive"]
    assert interactive and interactive[0]["metered"] is False
    # the deterministic engine-lane intake is genuinely $0, NOT unmetered
    engine_rows = [r for r in rows if r["lane"] == "engine"]
    assert engine_rows and engine_rows[0]["metered"] is True

    summary = eng.ledger.summary()
    assert summary["unmetered_calls"] == 1
    md = render_cost_summary("r1", summary)
    total_line = next(ln for ln in md.splitlines() if ln.startswith("- Total cost"))
    # the total line no longer claims "free" without qualification
    assert "unmetered" in total_line or "cannot meter" in total_line


def test_metered_run_renders_plain_total(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _drive_one_stage(eng, tokens=TokenUsage(input=1000, output=200),
                     mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    summary = eng.ledger.summary()
    assert summary["unmetered_calls"] == 0
    md = render_cost_summary("r1", summary)
    assert "Total cost: **$" in md and "unmetered" not in md


def test_mixed_run_states_the_unmetered_exclusion() -> None:
    md = render_cost_summary("r1", {
        "total_invocations": 6, "total_cost_usd": 1.23, "unmetered_calls": 2,
        "total_wall_s": 0, "by_model": {},
    })
    assert "metered calls only" in md and "2 unmetered call(s)" in md
    # #319 generalized the wording: an unmetered call is no longer necessarily an
    # interactive-lane one — a metered lane loses a call's usage report whenever an attempt
    # dies before printing it — so the line must not attribute the exclusion to one lane.
    assert "interactive call(s)" not in md


def test_duration_recorded_and_wall_time_rendered(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _drive_one_stage(eng, tokens=TokenUsage(input=100, output=10),
                     mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    rows = eng.ledger.rows()
    assert all(r["duration_s"] is not None and r["duration_s"] >= 0 for r in rows)
    md = render_cost_summary("r1", {
        "total_invocations": 3, "total_cost_usd": 2.0, "unmetered_calls": 0,
        "total_wall_s": 754.2, "by_model": {},
    })
    assert "Wall time (in model calls): **12.6 min**" in md


def test_status_flags_stale_nonterminal_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    st = eng.status("r1")
    assert st["tasks"]["t1"]["stale"] is False  # freshly added
    assert st["tasks"]["t1"]["seconds_since_update"] is not None

    # Age the task doc by hand, as a crashed/hung supervisor would leave it
    # (save_task, not update_task — the latter deliberately re-stamps updated_at).
    task = eng.store.load_task("r1", "t1")
    task.updated_at = "2020-01-01T00:00:00+00:00"
    eng.store.save_task(task)
    st = eng.status("r1")
    assert st["tasks"]["t1"]["stale"] is True


def test_status_never_flags_terminal_or_held_tasks(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(7):  # run the whole pipeline green
        w = eng.next_work("r1", "t1")
        if w is None:
            break
        eng.record("r1", make_result(w))
    task = eng.store.load_task("r1", "t1")
    task.updated_at = "2020-01-01T00:00:00+00:00"
    eng.store.save_task(task)
    st = eng.status("r1")
    assert st["tasks"]["t1"]["state"] == "completed"
    assert st["tasks"]["t1"]["stale"] is False  # a finished task can't be stale
