"""CLI smoke test — drives the supervisor's Bash interface end to end."""

from __future__ import annotations

import json

from orchestrator.cli import main
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

    # supervisor loop: next -> (shim would run agent) -> record
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

    assert stages_recorded == ["intake", "scope", "implement", "test", "deliver", "review"]

    status = _run(capsys, *base, "status")
    assert status["tasks"]["#42"]["state"] == "completed"
    assert status["lane_audit"]["clean"] is True
    assert status["lane_audit"]["total_calls"] == 6
    assert status["cost"]["total_invocations"] == 6

    report = _run(capsys, *base, "cost-report")
    assert set(report["by_stage"]) == {"intake", "scope", "implement", "test", "deliver", "review"}
    assert "net_win_usd" in report["session_reuse"]
    assert (tmp_path / "cost-report.md").exists()  # written at run finalize
