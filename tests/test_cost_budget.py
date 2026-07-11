"""Cost control (#34): per-run budgets, budget-aware lane routing, a-priori estimates.

Cost was RECORDED but controlled nothing. These cover the control half: a soft budget
warning (once), a hard PAUSE that leaves in-flight record() intact and unpauses cleanly,
unmetered rows never counting, deterministic cost-aware lane routing (bands + pin honored
+ evented + default-off), and the a-priori spec estimate math + strict gate.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.cost_policy import (
    DEFAULT_COST_ROUTER,
    estimate_to_usd,
)
from orchestrator.engine import Engine
from orchestrator.render import render_cost_summary
from orchestrator.schemas.enums import (
    LANE_DETERMINISTIC_STAGES,
    LANE_STAGES,
    ExecutionLane,
    Stage,
)
from orchestrator.schemas.work import TokenUsage
from orchestrator.spec_intake import estimate_budget, render_estimate
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


class NotifyProject(FakeProject):
    """FakeProject + a recording notify() hook (the alerting seam the engine calls)."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.notifications.append((kind, payload))


def _engine(tmp_path, project=None, **kw) -> Engine:
    project = project if project is not None else FakeProject()
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _advance(eng, *, tokens: TokenUsage | None = None, run="r1", task="t1"):
    """One next_work -> record step; returns (work, record_result) or (None, None)."""
    work = eng.next_work(run, task)
    if work is None:
        return None, None
    rec = eng.record(run, make_result(work, tokens=tokens))
    return work, rec


# scope runs on opus (deep_reason) at $5/Mtok input, so 200k input tokens == exactly $1.00.
_SCOPE_1USD = TokenUsage(input=200_000, output=0)


# --- estimate table + router (pure) ---------------------------------------------

def test_estimate_to_usd_forms() -> None:
    assert estimate_to_usd("small") == 1.5
    assert estimate_to_usd("LARGE") == 10.0
    assert estimate_to_usd("m") == 4.0  # alias
    assert estimate_to_usd("2.5") == 2.5  # bare numeric string -> USD
    assert estimate_to_usd(3) == 3.0
    assert estimate_to_usd(None) is None
    assert estimate_to_usd("huge") is None  # unknown word
    assert estimate_to_usd(True) is None  # bool is not a number here
    assert estimate_to_usd(-1) is None  # negative rejected


def test_cost_router_bands_and_nudge() -> None:
    r = DEFAULT_COST_ROUTER
    assert r.route(1.0).lane is ExecutionLane.FULL
    assert r.route(0.25).lane is ExecutionLane.FULL
    assert r.route(0.19).lane is ExecutionLane.LITE
    assert r.route(0.05).lane is ExecutionLane.LITE
    assert r.route(0.04).lane is ExecutionLane.MICRO
    # below FULL, prefer $0 deterministic TEST/DELIVER for whatever the preset runs
    assert set(r.route(0.10).deterministic_stages) == {Stage.TEST, Stage.DELIVER}
    assert set(r.route(0.04).deterministic_stages) == {Stage.DELIVER}  # micro has no TEST
    assert r.route(1.0).deterministic_stages == ()  # FULL: none forced
    # a LARGE estimate nudges one band cheaper — but never past the top band
    assert r.route(0.10, estimate="large").lane is ExecutionLane.MICRO
    assert r.route(1.0, estimate="large").lane is ExecutionLane.FULL


# --- per-run budget: soft warning ------------------------------------------------

def test_soft_warning_fires_once_at_threshold(tmp_path) -> None:
    proj = NotifyProject()
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.2)  # soft threshold = $0.96
    eng.add_task("r1", "t1")
    _advance(eng)  # intake — $0 engine stage
    _advance(eng, tokens=_SCOPE_1USD)  # scope — $1.00 metered (>= soft, < hard)

    # next dispatch: spend $1.00 >= $0.96 soft -> warn, but < $1.20 -> NOT paused
    work = eng.next_work("r1", "t1")
    assert work is not None and work.stage is Stage.IMPLEMENT
    assert eng.store.load_run("r1").state.value == "running"
    assert [k for k, _ in proj.notifications if k == "budget_warning"]

    # dedupe: a further cheap ($0) dispatch must NOT re-warn
    eng.record("r1", make_result(work, tokens=TokenUsage(input=0, output=0)))
    eng.next_work("r1", "t1")  # test stage
    warnings = [k for k, _ in proj.notifications if k == "budget_warning"]
    assert len(warnings) == 1


# --- per-run budget: hard stop + in-flight record + unpause ----------------------

def _drive_to_pause(tmp_path, proj, budget=0.5):
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=budget)
    eng.add_task("r1", "t1")
    _advance(eng)  # intake $0
    # scope: $1.00 metered — the record LANDS even though it blows the budget
    _, rec = _advance(eng, tokens=_SCOPE_1USD)
    assert rec["recorded"] is True and rec["outcome"] == "stage_completed"
    return eng


def test_hard_stop_pauses_and_inflight_record_lands(tmp_path) -> None:
    proj = NotifyProject()
    eng = _drive_to_pause(tmp_path, proj)

    # spend $1.00 >= budget $0.50 -> next dispatch pauses the run and yields no work
    assert eng.next_work("r1", "t1") is None
    run = eng.store.load_run("r1")
    assert run.state.value == "paused"

    events = eng.store.read_events("r1")
    paused = [e for e in events if e["type"] == "run_paused"]
    assert paused and "budget_exhausted" in paused[-1]["reason"]
    kinds = [k for k, _ in proj.notifications]
    assert "run_paused" in kinds and "budget_warning" in kinds


def test_unpause_without_raise_drops_cap_and_resumes(tmp_path) -> None:
    eng = _drive_to_pause(tmp_path, NotifyProject())
    eng.next_work("r1", "t1")  # trip the pause

    resumed = eng.unpause_run("r1")  # explicit human override, no new ceiling
    assert resumed.state.value == "running"
    assert resumed.budget_usd is None  # cap dropped so it can't instantly re-pause
    work = eng.next_work("r1", "t1")
    assert work is not None and work.stage is Stage.IMPLEMENT


def test_unpause_with_raise_budget_sets_new_ceiling(tmp_path) -> None:
    eng = _drive_to_pause(tmp_path, NotifyProject())
    eng.next_work("r1", "t1")  # trip the pause

    resumed = eng.unpause_run("r1", raise_budget_to=5.0)
    assert resumed.state.value == "running"
    assert resumed.budget_usd == 5.0
    assert resumed.budget_warning_sent is False  # re-armed against the new cap
    work = eng.next_work("r1", "t1")  # $1.00 spent < $5.00 -> dispatches
    assert work is not None and work.stage is Stage.IMPLEMENT


def test_unmetered_interactive_rows_never_count(tmp_path) -> None:
    # A whole run on zero-token interactive rows (unmetered => $0) must never trip a
    # budget, even a tiny one — the honest budget counts metered USD only.
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=0.01)
    eng.add_task("r1", "t1")
    for _ in range(8):  # safety bound
        work = eng.next_work("r1", "t1")
        if work is None:
            break
        eng.record("r1", make_result(work, tokens=TokenUsage(input=0, output=0)))
    assert eng.ledger.metered_spend() == 0.0
    assert eng.store.load_run("r1").state.value == "completed"


# --- cost-aware lane routing -----------------------------------------------------

def test_routing_default_off(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL)  # route_by_cost defaults off
    task = eng.add_task("r1", "t1")
    assert task.pipeline == LANE_STAGES[ExecutionLane.FULL]
    assert not [e for e in eng.store.read_events("r1") if e["type"] == "lane_routed"]


def test_routing_full_budget_picks_full_and_emits_event(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=10.0, route_by_cost=True)
    task = eng.add_task("r1", "t1")  # no spend yet -> remaining 1.0 -> FULL
    assert task.pipeline == LANE_STAGES[ExecutionLane.FULL]
    ev = [e for e in eng.store.read_events("r1") if e["type"] == "lane_routed"]
    assert len(ev) == 1 and ev[0]["preset"] == "full"
    assert ev[0]["remaining_fraction"] == 1.0


def test_routing_thin_budget_downgrades_next_task(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.0, route_by_cost=True)
    eng.add_task("r1", "t1")  # routed FULL at add (remaining 1.0)
    _advance(eng, task="t1")  # intake $0
    # spend $0.95 -> remaining fraction 0.05 -> LITE band for the next task
    _advance(eng, tokens=TokenUsage(input=190_000, output=0), task="t1")

    t2 = eng.add_task("r1", "t2")
    assert t2.pipeline == LANE_STAGES[ExecutionLane.LITE]
    assert set(t2.deterministic_stages) == {Stage.TEST, Stage.DELIVER}
    ev = [e for e in eng.store.read_events("r1") if e["type"] == "lane_routed"
          and e["task_id"] == "t2"]
    assert ev and ev[0]["preset"] == "lite"


def test_routing_decision_overrides_lane_preset_deterministic_default(tmp_path) -> None:
    # Three-way precedence, rung 2 vs 3 (#119): when route_by_cost is on, the deterministic
    # stages come from the ROUTING DECISION, and that wins over the lane preset default.
    # These two only diverge when a lane is pinned explicitly (otherwise effective_lane
    # collapses to the routed lane and both sources compute the same value), so pin FULL —
    # whose preset default is NO deterministic stages — while a thin budget routes the
    # pipeline to LITE. The task ends up with $0 TEST/DELIVER from the decision, proving the
    # decision beat FULL's () preset default.
    assert LANE_DETERMINISTIC_STAGES[ExecutionLane.FULL] == ()  # FULL preset default: none forced
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.0, route_by_cost=True)
    eng.add_task("r1", "t1")
    _advance(eng, task="t1")  # intake $0
    _advance(eng, tokens=TokenUsage(input=190_000, output=0), task="t1")  # spend $0.95 -> LITE band

    t2 = eng.add_task("r1", "t2", ExecutionLane.FULL)  # explicit FULL lane, but budget is thin
    assert t2.pipeline == LANE_STAGES[ExecutionLane.LITE]  # routing still downgrades the pipeline
    # the decision's {TEST, DELIVER} won over FULL's () preset default (rung 2 > rung 3)
    assert set(t2.deterministic_stages) == {Stage.TEST, Stage.DELIVER}
    ev = [e for e in eng.store.read_events("r1") if e["type"] == "lane_routed"
          and e["task_id"] == "t2"]
    assert ev and ev[0]["preset"] == "lite"


def test_explicit_deterministic_stages_override_the_routing_decision(tmp_path) -> None:
    # Three-way precedence, rung 1 vs 2 (#119): an explicit deterministic_stages arg
    # (--deterministic-stages) wins over the cost-routing decision. A thin budget routes to
    # LITE (whose decision forces {TEST, DELIVER}), but the caller pins DELIVER only, and that
    # pin survives untouched — routing still governs the LANE/pipeline, not the pin.
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.0, route_by_cost=True)
    eng.add_task("r1", "t1")
    _advance(eng, task="t1")  # intake $0
    _advance(eng, tokens=TokenUsage(input=190_000, output=0), task="t1")  # spend $0.95 -> LITE band

    t2 = eng.add_task("r1", "t2", deterministic_stages=[Stage.DELIVER])
    assert t2.deterministic_stages == (Stage.DELIVER,)  # caller pin wins over routing's {TEST, DELIVER}
    assert t2.pipeline == LANE_STAGES[ExecutionLane.LITE]  # routing still picks the pipeline


def test_explicit_pipeline_pin_always_honored(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.0, route_by_cost=True)
    pinned = (Stage.INTAKE, Stage.IMPLEMENT, Stage.REVIEW)
    task = eng.add_task("r1", "t1", pipeline=list(pinned))
    assert task.pipeline == pinned
    # a pinned task is never routed -> no lane_routed event
    assert not [e for e in eng.store.read_events("r1") if e["type"] == "lane_routed"]


def test_estimate_from_labels(tmp_path) -> None:
    eng = _engine(tmp_path)
    assert eng._estimate_from_labels(["size:large"]) == "large"
    assert eng._estimate_from_labels(["estimate:medium"]) == "medium"
    assert eng._estimate_from_labels(["small"]) == "small"
    assert eng._estimate_from_labels(["bug", "urgent"]) is None
    assert eng._estimate_from_labels(None) is None


# --- a-priori spec estimate ------------------------------------------------------

def _spec(*estimates):
    return {
        "title": "x", "summary": "y",
        "tasks": [
            {"id": f"t{i}", "title": f"T{i}", "body": "b", **({"estimate": e} if e else {})}
            for i, e in enumerate(estimates, 1)
        ],
    }


def test_estimate_budget_math() -> None:
    est = estimate_budget(_spec("small", "large", None), budget_usd=10.0)
    assert est["total_estimate_usd"] == 11.5  # 1.5 + 10.0 (+ one unestimated)
    assert est["unestimated"] == 1
    assert est["overrun"] is True
    within = estimate_budget(_spec("small", "large", None), budget_usd=20.0)
    assert within["overrun"] is False
    assert "OVER budget" in render_estimate(est)


def test_cli_spec_plan_budget_strict(tmp_path, capsys) -> None:
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(_spec("small", "large")))  # $11.50 total
    rc = main(["spec", "plan", str(f), "--budget-usd", "5", "--strict"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "A-priori cost estimate" in out and "OVER budget" in out
    # within budget -> success
    assert main(["spec", "plan", str(f), "--budget-usd", "50", "--strict"]) == 0


def test_cli_spec_plan_budget_no_strict_is_advisory(tmp_path, capsys) -> None:
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(_spec("large", "large")))  # $20 total, over $5
    # without --strict an overrun only prints; exit stays 0 (advisory)
    assert main(["spec", "plan", str(f), "--budget-usd", "5"]) == 0
    assert "OVER budget" in capsys.readouterr().out


# --- observability ---------------------------------------------------------------

def test_status_surfaces_budget(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=2.0, route_by_cost=True)
    eng.add_task("r1", "t1")
    _advance(eng)  # intake $0
    _advance(eng, tokens=_SCOPE_1USD)  # scope $1.00

    budget = eng.status("r1")["budget"]
    assert budget["budget_usd"] == 2.0
    assert budget["spent_usd"] == 1.0
    assert budget["fraction"] == 0.5
    assert budget["exhausted"] is False
    assert budget["route_by_cost"] is True
    # status() writes cost-summary.md with the budget line
    assert "Budget (metered)" in (tmp_path / "cost-summary.md").read_text()


def test_scheduler_run_pauses_when_budget_exhausted(tmp_path) -> None:
    # The batch loop must stop dispatching and PAUSE once metered spend crosses the
    # budget — the engine gate at next_work catches the scheduler path too.
    from orchestrator.scheduler import Scheduler

    proj = NotifyProject()
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=0.025)  # a few default stages' worth
    eng.add_task("r1", "t1")

    def runner(work):
        return [make_result(w) for w in work]

    status = Scheduler(eng).run("r1", runner)
    assert status["run_state"] == "paused"
    assert status["budget"]["exhausted"] is True
    assert status["tasks"]["t1"]["state"] != "completed"  # stopped mid-pipeline
    assert "run_paused" in [k for k, _ in proj.notifications]


def test_status_budget_none_without_budget(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert eng.status("r1")["budget"] is None


def test_render_cost_summary_budget_line() -> None:
    summary = {"total_cost_usd": 1.0, "total_invocations": 2, "unmetered_calls": 0,
               "by_model": {}}
    budget = {"budget_usd": 2.0, "spent_usd": 1.0, "fraction": 0.5, "exhausted": False,
              "remaining_usd": 1.0}
    md = render_cost_summary("r1", summary, budget=budget)
    assert "Budget (metered)" in md and "$1.0000 / $2.0000" in md
    exhausted = render_cost_summary("r1", summary, budget={**budget, "exhausted": True})
    assert "EXHAUSTED" in exhausted


def test_cli_init_run_accepts_budget_flags(tmp_path, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "full", "--budget-usd", "3.5", "--route-by-cost"])
    out = json.loads(capsys.readouterr().out)
    assert out["budget_usd"] == 3.5 and out["route_by_cost"] is True


def test_cli_init_run_accepts_route_by_capacity(tmp_path, capsys) -> None:
    # #12: capacity routing is a DISTINCT opt-in flag from cost routing (both default off).
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "full", "--route-by-capacity"])
    out = json.loads(capsys.readouterr().out)
    assert out["route_by_capacity"] is True and out["route_by_cost"] is False


@pytest.mark.parametrize("raise_flag,expected", [(["--raise-budget", "9"], 9.0), ([], None)])
def test_cli_unpause_raise_budget(tmp_path, capsys, raise_flag, expected) -> None:
    proj = NotifyProject()
    eng = _drive_to_pause(tmp_path, proj)
    eng.next_work("r1", "t1")  # trip pause
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    main([*base, "unpause", *raise_flag])
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "running" and out["budget_usd"] == expected
