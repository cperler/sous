"""Serialize tasks whose approved SCOPE names the same load-bearing file (#377).

`batch-plan`/`dispatchable` fanned tasks out on DAG readiness alone, so two tasks that
both rewrite a schema module were dispatched in parallel and only met at merge — where
git either conflicts noisily or auto-merges into a runtime break (#370's `ff-v1-b3b`:
five branches each claiming "the next" SCHEMA_VERSION, a 2-tuple→3-tuple return change
auto-merging cleanly against new 2-value call sites, a CLI subcommand registered twice).

These tests pin both halves: the PURE normalization/planning rules (no clock, no I/O),
and the engine gate that holds a waiter out of the dispatchable set, releases it when the
blocker goes terminal — completed OR failed — and stays quiet across ticks.
"""

from __future__ import annotations

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.file_contention import (
    MAX_CLAIMS,
    MAX_PATH_LEN,
    ClaimEntry,
    normalize_claims,
    plan_deferrals,
)
from orchestrator.schemas.enums import ExecutionLane, ResultStatus, Stage, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


def _drive_to_scope(eng: Engine, run: str, task: str, files: list[str] | None) -> None:
    """Run INTAKE + SCOPE for a task, declaring ``files`` (None = no declaration)."""
    for stage in (Stage.INTAKE, Stage.SCOPE):
        work = eng.next_work(run, task)
        assert work is not None and work.stage is stage
        output = None
        if stage is Stage.SCOPE:
            output = {"feasible": True, "plan": ["do it"]}
            if files is not None:
                output["files"] = files
        eng.record(run, make_result(work, structured_output=output))


# --- pure: normalization ------------------------------------------------------


def test_claims_are_repo_relative_deduped_and_order_preserving() -> None:
    claims, notices = normalize_claims(
        ["orchestrator/engine.py", "./orchestrator/engine.py", "a/../b.py", " c.py "]
    )
    assert claims == ("orchestrator/engine.py", "b.py", "c.py")
    assert notices == []


def test_unusable_paths_are_dropped_with_a_notice_never_silently() -> None:
    # A path the engine cannot use must not read as "this task touches nothing" — that
    # reading is what lets the task fan out into the collision the gate exists to prevent.
    claims, notices = normalize_claims(["/etc/passwd", "../outside.py", "", 7, ".", "ok.py"])
    assert claims == ("ok.py",)
    assert [n["reason"] for n in notices] == [
        "absolute_path", "escapes_repo_root", "empty", "not_a_string", "escapes_repo_root",
    ]


def test_a_malformed_declaration_yields_no_claims_and_one_notice() -> None:
    # Total, never an exception into record(): a model can return anything here.
    for raw in ("engine.py", {"a": 1}, 5):
        claims, notices = normalize_claims(raw)
        assert claims == ()
        assert [n["reason"] for n in notices] == ["not_a_list"]
    assert normalize_claims(None) == ((), [])


def test_an_over_long_path_is_dropped_not_truncated() -> None:
    # Truncating would produce a DIFFERENT path that silently matches its own prefix.
    long_path = "a/" + "b" * MAX_PATH_LEN
    claims, notices = normalize_claims([long_path, "fine.py"])
    assert claims == ("fine.py",)
    assert notices[0]["reason"] == "too_long"


def test_claim_count_is_capped_and_says_how_much_it_dropped() -> None:
    claims, notices = normalize_claims([f"f{i}.py" for i in range(MAX_CLAIMS + 3)])
    assert len(claims) == MAX_CLAIMS
    assert notices[0]["reason"] == "claim_cap"
    assert notices[0]["dropped"] == 3


# --- pure: the deferral plan --------------------------------------------------


def test_a_waiter_defers_to_a_holder_and_names_the_contended_paths() -> None:
    plan = plan_deferrals([
        ClaimEntry("t1", ("schema.py", "other.py"), holding=True),
        ClaimEntry("t2", ("schema.py", "mine.py"), holding=False),
    ])
    assert plan.admitted == ()
    assert plan.deferrals["t2"].blocked_by == ("t1",)
    assert plan.deferrals["t2"].paths == ("schema.py",)


def test_disjoint_claims_all_dispatch_together() -> None:
    plan = plan_deferrals([
        ClaimEntry("t1", ("a.py",), holding=False),
        ClaimEntry("t2", ("b.py",), holding=False),
    ])
    assert plan.admitted == ("t1", "t2")
    assert plan.deferrals == {}


def test_simultaneous_waiters_break_the_tie_by_run_order() -> None:
    # Exactly one waiter per contended path is admitted per pass, and the SAME one on
    # every tick — a coin flip here would keep re-picking a different winner and stall.
    entries = [
        ClaimEntry("t1", ("shared.py",), holding=False),
        ClaimEntry("t2", ("shared.py",), holding=False),
        ClaimEntry("t3", ("shared.py",), holding=False),
    ]
    for _ in range(3):
        plan = plan_deferrals(entries)
        assert plan.admitted == ("t1",)
        assert set(plan.deferrals) == {"t2", "t3"}
        assert plan.deferrals["t2"].blocked_by == ("t1",)


def test_a_pass_can_never_defer_everybody() -> None:
    # The no-deadlock property: with no holder, the first waiter in the order always wins,
    # so the wait graph can never close into a cycle.
    entries = [
        ClaimEntry("t1", ("a.py", "b.py"), holding=False),
        ClaimEntry("t2", ("b.py", "c.py"), holding=False),
        ClaimEntry("t3", ("c.py", "a.py"), holding=False),
    ]
    plan = plan_deferrals(entries)
    assert plan.admitted == ("t1",)
    assert set(plan.deferrals) == {"t2", "t3"}


def test_a_blocker_left_out_of_the_entries_releases_its_paths() -> None:
    # Release is by omission: the engine passes only LIVE tasks, so a terminal blocker —
    # completed or failed — stops holding anything and the waiter is admitted.
    plan = plan_deferrals([ClaimEntry("t2", ("schema.py",), holding=False)])
    assert plan.admitted == ("t2",)
    assert plan.deferrals == {}


def test_a_task_that_declared_nothing_neither_defers_nor_blocks() -> None:
    plan = plan_deferrals([
        ClaimEntry("t1", (), holding=True),
        ClaimEntry("t2", (), holding=False),
    ])
    assert plan.admitted == ()
    assert plan.deferrals == {}


# --- the fold: SCOPE's declaration lands on the task doc ----------------------


def test_scope_files_fold_onto_the_task_document(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive_to_scope(eng, "r1", "t1", ["orchestrator/engine.py", "./orchestrator/engine.py"])

    task = eng.store.load_task("r1", "t1")
    assert task.scope_files == ["orchestrator/engine.py"]
    # NOT the context plane: a context key can be evicted by the whole-context ceiling,
    # and an evicted claim would silently un-serialize the run.
    assert "files" not in task.context


def test_a_dropped_claim_is_evented_rather_than_silently_ignored(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive_to_scope(eng, "r1", "t1", ["/etc/passwd", "kept.py"])

    assert eng.store.load_task("r1", "t1").scope_files == ["kept.py"]
    dropped = [e for e in eng.store.read_events("r1") if e["type"] == "scope_file_claim_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["reason"] == "absolute_path"
    assert dropped[0]["stage"] == "scope"


def test_a_scope_without_files_leaves_the_claim_empty(tmp_path, project) -> None:
    # Pre-#377 runs and lanes whose SCOPE declares nothing must keep fanning out.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive_to_scope(eng, "r1", "t1", None)
    assert eng.store.load_task("r1", "t1").scope_files == []


# --- the gate inside dispatchable --------------------------------------------


def _two_scoped_tasks(tmp_path, project, files_a, files_b, **run_kwargs) -> Engine:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL, **run_kwargs)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")
    _drive_to_scope(eng, "r1", "t1", files_a)
    _drive_to_scope(eng, "r1", "t2", files_b)
    return eng


def test_colliding_tasks_are_serialized_not_fanned_out(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py", "cli.py"])

    assert eng.dispatchable("r1") == ["t1"]  # t2 waits rather than racing t1 to the file
    # ... and stays waiting while t1 is in flight on its own stage.
    eng.next_work("r1", "t1")
    assert eng.dispatchable("r1") == []
    assert eng.store.load_task("r1", "t2").file_contention_deferred_on == ["t1"]
    assert eng.store.load_task("r1", "t1").file_claim_acquired_at is not None


def test_disjoint_declarations_still_run_in_parallel(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["a.py"], ["b.py"])
    assert eng.dispatchable("r1") == ["t1", "t2"]


def test_an_undeclared_task_is_never_gated(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], None)
    assert eng.dispatchable("r1") == ["t1", "t2"]


def test_the_claim_releases_when_the_blocker_completes(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    assert eng.dispatchable("r1") == ["t1"]

    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED

    assert eng.dispatchable("r1") == ["t2"]
    assert eng.store.load_task("r1", "t2").file_contention_deferred_on == []


def test_a_failed_blocker_does_not_starve_its_waiter(tmp_path, project) -> None:
    # Release is by TERMINAL state, not by success — otherwise a task that will never
    # finish holds its files for the rest of the run.
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    assert eng.dispatchable("r1") == ["t1"]

    for _ in range(5):
        work = eng.next_work("r1", "t1")
        if work is None:
            break
        eng.record("r1", make_result(work, status=ResultStatus.FAILURE, error="boom"))
    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED

    assert eng.dispatchable("r1") == ["t2"]


def test_a_holder_parked_at_a_human_gate_still_owns_its_files(tmp_path, project) -> None:
    # A parked task is non-terminal and its worktree still holds the edits; letting a
    # waiter past it would be the exact collision the gate exists to prevent.
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    assert eng.dispatchable("r1") == ["t1"]  # t1 acquires
    eng.hold_for_approval("r1", "t1", what="live PR to the product repo")

    assert eng.store.load_task("r1", "t1").state is TaskState.BLOCKED_ON_HUMAN
    assert eng.dispatchable("r1") == []


def test_the_run_setting_off_restores_full_fan_out(tmp_path, project) -> None:
    eng = _two_scoped_tasks(
        tmp_path, project, ["schema.py"], ["schema.py"], serialize_file_contention=False
    )
    assert eng.dispatchable("r1") == ["t1", "t2"]
    # No claim is taken either, so flipping the setting on later starts from a clean slate.
    assert eng.store.load_task("r1", "t1").file_claim_acquired_at is None


def test_the_wait_is_evented_once_not_once_per_tick(tmp_path, project) -> None:
    # dispatchable() runs every scheduler tick; an unconditional emit would write a line
    # per waiting task per tick and bury the timeline the event exists to explain.
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    for _ in range(4):
        eng.dispatchable("r1")

    events = eng.store.read_events("r1")
    deferred = [e for e in events if e["type"] == "dispatch_deferred_file_contention"]
    acquired = [e for e in events if e["type"] == "file_claim_acquired"]
    assert len(deferred) == 1
    assert deferred[0]["task_id"] == "t2"
    assert deferred[0]["blocked_by"] == ["t1"]
    assert deferred[0]["files"] == ["schema.py"]
    assert len(acquired) == 1
    assert acquired[0]["task_id"] == "t1"


def test_status_explains_why_a_waiting_task_is_sitting_still(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    eng.dispatchable("r1")

    tasks = eng.status("r1")["tasks"]
    assert tasks["t2"]["file_contention"] == {"blocked_by": ["t1"], "files": ["schema.py"]}
    assert tasks["t1"]["file_claim"]["files"] == ["schema.py"]


# --- the same gate at next_work, for direct per-task callers ------------------
#
# `orchestrator next --task <id>` (and the per-task interactive supervisor over it) never
# calls dispatchable(), so a gate enforced only in the eligibility predicate would be
# silently absent from a real, supported lane. next_work is self-safe for the same reason
# its terminal and BLOCKED_ON_HUMAN guards are.


def test_next_work_defers_a_collision_it_was_asked_for_directly(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py", "cli.py"])

    # Deliberately NOT routed through dispatchable() — this is the direct CLI path.
    first = eng.next_work("r1", "t1")
    assert first is not None and first.stage is Stage.IMPLEMENT
    assert eng.next_work("r1", "t2") is None  # would have raced t1 to schema.py

    t2 = eng.store.load_task("r1", "t2")
    assert t2.file_claim_acquired_at is None  # refused, so it took no claim
    assert t2.state is not TaskState.BLOCKED_ON_HUMAN  # a wait, not a human gate
    deferred = [
        e for e in eng.store.read_events("r1")
        if e["type"] == "dispatch_deferred_file_contention"
    ]
    assert len(deferred) == 1  # quiet to the caller, never silent in the timeline
    assert deferred[0]["task_id"] == "t2"
    assert deferred[0]["blocked_by"] == ["t1"]
    assert deferred[0]["files"] == ["schema.py"]  # only the CONTENDED path


def test_next_work_acquires_the_claim_when_the_files_are_free(tmp_path, project) -> None:
    # The other half of the backstop: a direct caller must also TAKE the claim, or a
    # later dispatchable() pass would hand the same file to a second task.
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])

    assert eng.next_work("r1", "t1") is not None
    assert eng.store.load_task("r1", "t1").file_claim_acquired_at is not None
    assert eng.dispatchable("r1") == []  # t2 is held by the claim next_work took


def test_next_work_releases_a_waiter_once_the_blocker_is_terminal(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    # With no holder yet the FIRST caller to ask wins the file, so t1 must take it before
    # t2 can be a waiter at all.
    first = eng.next_work("r1", "t1")
    assert first is not None
    assert eng.next_work("r1", "t2") is None

    eng.record("r1", make_result(first))
    while (work := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(work))
    assert eng.store.load_task("r1", "t1").state is TaskState.COMPLETED

    freed = eng.next_work("r1", "t2")
    assert freed is not None and freed.stage is Stage.IMPLEMENT
    assert eng.store.load_task("r1", "t2").file_claim_acquired_at is not None


def test_a_holder_keeps_dispatching_its_own_later_stages(tmp_path, project) -> None:
    # The claim must not gate the task that owns it — otherwise a holder would deadlock
    # against itself at the stage after the one that acquired it.
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], ["schema.py"])
    work = eng.next_work("r1", "t1")
    assert work is not None
    eng.record("r1", make_result(work))

    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is not Stage.IMPLEMENT


def test_next_work_leaves_undeclared_and_disjoint_tasks_alone(tmp_path, project) -> None:
    eng = _two_scoped_tasks(tmp_path, project, ["schema.py"], None)
    assert eng.next_work("r1", "t1") is not None
    assert eng.next_work("r1", "t2") is not None  # declared nothing: never gated

    other = _two_scoped_tasks(tmp_path / "b", project, ["a.py"], ["b.py"])
    assert other.next_work("r1", "t1") is not None
    assert other.next_work("r1", "t2") is not None


def test_next_work_ignores_contention_when_the_run_setting_is_off(tmp_path, project) -> None:
    eng = _two_scoped_tasks(
        tmp_path, project, ["schema.py"], ["schema.py"], serialize_file_contention=False
    )
    assert eng.next_work("r1", "t1") is not None
    assert eng.next_work("r1", "t2") is not None
    assert eng.store.load_task("r1", "t2").file_claim_acquired_at is None
