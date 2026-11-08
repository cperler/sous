"""#642: a TEST that only timed out on a loaded host must not fail the task.

Run pred-1485-1101 had three tasks in TEST at once; their suites overloaded the host and each
hit the hardcoded 900 s cap. The runner reported ``failure_kinds: ["infra"]``, but the
project's classifier did not recognise ``<unit timeout>``, so the engine treated two timeouts
as the same code failure and the breaker failed a task whose fix was correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.execution.deterministic_setup import DeterministicSetupRunner
from adapters.execution.deterministic_test import (
    DEFAULT_TEST_TIMEOUT_S,
    DeterministicTestRunner,
    resolve_test_timeout,
)
from orchestrator import scaffold
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    TaskState,
)
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

TIMED_OUT = {
    "passed": False, "failures": ["<unit timeout>"], "failure_kinds": ["infra"],
    "validation_notes": "unit: TIMED OUT after 900s",
}


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _to_test(eng, task="t1", run="r1"):
    while True:
        w = eng.next_work(run, task)
        assert w is not None
        if w.stage is Stage.TEST:
            return w
        eng.record(run, make_result(w))


def _timed_out(eng, w, *, mode=ExecutionMode.ENGINE, provider=Provider.NONE, run="r1"):
    return eng.record(run, make_result(
        w, status=ResultStatus.FAILURE, error="test stage red", structured_output=TIMED_OUT,
        mode=mode, provider=provider,
    ))


def _events(eng, kind, run="r1") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e["type"] == kind]


def _two_tasks(tmp_path, project) -> Engine:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    return eng


# --- the declared timeout ---------------------------------------------------------------


def test_the_runner_uses_the_projects_declared_timeout(tmp_path, project) -> None:
    project.test_timeout_s = 1
    project.test_unit_cmd = lambda files=None: ["sleep", "5"]
    project.test_e2e_cmd = lambda files=None: ["true"]
    project.test_shell_cmd = lambda files=None: ["true"]
    run = DeterministicTestRunner(project).evaluate(_work(tmp_path))
    assert not run.ok
    assert run.out["failure_kinds"] == ["infra"]
    assert "unit: TIMED OUT after 1s" in run.out["validation_notes"]


def test_a_callable_declaration_is_read_too(project) -> None:
    project.test_timeout_s = lambda: 1800
    assert resolve_test_timeout(project) == (1800, None)


def test_no_declaration_means_the_900s_default(project) -> None:
    assert resolve_test_timeout(project) == (DEFAULT_TEST_TIMEOUT_S, None) == (900, None)


@pytest.mark.parametrize("bad", [0, -5, "1800", True, 12.5])
def test_an_unusable_declaration_falls_back_with_a_notice(project, bad) -> None:
    project.test_timeout_s = bad
    value, notice = resolve_test_timeout(project)
    assert value == 900
    assert notice is not None and notice["notice"] == "test_timeout_invalid"


def test_a_raising_declaration_falls_back_with_a_notice(project) -> None:
    def boom():
        raise RuntimeError("no")

    project.test_timeout_s = boom
    value, notice = resolve_test_timeout(project)
    assert value == 900 and "RuntimeError" in str(notice["detail"])


def test_the_invalid_notice_reaches_the_test_result(tmp_path, project) -> None:
    project.test_timeout_s = 0
    run = DeterministicTestRunner(project).evaluate(_work(tmp_path))
    assert run.ok
    assert "test_timeout_invalid" in [n["notice"] for n in run.notices]


def test_an_explicit_timeout_wins_over_the_declaration(tmp_path, project) -> None:
    project.test_timeout_s = 1800
    project.test_unit_cmd = lambda files=None: ["sleep", "5"]
    run = DeterministicTestRunner(project, timeout_s=1).evaluate(_work(tmp_path))
    assert "unit: TIMED OUT after 1s" in run.out["validation_notes"]


def test_the_intake_baseline_uses_the_same_declared_timeout(tmp_path, project) -> None:
    project.test_timeout_s = 1
    out = DeterministicSetupRunner(project)._run_baseline(tmp_path, ["sleep", "5"], None)
    assert out == {"captured": False, "failures": [], "note": "baseline run timed out (1s)"}


def test_an_origin_probe_that_only_timed_out_reports_infra(tmp_path, project, monkeypatch) -> None:
    from adapters.execution import worktree_origin

    monkeypatch.setattr(worktree_origin, "_PROBE_TIMEOUT_S", 1)
    project.worktree_origin_probes = lambda: [("slow", ["sleep", "5"], "source")]
    run = DeterministicTestRunner(project).evaluate(_work(tmp_path))
    assert not run.ok and run.refused
    assert run.out["failure_kinds"] == ["infra"]


def test_an_origin_mismatch_is_still_not_infra(tmp_path, project) -> None:
    project.worktree_origin_probes = lambda: [("elsewhere", ["echo", "/usr/bin/env"], "source")]
    run = DeterministicTestRunner(project).evaluate(_work(tmp_path))
    assert not run.ok and run.refused and run.out is None


def test_a_mismatch_beside_a_probe_timeout_is_not_infra(tmp_path, project, monkeypatch) -> None:
    """Only an all-timeout probe set is infra: a probe that resolved OUTSIDE the worktree is
    a real mismatch, even when another probe timed out, and must not re-run for free."""
    from adapters.execution import worktree_origin

    monkeypatch.setattr(worktree_origin, "_PROBE_TIMEOUT_S", 1)
    project.worktree_origin_probes = lambda: [
        ("elsewhere", ["echo", "/usr/bin/env"], "source"),
        ("slow", ["sleep", "5"], "source"),
    ]
    run = DeterministicTestRunner(project).evaluate(_work(tmp_path))
    assert not run.ok and run.refused and run.out is None


# --- the engine treats a runner-reported timeout as infra ------------------------------


def test_a_runner_timeout_is_infra_even_when_the_classifier_disagrees(tmp_path, project) -> None:
    """The fixture's classifier tags every line ``unit``, like the prediction scaffold's
    classifier returned nothing infra for ``<unit timeout>``."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    for _ in range(2):  # pred-1485-1101's #1031: the second timeout tripped the breaker
        out = _timed_out(eng, _to_test(eng))
        assert out["outcome"] == "stage_infra_reset_retry"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.RETRYING and task.infra_resets == 2
    assert task.error_signatures == []  # never stacks the breaker's streak
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST and w.attempt == 0  # no attempt spent
    classified = _events(eng, "failure_classified")
    assert [e["infra_only"] for e in classified] == [True, True]
    assert all(e["runner_reported_infra"] for e in classified)


def test_past_the_reset_budget_it_retries_without_the_breaker(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_infra_resets=0)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    assert _timed_out(eng, _to_test(eng))["outcome"] == "stage_failed_will_retry"
    assert _timed_out(eng, _to_test(eng))["outcome"] == "stage_failed_will_retry"
    assert eng.store.load_task("r1", "t1").state is TaskState.RETRYING


def test_a_model_lane_claim_of_infra_is_not_taken_on_trust(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", deterministic_stages=())
    w = _to_test(eng)
    out = _timed_out(eng, w, mode=w.lane_policy.execution_mode, provider=w.lane_policy.provider)
    assert out["outcome"] == "stage_failed_will_retry"
    assert eng.store.load_task("r1", "t1").test_wait_for_quiet is False


# --- the retry waits until no other TEST is running ------------------------------------


def test_the_retry_waits_while_another_task_runs_test(tmp_path, project) -> None:
    eng = _two_tasks(tmp_path, project)
    _timed_out(eng, _to_test(eng, "t1"))
    assert eng.store.load_task("r1", "t1").test_wait_for_quiet is True
    assert len(_events(eng, "test_retry_waiting_for_quiet")) == 1
    busy = _to_test(eng, "t2")  # t2's TEST is now in flight
    assert "t1" not in eng.dispatchable("r1")
    assert eng.next_work("r1", "t1") is None  # the direct per-task path holds it too
    eng.record("r1", make_result(busy))
    assert "t1" in eng.dispatchable("r1")
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.TEST
    assert eng.store.load_task("r1", "t1").test_wait_for_quiet is False
    dispatched = [e for e in _events(eng, "stage_dispatched")
                  if e["task_id"] == "t1" and e["stage"] == "test"]
    assert "waited_for_quiet" not in dispatched[0]
    assert dispatched[-1]["waited_for_quiet"] is True


def test_waiters_are_released_one_at_a_time(tmp_path, project) -> None:
    eng = _two_tasks(tmp_path, project)
    _timed_out(eng, _to_test(eng, "t1"))
    _timed_out(eng, _to_test(eng, "t2"))
    ready = eng.dispatchable("r1")
    assert "t1" in ready and "t2" not in ready
    eng.next_work("r1", "t1")
    assert "t2" not in eng.dispatchable("r1")  # t1's re-run is now in flight


def test_a_waiter_does_not_hold_a_task_entering_test_for_the_first_time(tmp_path, project) -> None:
    eng = _two_tasks(tmp_path, project)
    _timed_out(eng, _to_test(eng, "t1"))
    eng.next_work("r1", "t1")  # t1's re-run is in flight
    assert _to_test(eng, "t2").stage is Stage.TEST


def test_only_a_test_lease_counts_as_a_test_in_flight(tmp_path, project) -> None:
    """A sibling leased on IMPLEMENT is not running a suite, so it neither holds a waiter
    nor counts toward ``tests_in_flight`` on the waiter's TEST dispatch."""
    eng = _two_tasks(tmp_path, project)
    _timed_out(eng, _to_test(eng, "t1"))
    while (w := eng.next_work("r1", "t2")).stage is not Stage.IMPLEMENT:
        eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t2").pending_work_item_id is not None  # leased
    assert "t1" in eng.dispatchable("r1")
    assert eng.next_work("r1", "t1").stage is Stage.TEST
    dispatched = [e for e in _events(eng, "stage_dispatched")
                  if e["task_id"] == "t1" and e["stage"] == "test"]
    assert dispatched[-1]["tests_in_flight"] == 0


# --- the dispatch event says how loaded the host was -----------------------------------


def test_a_test_dispatch_records_the_in_flight_count_and_load(tmp_path, project) -> None:
    eng = _two_tasks(tmp_path, project)
    _to_test(eng, "t1")
    _to_test(eng, "t2")
    by_task = {e["task_id"]: e for e in _events(eng, "stage_dispatched") if e["stage"] == "test"}
    assert by_task["t1"]["tests_in_flight"] == 0
    assert by_task["t2"]["tests_in_flight"] == 1
    assert isinstance(by_task["t2"]["load_avg_1m"], float)
    others = [e for e in _events(eng, "stage_dispatched") if e["stage"] != "test"]
    assert others and not any("tests_in_flight" in e or "load_avg_1m" in e for e in others)


# --- profile.toml carries the timeout into the generated adapter -----------------------


def test_profile_test_timeout_round_trips_and_reaches_the_config(tmp_path) -> None:
    profile = scaffold.Profile(name="demo", languages=["python"], test={"timeout_s": 1800})
    path = tmp_path / "profile.toml"
    scaffold.write_profile(profile, path)
    assert "[test]\ntimeout_s = 1800" in path.read_text()
    assert scaffold.read_profile(path).test == {"timeout_s": 1800}
    assert "    test_timeout_s = 1800\n" in scaffold.render_config("demo", profile)


def test_an_undeclared_timeout_generates_nothing() -> None:
    profile = scaffold.Profile(name="demo", languages=["python"])
    assert "[test]" not in scaffold.profile_to_toml(profile)
    assert "test_timeout_s" not in scaffold.render_config("demo", profile)


def test_a_regeneration_keeps_the_declared_timeout() -> None:
    manifest = scaffold.load_kit_manifest()
    existing = scaffold.Profile(name="demo", languages=["python"], test={"timeout_s": 1800})
    incoming = scaffold.profile_from_languages("demo", ["python"], manifest)
    assert scaffold.merge_profiles(existing, incoming, manifest).test == {"timeout_s": 1800}


# --- helpers ----------------------------------------------------------------------------


def _work(tmp_path: Path):
    """A TEST WorkItem pointed at an empty worktree, from a throwaway engine run."""
    from tests.conftest import FakeProject

    root = tmp_path / "_work_store"
    eng = Engine(StatusStore(root), CostLedger(root / "costs.jsonl"), FakeProject())
    eng.create_run("w", ExecutionLane.FULL)
    eng.add_task("w", "t")
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return _to_test(eng, "t", run="w").model_copy(update={"cwd": str(wt)})
