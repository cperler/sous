"""CLI smoke test — drives the supervisor's Bash interface end to end."""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.errors import ContractError
from orchestrator.schemas.work import WorkItem
from tests.conftest import make_result


def _run(capsys, *argv) -> dict | None:
    rc = main(list(argv))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out and out != "null" else None


def test_cli_drives_a_task_to_completion(tmp_path, capsys) -> None:
    root = str(tmp_path)
    base = ["--root", root, "--run", "run1", "--project", "tests.fakeproject"]

    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    # supervisor loop: next -> (shim would run agent) -> record. `next` drains the
    # deterministic intake stage in-process, so the supervisor only sees model stages.
    stages_recorded = []
    for _ in range(10):  # safety bound
        work = _run(capsys, *base, "next", "--task", "#42")
        if work is None:
            break
        from orchestrator.schemas.work import WorkItem

        wi = WorkItem.model_validate(work)
        result_file = tmp_path / "result.json"
        result_file.write_text(make_result(wi).model_dump_json())
        outcome = _run(capsys, *base, "record", "--result", str(result_file))
        stages_recorded.append(outcome["stage"])
        assert outcome["lane_attributed"] is True

    assert stages_recorded == ["scope", "implement", "test", "deliver", "review"]

    status = _run(capsys, *base, "status")
    assert status["tasks"]["#42"]["state"] == "completed"
    assert status["lane_audit"]["clean"] is True
    assert status["lane_audit"]["total_calls"] == 6
    assert status["cost"]["total_invocations"] == 6

    report = _run(capsys, *base, "cost-report")
    assert set(report["by_stage"]) == {"intake", "scope", "implement", "test", "deliver", "review"}
    assert "net_win_usd" in report["session_reuse"]
    assert (tmp_path / "cost-report.md").exists()  # written at run finalize


def test_cli_next_resume_reemits_leased_item_and_record_succeeds(tmp_path, capsys) -> None:
    # #50: a supervisor got a WorkItem then crashed before recording — the dispatch lease
    # is held. `next --resume` must re-emit that leased item (byte-identical in the fields
    # that define the work) so the supervisor recovers without hand-editing state, and a
    # record() of the re-emitted item must still validate against the (re-bound) lease.
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    # first `next` drains the deterministic intake and emits the first MODEL WorkItem
    # (scope); the lease is now held. Simulate a crash: do NOT record it.
    first = _run(capsys, *base, "next", "--task", "#42")
    assert first is not None and first["stage"] == "scope"

    # a plain `next` now refuses — the lease is held (proves resume is the only recovery).
    with pytest.raises(ContractError):
        main([*base, "next", "--task", "#42"])

    # `next --resume` re-emits the same crashed stage, byte-identical where it matters.
    resumed = _run(capsys, *base, "next", "--task", "#42", "--resume")
    assert resumed is not None and resumed["stage"] == "scope"
    assert resumed["content_hash"] == first["content_hash"]  # same work-defining fields

    # recording the re-emitted item validates against the re-bound lease and advances.
    wi = WorkItem.model_validate(resumed)
    result_file = tmp_path / "result.json"
    result_file.write_text(make_result(wi).model_dump_json())
    outcome = _run(capsys, *base, "record", "--result", str(result_file))
    assert outcome["stage"] == "scope" and outcome["lane_attributed"] is True


def test_cli_next_resume_with_nothing_leased_errors_clearly(tmp_path, capsys) -> None:
    # #50: --resume is ONLY for recovering a held lease. On a fresh task with nothing
    # outstanding there is nothing to resume — fail loudly rather than silently minting a
    # fresh dispatch (which could double-run a stage whose supervisor is still alive).
    base = ["--root", str(tmp_path), "--run", "run1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#42")

    with pytest.raises(ContractError, match="nothing leased"):
        main([*base, "next", "--task", "#42", "--resume"])


def test_cli_next_terminates_when_deterministic_setup_fails(tmp_path, capsys) -> None:
    # Regression: the `next` drain runs the deterministic intake in-process. If that stage
    # persistently fails, `next` must terminate (task -> FAILED, returns null), NOT loop
    # forever re-dispatching the failed stage.
    base = ["--root", str(tmp_path), "--run", "r", "--project", "tests.failsetup"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#7")

    work = _run(capsys, *base, "next", "--task", "#7")  # drains intake; must not hang

    assert work is None  # terminated instead of re-dispatching a failed task forever
    status = _run(capsys, *base, "status")
    assert status["tasks"]["#7"]["state"] == "failed"


def test_cli_watch_exits_on_terminal_run(tmp_path, capsys) -> None:
    # `watch` polls to a terminal state and prints a final summary. Driving the task to
    # completion first means the run is already terminal, so watch returns on the first
    # poll without ever sleeping (a passing test can't block on real time).
    base = ["--root", str(tmp_path), "--run", "w1", "--project", "tests.fakeproject"]
    _run(capsys, *base, "init-run", "--lane", "full")
    _run(capsys, *base, "add-task", "--task", "#9")
    for _ in range(10):
        work = _run(capsys, *base, "next", "--task", "#9")
        if work is None:
            break
        from orchestrator.schemas.work import WorkItem

        wi = WorkItem.model_validate(work)
        rf = tmp_path / "result.json"
        rf.write_text(make_result(wi).model_dump_json())
        _run(capsys, *base, "record", "--result", str(rf))

    summary = _run(capsys, *base, "watch", "--interval", "1")
    assert summary["watch"] == "done"
    assert summary["run_state"] == "completed"
    assert summary["progress"]["completed"] == 1
