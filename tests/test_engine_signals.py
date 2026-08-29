"""Engine-AUTHORED input to the meta-authoring seam (#400).

The seam could only ever report what a stage model volunteered in a retrospective, so a
driver- or scheduler-level defect — which has no model author — reached the tracker only
if a human read `events.jsonl` by hand. These cover the second input: which log records
qualify (and, just as load-bearing, which do NOT), that the recurrence threshold belongs
to the signal rather than to a module constant, that re-scanning is idempotent, and that
the filing still lands on the ENGINE's tracker rather than a product's.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import engine as engine_module
from orchestrator import engine_signals as sig
from orchestrator import meta_authoring as meta
from orchestrator import scheduler as sched_mod
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.driver_log import REC_EXIT
from orchestrator.engine import Engine
from orchestrator.schemas.enums import TERMINAL_TASK_STATES, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeTaskSource, make_result


def _engine(tmp_path, project, *, meta_task_source=None) -> Engine:
    return Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "cost.jsonl"),
        project,
        meta_task_source=meta_task_source or project.task_source,
    )


def _facts(**kw) -> sig.RunFacts:
    base = {"run_id": "r1", "run_state": "running", "run_terminal": False}
    return sig.RunFacts(**{**base, **kw})


def _detect(events=(), driver=(), facts=None):
    observations, notices = sig.detect_observations(
        list(events), list(driver), facts or _facts()
    )
    return observations, notices


def _exit(reason: str) -> dict:
    return {"type": REC_EXIT, "ts": "2026-08-28T01:00:00+00:00", "run_id": "r1",
            "reason": reason}


# --- the exit-reason constants this module reads out of driver.jsonl -------------------


def test_exit_reason_constants_match_the_scheduler() -> None:
    """`engine_signals` cannot import `scheduler` (it would close a cycle through Engine),
    so it redeclares the two exit reasons it matches on. This is the guard that keeps that
    duplication from drifting into a detector that silently never fires."""
    assert sig.EXIT_NOTHING_DISPATCHABLE == sched_mod.EXIT_DONE
    assert sig.EXIT_BLOCKED_ORPHANED == sched_mod.EXIT_BLOCKED_ORPHANED


# --- #399: driver exit on unfinished work ---------------------------------------------


def test_driver_exit_on_unfinished_work_fires() -> None:
    obs, _ = _detect(
        driver=[_exit(sig.EXIT_NOTHING_DISPATCHABLE)],
        facts=_facts(unfinished_tasks=("t1", "t2")),
    )
    assert [o.signal for o in obs] == ["driver_exit_on_unfinished_work"]
    assert "t1, t2" in obs[0].text


@pytest.mark.parametrize(
    "facts",
    [
        # The run genuinely finished — the ordinary, overwhelmingly common exit.
        _facts(run_terminal=True, run_state="completed", unfinished_tasks=("t1",)),
        # A human paused/parked the batch: the driver is obeying, not failing.
        _facts(run_state="paused", unfinished_tasks=("t1",)),
        _facts(run_state="parked", unfinished_tasks=("t1",)),
        # Nothing was dispatchable because nothing was left to dispatch.
        _facts(unfinished_tasks=()),
    ],
    ids=["run-finished", "paused", "parked", "no-unfinished-work"],
)
def test_driver_exit_on_unfinished_work_declines_normal_operation(facts) -> None:
    obs, _ = _detect(driver=[_exit(sig.EXIT_NOTHING_DISPATCHABLE)], facts=facts)
    assert obs == []


def test_rate_limit_cooldown_is_not_a_signal() -> None:
    """The allowlist's whole point: a cooldown wait is normal operation. A driver that is
    SLEEPING through one has not exited, and its heartbeats must not read as a defect."""
    heartbeats = [
        {"type": "driver_heartbeat", "ts": "2026-08-28T01:00:00+00:00", "run_id": "r1",
         "state": "waiting_on_cooldown", "reason": "rate-limit cooldown"},
    ]
    events = [{"type": "rate_limited", "ts": "2026-08-28T00:30:00+00:00", "run_id": "r1"}]
    obs, _ = _detect(events=events, driver=heartbeats,
                     facts=_facts(unfinished_tasks=("t1",)))
    assert obs == []


# --- the remaining allowlist ----------------------------------------------------------


def test_blocked_on_orphaned_dispatches_prefers_the_engine_event() -> None:
    events = [{"type": "scheduler_exit_blocked", "ts": "2026-08-28T01:00:00+00:00",
               "run_id": "r1", "in_flight": ["t3"]}]
    obs, _ = _detect(events=events, driver=[_exit(sig.EXIT_BLOCKED_ORPHANED)])
    # ONE observation, not two: both logs describe the same stop.
    assert [o.signal for o in obs] == ["blocked_on_orphaned_dispatches"]
    assert "t3" in obs[0].text


def test_blocked_on_orphaned_dispatches_falls_back_to_the_driver_log() -> None:
    """The engine event and the driver record fail independently — a run killed before the
    event reached disk still has the driver's own exit record."""
    obs, _ = _detect(driver=[_exit(sig.EXIT_BLOCKED_ORPHANED)])
    assert [o.signal for o in obs] == ["blocked_on_orphaned_dispatches"]


def test_result_rejected_and_meta_proposal_failed_qualify() -> None:
    events = [
        {"type": "result_rejected", "ts": "2026-08-28T01:00:00+00:00", "run_id": "r1",
         "task_id": "t1", "stage": "implement", "attempt": 1, "reason": "lease_mismatch",
         "detail": "work item id did not match"},
        {"type": "meta_proposal_failed", "ts": "2026-08-28T02:00:00+00:00", "run_id": "r1",
         "key": "text:abc", "error": "tracker unavailable"},
    ]
    obs, _ = _detect(events=events)
    assert sorted(o.signal for o in obs) == ["meta_proposal_failed", "result_rejected"]
    rejected = next(o for o in obs if o.signal == "result_rejected")
    assert rejected.task_id == "t1"
    assert "lease_mismatch" in rejected.text


def test_deliver_reroute_is_expected_and_declined_but_counted() -> None:
    """DELIVER on codex is the DESIGNED #364 veto — normal operation, so it must not file.
    It is still counted in the returned notices, so "we looked and it was the known case"
    is a statement the run can make."""
    events = [
        {"type": "stage_rerouted_to_engine_lane", "ts": "2026-08-28T01:00:00+00:00",
         "run_id": "r1", "task_id": "t1", "stage": "deliver", "reason": "codex push",
         "from": "codex"},
    ]
    obs, notices = _detect(events=events)
    assert obs == []
    assert notices.declined["unexpected_engine_lane_reroute"] == 1


def test_non_deliver_reroute_is_an_undesigned_capability_loss() -> None:
    events = [
        {"type": "stage_rerouted_to_engine_lane", "ts": "2026-08-28T01:00:00+00:00",
         "run_id": "r1", "task_id": "t1", "stage": "implement", "reason": "???",
         "from": "codex"},
    ]
    obs, _ = _detect(events=events)
    assert [o.signal for o in obs] == ["unexpected_engine_lane_reroute"]


def test_orphaned_leases_come_from_the_events_audit_balance() -> None:
    obs, _ = _detect(facts=_facts(dispatch_orphans=(
        sig.DispatchOrphan("w-1", ts="2026-08-28T02:00:00+00:00"),
        sig.DispatchOrphan("w-2", ts="2026-08-28T01:00:00+00:00"),
    )))
    assert [o.signal for o in obs] == ["orphaned_dispatch_leases"]
    assert "w-1, w-2" in obs[0].text
    # No single record dates a whole-log imbalance, so the sighting takes the EARLIEST
    # orphaned dispatch's own timestamp — data-derived, and therefore identical on a
    # re-scan. Stamping the scanner's clock here would re-identify an unchanged orphan on
    # every pass and defeat the store's dedupe.
    assert obs[0].ts == "2026-08-28T01:00:00+00:00"


def test_orphan_sighting_identity_does_not_move_between_scans() -> None:
    """The regression behind the clock-stamped date: two scans of ONE unchanged log must
    produce the same fingerprint, or the observation store counts one orphan twice."""
    facts = _facts(dispatch_orphans=(sig.DispatchOrphan("w-1", ts="2026-08-28T01:00:00+00:00"),))
    first, _ = _detect(facts=facts)
    second, _ = _detect(facts=facts)
    assert [o.fingerprint() for o in first] == [o.fingerprint() for o in second]


def test_an_undated_orphan_is_left_undated_rather_than_invented() -> None:
    """A pre-#175 log carries no dispatch ts. Empty renders as "timestamp unavailable" —
    still stable, still never the scanner's clock."""
    obs, _ = _detect(facts=_facts(dispatch_orphans=(sig.DispatchOrphan("w-1"),)))
    assert obs[0].ts == ""
    assert "timestamp unavailable" in sig.signal_body(
        {"signal": "orphaned_dispatch_leases", "evidence": [obs[0].as_row()]}
    )


def test_a_clean_run_produces_nothing() -> None:
    events = [{"type": "stage_recorded", "ts": "2026-08-28T01:00:00+00:00", "run_id": "r1"}]
    obs, notices = _detect(events=events, driver=[_exit(sig.EXIT_NOTHING_DISPATCHABLE)],
                           facts=_facts(run_terminal=True, run_state="completed"))
    assert obs == []
    assert notices.declined == {}


# --- min_runs is a property of the signal ---------------------------------------------


def _row(signal: str, run_id: str, text: str = "x") -> dict:
    return sig.Observation(signal=signal, run_id=run_id, ts="2026-08-28T00:00:00+00:00",
                           text=text).as_row()


def test_threshold_is_per_signal_not_a_module_constant() -> None:
    """The #400 policy argument, as a test. A deterministic engine bug is believable on
    its first sighting; a stray attribution trailer is one model being a model until it
    happens again."""
    assert sig.SPECS_BY_ID["driver_exit_on_unfinished_work"].min_runs == 1
    assert sig.SPECS_BY_ID["commit_attribution_trailer"].min_runs == 2

    rows = [_row("driver_exit_on_unfinished_work", "r1"),
            _row("commit_attribution_trailer", "r1")]
    filed = {p["signal"] for p in sig.signal_proposals(rows)}
    held = {p["signal"] for p in sig.withheld_signals(rows)}
    assert filed == {"driver_exit_on_unfinished_work"}
    assert held == {"commit_attribution_trailer"}

    rows.append(_row("commit_attribution_trailer", "r2"))
    assert {p["signal"] for p in sig.signal_proposals(rows)} == {
        "driver_exit_on_unfinished_work", "commit_attribution_trailer"
    }
    assert sig.withheld_signals(rows) == []


def test_repeats_within_one_run_do_not_meet_a_two_run_threshold() -> None:
    rows = [_row("commit_attribution_trailer", "r1", text="a"),
            _row("commit_attribution_trailer", "r1", text="b")]
    assert sig.signal_proposals(rows) == []
    assert sig.withheld_signals(rows)[0]["runs"] == 1


def test_cluster_keys_cannot_collide_with_model_authored_ones() -> None:
    keys = {spec.key for spec in sig.SIGNAL_SPECS}
    assert all(k.startswith(sig.SIGNAL_KEY_PREFIX) for k in keys)
    model_keys = {
        meta.cluster_key({"text": "anything"}),
        meta.cluster_key({"target": {"kind": "stage-template", "ref": "REVIEW"}}),
    }
    assert not (keys & model_keys)


# --- the cross-run observation store --------------------------------------------------


def test_observation_store_is_idempotent(tmp_path) -> None:
    path = tmp_path / "engine-signals.jsonl"
    obs = [sig.Observation(signal="result_rejected", run_id="r1",
                           ts="2026-08-28T00:00:00+00:00", text="same")]
    assert len(sig.append_observations(path, obs)) == 1
    assert sig.append_observations(path, obs) == []  # a re-scan adds nothing
    assert len(sig.read_observations(path)) == 1


def test_observation_store_tolerates_a_torn_line(tmp_path) -> None:
    path = tmp_path / "engine-signals.jsonl"
    sig.append_observations(path, [sig.Observation("result_rejected", "r1", "t", "text")])
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"signal": "result_rejec')  # killed mid-append
    assert len(sig.read_observations(path)) == 1


# --- end to end through the Engine ----------------------------------------------------


def _run_with_driver_exit(eng: Engine, run_id: str, reason: str) -> None:
    """A run left mid-flight, with a driver that stopped on `reason`. Deliberately never
    finalized: that is the #399 shape the finalize-only seam could not see."""
    eng.create_run(run_id)
    eng.add_task(run_id, "t1", pipeline=[Stage.REVIEW])
    (eng.store.root / "driver.jsonl").write_text(
        json.dumps({"type": REC_EXIT, "ts": "2026-08-28T01:00:00+00:00",
                    "run_id": run_id, "reason": reason}) + "\n",
        encoding="utf-8",
    )


def test_early_exit_run_files_without_ever_finalizing(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _run_with_driver_exit(eng, "r1", sig.EXIT_NOTHING_DISPATCHABLE)

    rollup = eng.scan_engine_signals("r1")

    assert rollup["scanned"] is True
    assert rollup["signals"] == ["driver_exit_on_unfinished_work"]
    assert rollup["filed"] == 1
    assert len(project.task_source.followups) == 1
    filed = project.task_source.followups[0]
    assert "bug" in filed["labels"] and "meta-authoring" in filed["labels"]
    assert "#400" in filed["body"] and "no model authored this" in filed["body"]
    # The run never finalized — this is exactly the case the finalize-only seam missed.
    assert eng.store.load_run("r1").state.value != "completed"


def test_scan_is_idempotent_across_repeat_invocations(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _run_with_driver_exit(eng, "r1", sig.EXIT_NOTHING_DISPATCHABLE)

    eng.scan_engine_signals("r1")
    second = eng.scan_engine_signals("r1")

    assert second["new"] == 0
    assert len(project.task_source.followups) == 1  # no duplicate issue
    skipped = [e for e in eng.store.read_events("r1")
               if e["type"] == "meta_proposal_skipped"]
    assert skipped and skipped[0]["source"] == "engine"


def test_repeat_scan_of_an_orphaned_lease_adds_no_evidence(tmp_path, project) -> None:
    """The one signal no single event dates. Every OTHER signal takes its timestamp from a
    real record, so a scan-time stamp only broke this one — and it broke it on the most
    ordinary path there is, since a completed run scans twice (finalize, then the driver's
    exit). A second scan of an UNCHANGED orphan must add no row and, above all, must not
    comment "recurred" on the issue the first scan just filed."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW])
    # A lease opened and never closed by recorded/superseded/abandoned/reclaimed, held by
    # no live task — the #142 imbalance.
    eng.store.append_event("r1", {"ts": "2026-08-28T01:00:00+00:00",
                                  "type": "stage_dispatched", "run_id": "r1",
                                  "task_id": "t1", "stage": "review", "attempt": 0,
                                  "work_item_id": "w-orphan"})

    first = eng.scan_engine_signals("r1")
    second = eng.scan_engine_signals("r1")

    assert first["signals"] == ["orphaned_dispatch_leases"] and first["new"] == 1
    assert second["new"] == 0
    rows = sig.read_observations(eng._engine_signals_path())
    assert len(rows) == 1 and rows[0]["ts"] == "2026-08-28T01:00:00+00:00"
    assert len(project.task_source.followups) == 1
    assert project.task_source.comments == []  # nothing "recurred"


def test_concurrent_scanners_file_one_issue(tmp_path, project, monkeypatch) -> None:
    """Two engines sharing a runs root race the guard. Both are forced past detection
    before either may enter the per-cluster lock — without the locked ledger recheck, both
    would create an external issue for the same evidence."""
    import threading

    engines = [_engine(tmp_path, project) for _ in range(2)]
    _run_with_driver_exit(engines[0], "r1", sig.EXIT_NOTHING_DISPATCHABLE)
    barrier = threading.Barrier(2)
    original = engine_module.signal_proposals

    def synchronized(rows):
        proposals = original(rows)
        barrier.wait(timeout=10)
        return proposals

    monkeypatch.setattr(engine_module, "signal_proposals", synchronized)
    threads = [threading.Thread(target=e.scan_engine_signals, args=("r1",))
               for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(not t.is_alive() for t in threads)
    assert len(project.task_source.followups) == 1
    rows = [r for r in meta.read_filing_ledger(engines[0]._meta_proposals_path())
            if str(r["key"]).startswith("signal:")]
    assert len(rows) == 1


def test_signals_file_to_the_engine_tracker_not_the_product(tmp_path, project) -> None:
    """#380 must hold for this input too: a harness bug found during a product run belongs
    in the engine's tracker, never in the product's backlog."""
    engine_source = FakeTaskSource()
    eng = _engine(tmp_path, project, meta_task_source=engine_source)
    _run_with_driver_exit(eng, "r1", sig.EXIT_NOTHING_DISPATCHABLE)

    eng.scan_engine_signals("r1")

    assert len(engine_source.followups) == 1
    assert project.task_source.followups == []


def test_report_only_mode_files_nothing(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _run_with_driver_exit(eng, "r1", sig.EXIT_NOTHING_DISPATCHABLE)

    rollup = eng.scan_engine_signals("r1", file_proposals=False)

    assert rollup["observed"] == 1
    assert rollup["reportable"] == ["signal:driver_exit_on_unfinished_work"]
    assert project.task_source.followups == []


def test_scan_always_leaves_a_receipt_even_when_clean(tmp_path, project) -> None:
    """Clean and never-looked must not read alike."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")

    eng.scan_engine_signals("r1")

    scanned = [e for e in eng.store.read_events("r1")
               if e["type"] == "engine_signals_scanned"]
    assert len(scanned) == 1 and scanned[0]["observed"] == 0
    assert eng.status("r1")["meta_proposals"]["engine_scans"] == 1


def test_scan_never_raises_into_its_caller(tmp_path, project, monkeypatch) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    monkeypatch.setattr(
        eng, "_run_facts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("status store is gone")),
    )

    rollup = eng.scan_engine_signals("r1")

    assert rollup["error"] == "status store is gone"
    failures = [e for e in eng.store.read_events("r1") if e["type"] == "meta_proposal_failed"]
    assert failures and failures[0]["source"] == "engine"


def test_env_switch_disables_the_scan_independently_of_the_kb(
    tmp_path, project, monkeypatch
) -> None:
    """Turning off the learnings KB must NOT turn off harness-bug reporting: the KB is the
    model-authored plane, and #400 exists because these defects have no model author."""
    eng = _engine(tmp_path, project)
    _run_with_driver_exit(eng, "r1", sig.EXIT_NOTHING_DISPATCHABLE)

    monkeypatch.setenv("ORCHESTRATOR_NO_LEARNINGS_KB", "1")
    assert eng.scan_engine_signals("r1")["filed"] == 1

    monkeypatch.setenv("ORCHESTRATOR_NO_ENGINE_SIGNALS", "1")
    off = eng.scan_engine_signals("r1")
    assert off["scanned"] is False and "ORCHESTRATOR_NO_ENGINE_SIGNALS" in off["error"]


def test_in_flight_and_human_held_tasks_are_not_dispatchable_work(tmp_path, project) -> None:
    """`_run_facts` is where the #399 predicate gets its precision: each exclusion is a
    legitimate reason for the loop to find nothing to do."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "held", pipeline=[Stage.REVIEW])
    eng.add_task("r1", "inflight", pipeline=[Stage.REVIEW])
    eng.add_task("r1", "ready", pipeline=[Stage.REVIEW])
    eng.hold_for_approval("r1", "held", "needs a human")
    assert eng.next_work("r1", "inflight") is not None  # takes a lease

    facts = eng._run_facts("r1")

    assert facts.unfinished_tasks == ("ready",)


def test_exhausted_retry_budget_is_not_dispatchable_work(tmp_path, project) -> None:
    """A non-terminal task with no attempts left is undispatchable BY DESIGN, so a driver
    that stops on it has done nothing wrong."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW])
    eng.store.update_task("r1", "t1", lambda t: setattr(t, "attempt", t.max_attempts))

    assert eng.store.load_task("r1", "t1").state not in TERMINAL_TASK_STATES
    assert eng._run_facts("r1").unfinished_tasks == ()


def test_finalize_runs_the_scan(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW])
    work = eng.next_work("r1", "t1")
    assert work is not None
    eng.record("r1", make_result(work, structured_output={"approved": True, "issues": []}))

    assert eng.store.load_run("r1").state.value == "completed"
    assert any(e["type"] == "engine_signals_scanned" for e in eng.store.read_events("r1"))


# --- the standalone CLI check ---------------------------------------------------------


def _cli_run_with_exit(root, run_id: str, reason: str) -> None:
    """Build a run through the CLI, then leave a driver exit record in its log dir — the
    post-hoc shape a human or CI inspects after a run that ended badly."""
    base = ["--root", str(root), "--run", run_id, "--shared-root",
            "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "micro"])
    main([*base, "add-task", "--task", "t1"])
    log = root / run_id / "driver.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"type": REC_EXIT, "ts": "2026-08-28T01:00:00+00:00",
                    "run_id": run_id, "reason": reason}) + "\n",
        encoding="utf-8",
    )


def test_cli_exits_nonzero_when_a_signal_fired(tmp_path, capsys) -> None:
    _cli_run_with_exit(tmp_path, "r1", sig.EXIT_NOTHING_DISPATCHABLE)
    capsys.readouterr()

    rc = main(["--root", str(tmp_path), "--run", "r1", "--shared-root",
               "--project", "tests.fakeproject", "engine-signals"])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["signals"] == ["driver_exit_on_unfinished_work"]
    assert out["filed"] == 1


def test_cli_exits_zero_on_a_clean_run(tmp_path, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "r1", "--shared-root",
            "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "micro"])
    capsys.readouterr()

    rc = main([*base, "engine-signals"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["observed"] == 0


def test_cli_no_file_reports_without_filing(tmp_path, capsys) -> None:
    _cli_run_with_exit(tmp_path, "r1", sig.EXIT_NOTHING_DISPATCHABLE)
    capsys.readouterr()

    rc = main(["--root", str(tmp_path), "--run", "r1", "--shared-root",
               "--project", "tests.fakeproject", "engine-signals", "--no-file"])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["observed"] == 1 and out["filed"] == 0


# --- the driver's own exit path -------------------------------------------------------


def test_scheduler_scans_on_the_way_out(tmp_path, project, monkeypatch) -> None:
    """The #399 trigger, reproduced. The loop is made to see nothing dispatchable while
    the run genuinely has eligible work — the cooldown-boundary race — and stops. The scan
    runs AFTER `log.exit` writes the record it reads, and on a run that, having never
    finished, never reaches finalize at all."""
    from orchestrator.scheduler import Scheduler

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW])
    assert eng.dispatchable("r1") == ["t1"]  # the work the driver is about to walk away from
    monkeypatch.setattr(Scheduler, "_plan", lambda self, *a, **k: ([], 1))

    status = Scheduler(eng, max_concurrent=1).run("r1", lambda items: [])

    assert status["scheduler"]["exit_reason"] == sig.EXIT_NOTHING_DISPATCHABLE
    assert eng.store.load_run("r1").state.value != "completed"  # never finalized
    assert any(e["type"] == "engine_signals_scanned" for e in eng.store.read_events("r1"))
    filed = [f for f in project.task_source.followups if "driver exited" in f["body"]]
    assert len(filed) == 1
    assert "t1" in filed[0]["body"]


def test_a_driver_that_stops_on_a_finished_run_files_nothing(tmp_path, project) -> None:
    """The overwhelmingly common exit, and the one that must stay silent."""
    from orchestrator.scheduler import Scheduler

    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", pipeline=[Stage.REVIEW])

    def runner(items):
        return [make_result(w, structured_output={"approved": True, "issues": []})
                for w in items]

    status = Scheduler(eng, max_concurrent=1).run("r1", runner)

    assert status["scheduler"]["exit_reason"] == sig.EXIT_NOTHING_DISPATCHABLE
    assert eng.store.load_run("r1").state.value == "completed"
    assert project.task_source.followups == []
