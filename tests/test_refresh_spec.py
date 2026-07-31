"""Sanctioned refresh of a task's snapshotted spec, and the staleness signal (#271).

A task's title/body are snapshotted onto the Task doc at ``add_task`` and every stage prompt
for the rest of the run renders from that copy. Amending the upstream issue mid-run used to
reach nothing, and nothing said so — the workarounds were rebuilding the run or hand-patching
the status JSON behind the engine's back. This exercises the sanctioned operation:

  - ``add_task`` stamps the snapshot's provenance (captured_at / source updated_at / fingerprint);
  - ``refresh_spec`` lands an amended spec on the doc AND on the next rendered prompt;
  - ``task_spec_refreshed`` is emitted with a diff summary — including on a no-op refresh,
    so "verified identical" and "never looked" do not read alike;
  - the lease guard refuses while a dispatch is outstanding, ``force=True`` overrides loudly;
  - a terminal task is refused;
  - ``check_only`` is a genuine dry run (no doc write, no event);
  - ``status(check_spec=True)`` flags a diverged task and degrades (never raises) on a
    source failure, while the default status output stays untouched;
  - the pure diff/fingerprint helpers behave.
"""

from __future__ import annotations

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.schemas.enums import Stage, TaskState
from orchestrator.spec_refresh import diff_summary, fingerprint
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _started(eng: Engine, *, run: str = "r1", task: str = "t1") -> None:
    """Run through INTAKE so the task is live but not holding a lease."""
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))


def _mid_dispatch(eng: Engine, *, run: str = "r1", task: str = "t1") -> None:
    """Leave a dispatch outstanding on SCOPE — its prompt is already rendered."""
    _started(eng, run=run, task=task)
    work = eng.next_work(run, task)
    assert work is not None and work.stage is Stage.SCOPE


def _events(eng: Engine, run: str = "r1", kind: str = "task_spec_refreshed") -> list[dict]:
    return [e for e in eng.store.read_events(run) if e.get("type") == kind]


# --- snapshot provenance ----------------------------------------------------


def test_add_task_stamps_snapshot_provenance(tmp_path, project) -> None:
    project.task_source.spec_overrides["t1"] = {"updated_at": "2026-07-30T10:00:00Z"}
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1")

    assert task.spec_captured_at  # when this copy was taken
    assert task.spec_source_updated_at == "2026-07-30T10:00:00Z"
    assert task.spec_fingerprint == fingerprint(task.title, task.body)
    # None distinguishes an original capture from a refreshed one.
    assert task.spec_refreshed_at is None


# --- refresh ----------------------------------------------------------------


def test_refresh_lands_amended_spec_on_doc_and_next_prompt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)
    before = eng.store.load_task("r1", "t1")

    project.task_source.spec_overrides["t1"] = {
        "title": "Amended title",
        "body": "MUST also handle SubCall.schema_retries",
        "updated_at": "2026-07-31T01:02:03Z",
    }
    report = eng.refresh_spec("r1", "t1")

    assert report["changed"] is True and report["applied"] is True
    task = eng.store.load_task("r1", "t1")
    assert task.title == "Amended title"
    assert "schema_retries" in task.body
    assert task.spec_fingerprint == fingerprint(task.title, task.body)
    assert task.spec_source_updated_at == "2026-07-31T01:02:03Z"
    assert task.spec_refreshed_at and task.spec_refreshed_at == task.spec_captured_at
    assert task.spec_captured_at != before.spec_captured_at

    # The point of the whole exercise: the NEXT rendered prompt carries the amendment.
    work = eng.next_work("r1", "t1")
    assert work is not None
    assert "schema_retries" in work.prompt
    assert before.body not in work.prompt or before.body == task.body


def test_refresh_emits_event_with_diff_summary(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)
    project.task_source.spec_overrides["t1"] = {"body": "do it\nand also this"}
    eng.refresh_spec("r1", "t1")

    events = _events(eng)
    assert len(events) == 1
    evt = events[0]
    assert evt["task_id"] == "t1" and evt["changed"] is True
    assert evt["leased_dispatch"] is False and evt["forced"] is False
    diff = evt["diff"]
    assert diff["body_changed"] is True and diff["title_changed"] is False
    assert diff["body"]["lines_added"] == 1 and diff["body"]["lines_removed"] == 0
    assert diff["old_fingerprint"] != diff["new_fingerprint"]


def test_noop_refresh_is_still_a_receipt(tmp_path, project) -> None:
    """Clean and never-looked must not read alike (#322's convention)."""
    eng = _engine(tmp_path, project)
    _started(eng)
    report = eng.refresh_spec("r1", "t1")  # nothing changed upstream

    assert report["changed"] is False and report["applied"] is True
    events = _events(eng)
    assert len(events) == 1 and events[0]["changed"] is False
    assert events[0]["diff"]["old_fingerprint"] == events[0]["diff"]["new_fingerprint"]


# --- guards -----------------------------------------------------------------


def test_refresh_refuses_while_a_dispatch_is_leased(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    project.task_source.spec_overrides["t1"] = {"body": "amended mid-flight"}

    with pytest.raises(ContractError, match="already rendered"):
        eng.refresh_spec("r1", "t1")

    # Refused ⇒ nothing moved and nothing was evented.
    assert eng.store.load_task("r1", "t1").body == "do it"
    assert _events(eng) == []


def test_force_overrides_the_lease_guard_and_marks_the_event(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    project.task_source.spec_overrides["t1"] = {"body": "amended mid-flight"}

    report = eng.refresh_spec("r1", "t1", force=True)

    assert report["applied"] is True and report["leased_dispatch"] is True
    assert eng.store.load_task("r1", "t1").body == "amended mid-flight"
    evt = _events(eng)[0]
    assert evt["leased_dispatch"] is True and evt["forced"] is True
    assert evt["stage"] == Stage.SCOPE.value


def test_refresh_refuses_a_terminal_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    eng.abandon("r1", "t1", reason="run killed")
    assert eng.store.load_task("r1", "t1").state is TaskState.FAILED

    with pytest.raises(ContractError, match="terminal"):
        eng.refresh_spec("r1", "t1")
    assert _events(eng) == []


def test_refresh_refuses_a_dispatch_that_lands_during_the_source_read(tmp_path, project) -> None:
    """The guards run on a pre-lock read, and the source round-trip is long enough for a
    scheduler tick to dispatch the task. The under-lock re-check must catch it — and abort
    before writing anything."""
    eng = _engine(tmp_path, project)
    _started(eng)
    original = project.task_source.resolve
    raced = False

    def racing_resolve(task_id: str):
        nonlocal raced
        spec = original(task_id)
        if not raced:  # one-shot, so the dispatch below can't re-enter
            raced = True
            eng.next_work("r1", "t1")  # a tick leases SCOPE while we were reading
        return spec.model_copy(update={"body": "amended upstream"})

    project.task_source.resolve = racing_resolve  # type: ignore[method-assign]

    # The pre-lock guard cannot have fired — no lease existed when it ran.
    with pytest.raises(ContractError, match="while its spec was being re-read"):
        eng.refresh_spec("r1", "t1")

    assert eng.store.load_task("r1", "t1").body == "do it"
    assert _events(eng) == []


def test_a_source_failure_propagates_rather_than_silently_keeping_the_snapshot(
    tmp_path, project
) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)
    project.task_source.resolve_error = RuntimeError("gh: network unreachable")

    with pytest.raises(RuntimeError, match="network unreachable"):
        eng.refresh_spec("r1", "t1")
    assert _events(eng) == []


# --- dry run ----------------------------------------------------------------


def test_check_only_reports_without_writing(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)
    project.task_source.spec_overrides["t1"] = {"title": "Amended", "body": "new body"}

    report = eng.refresh_spec("r1", "t1", check_only=True)

    assert report["changed"] is True and report["applied"] is False
    assert report["diff"]["title"]["after"] == "Amended"
    task = eng.store.load_task("r1", "t1")
    assert (task.title, task.body) == ("Fake t1", "do it")  # untouched
    assert task.spec_refreshed_at is None
    assert _events(eng) == []


def test_check_only_is_allowed_while_leased(tmp_path, project) -> None:
    """A dry run mutates nothing, so the lease guard has nothing to protect."""
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    project.task_source.spec_overrides["t1"] = {"body": "amended"}

    report = eng.refresh_spec("r1", "t1", check_only=True)
    assert report["changed"] is True and report["applied"] is False
    assert report["leased_dispatch"] is True
    assert _events(eng) == []


# --- staleness signal -------------------------------------------------------


def test_status_check_spec_flags_a_diverged_task(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)

    fresh = eng.status("r1", check_spec=True)["tasks"]["t1"]["spec"]
    assert fresh["spec_stale"] is False and "spec_diff" not in fresh

    project.task_source.spec_overrides["t1"] = {
        "body": "amended upstream", "updated_at": "2026-07-31T09:00:00Z"
    }
    stale = eng.status("r1", check_spec=True)["tasks"]["t1"]["spec"]
    assert stale["spec_stale"] is True
    assert stale["spec_source_updated_at"] == "2026-07-31T09:00:00Z"
    assert stale["spec_diff"]["body_changed"] is True

    # ...and a refresh clears the flag.
    eng.refresh_spec("r1", "t1")
    assert eng.status("r1", check_spec=True)["tasks"]["t1"]["spec"]["spec_stale"] is False


def test_status_default_does_not_touch_the_task_source(tmp_path, project) -> None:
    """The cheap poll path stays offline and its output unchanged."""
    eng = _engine(tmp_path, project)
    _started(eng)
    project.task_source.resolve_error = AssertionError("status must not resolve()")

    assert "spec" not in eng.status("r1")["tasks"]["t1"]


def test_status_check_spec_degrades_on_a_source_failure(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _started(eng)
    project.task_source.resolve_error = RuntimeError("gh: rate limited")

    spec = eng.status("r1", check_spec=True)["tasks"]["t1"]["spec"]
    assert spec["spec_stale"] is None
    assert "rate limited" in spec["spec_check_error"]


def test_status_check_spec_skips_terminal_tasks(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    _mid_dispatch(eng)
    eng.abandon("r1", "t1", reason="run killed")

    assert "spec" not in eng.status("r1", check_spec=True)["tasks"]["t1"]


def test_staleness_answers_for_a_pre_271_task_doc(tmp_path, project) -> None:
    """A doc snapshotted before this change carries no fingerprint; it still gets a real
    verdict (hashed from its stored title/body), never a permanent 'unknown'."""
    eng = _engine(tmp_path, project)
    _started(eng)
    eng.store.update_task("r1", "t1", lambda t: setattr(t, "spec_fingerprint", None))

    assert eng.spec_staleness("r1", "t1")["spec_stale"] is False
    project.task_source.spec_overrides["t1"] = {"body": "amended"}
    assert eng.spec_staleness("r1", "t1")["spec_stale"] is True


# --- pure helpers -----------------------------------------------------------


def test_fingerprint_separates_title_from_body(tmp_path) -> None:
    assert fingerprint("a", "b") != fingerprint("ab", "")
    assert fingerprint("a", "b") == fingerprint("a", "b")


def test_diff_summary_counts_and_caps(tmp_path) -> None:
    d = diff_summary("old", "one\ntwo", "new", "one\nthree\nfour")
    assert d["changed"] and d["title_changed"] and d["body_changed"]
    assert d["title"]["before"] == "old" and d["title"]["after"] == "new"
    assert d["body"]["lines_added"] == 2 and d["body"]["lines_removed"] == 1
    assert d["body"]["chars_before"] == len("one\ntwo")

    same = diff_summary("t", "b", "t", "b")
    assert same["changed"] is False and same["body"]["lines_added"] == 0

    long_title = "x" * 500
    assert len(diff_summary("", "", long_title, "")["title"]["after"]) < 500


# --- CLI wiring -------------------------------------------------------------


def test_cli_refresh_spec_and_status_check_spec(tmp_path, capsys) -> None:
    """Each CLI invocation rebuilds the project (and so a fresh FakeTaskSource), so this
    covers the wiring — parser, engine call, JSON report — not an upstream amendment."""
    import json

    from orchestrator.cli import main

    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]

    def _cli(*argv) -> dict:
        assert main([*base, *argv]) == 0
        return json.loads(capsys.readouterr().out.strip())

    _cli("init-run", "--lane", "full")
    _cli("add-task", "--task", "#42")

    dry = _cli("refresh-spec", "--task", "#42", "--check")
    assert dry["check_only"] is True and dry["applied"] is False and dry["changed"] is False

    report = _cli("refresh-spec", "--task", "#42")
    assert report["applied"] is True and report["task_id"] == "#42"

    spec = _cli("status", "--check-spec")["tasks"]["#42"]["spec"]
    assert spec["spec_stale"] is False and spec["spec_refreshed_at"]
    assert "spec" not in _cli("status")["tasks"]["#42"]
