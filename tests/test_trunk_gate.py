"""Post-merge trunk gate (#229): run the project adapter's verification commands over a
merged-trunk checkout and auto-file a single remediation task when trunk is red.

The engine stays project-agnostic (only adapter argv, no hardcoded pytest/ruff/mypy) and
never calls a model. Filing is best-effort and deduped so a re-invocation never files the
fix twice; a raising task source is swallowed, never a crash.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject


def _engine(tmp_path, project) -> Engine:
    store = StatusStore(tmp_path)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return Engine(store, ledger, project)


def _events(tmp_path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _types(tmp_path) -> list[str]:
    return [e["type"] for e in _events(tmp_path)]


class _GreenProject(FakeProject):
    """Every verification command exits 0 (FakeProject's `echo` commands already do)."""


class _RedProject(FakeProject):
    def test_unit_cmd(self, files=None):
        return ["sh", "-c", "echo boom-detail >&2; exit 1"]


def test_green_trunk_files_nothing_and_reports_green(tmp_path):
    project = _GreenProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is True
    assert result["failing"] == []
    assert result["filed"] is None
    assert project.task_source.followups == []
    types = _types(tmp_path)
    assert "trunk_gate_ran" in types
    assert "trunk_gate_red" not in types
    assert "trunk_gate_fix_filed" not in types


def test_red_trunk_files_exactly_one_fix(tmp_path):
    project = _RedProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is False
    assert "test_unit" in result["failing"]
    assert result["filed"] is not None
    # Exactly one remediation issue, labeled deferred-scope, carrying the failing command
    # name and its output tail + the run id.
    assert len(project.task_source.followups) == 1
    filed = project.task_source.followups[0]
    assert filed["labels"] == ["deferred-scope"]
    assert "r1" in filed["title"]
    assert "test_unit" in filed["body"]
    assert "boom-detail" in filed["body"]
    # The red command's tail is captured on the result too.
    red_cmd = next(c for c in result["commands"] if c["name"] == "test_unit")
    assert red_cmd["rc"] != 0
    assert "boom-detail" in red_cmd["output_tail"]
    types = _types(tmp_path)
    assert types.count("trunk_gate_ran") == 1
    assert types.count("trunk_gate_red") == 1
    assert types.count("trunk_gate_fix_filed") == 1


def test_red_trunk_report_only_files_nothing(tmp_path):
    project = _RedProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path, file_fix=False)

    assert result["green"] is False
    assert result["filed"] is None
    assert project.task_source.followups == []
    types = _types(tmp_path)
    assert "trunk_gate_red" in types  # the red rollup still fires
    assert "trunk_gate_fix_filed" not in types


def test_second_invocation_dedups(tmp_path):
    project = _RedProject()
    eng = _engine(tmp_path, project)

    first = eng.trunk_gate("r1", cwd=tmp_path)
    second = eng.trunk_gate("r1", cwd=tmp_path)

    # Only the first invocation files; the second dedups on the prior fix event.
    assert len(project.task_source.followups) == 1
    assert second["deduped"] is True
    assert second["filed"] == first["filed"]
    types = _types(tmp_path)
    assert types.count("trunk_gate_fix_filed") == 1
    assert types.count("trunk_gate_fix_skipped_duplicate") == 1


def test_raising_task_source_is_swallowed(tmp_path):
    project = _RedProject()

    def boom(title, body, labels=None):
        raise RuntimeError("gh is down")

    project.task_source.file_followup = boom  # type: ignore[method-assign]
    eng = _engine(tmp_path, project)

    # Must not crash even though filing raises.
    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is False
    assert result["filed"] is None  # ref=None on a raising source
    types = _types(tmp_path)
    assert "followup_failed" in types
    assert "trunk_gate_fix_filed" not in types


def test_empty_gate_reports_green(tmp_path):
    """A minimal adapter whose commands are all the `['true']` no-op sentinel has nothing
    to fail — the gate must report green, not auto-file on an empty command set."""

    class _NoopProject(FakeProject):
        def test_unit_cmd(self, files=None):
            return ["true"]

        def test_e2e_cmd(self, files=None):
            return ["true"]

        def test_shell_cmd(self, files=None):
            return ["true"]

        def typecheck_cmd(self):
            return ["true"]

    project = _NoopProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is True
    assert result["commands"] == []
    assert project.task_source.followups == []


# --- CLI wiring (exit codes + report-only) ------------------------------------------


def test_cli_green_exits_zero(tmp_path, capsys):
    rc = main([
        "--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject",
        "trunk-gate", "-C", str(tmp_path),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["green"] is True


def test_cli_red_exits_nonzero(tmp_path, capsys):
    rc = main([
        "--root", str(tmp_path), "--run", "r1", "--project", "tests.redtrunkproject",
        "trunk-gate", "-C", str(tmp_path),
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["green"] is False
    assert out["filed"] is not None


def test_cli_no_file_fix_reports_red_but_files_nothing(tmp_path, capsys):
    rc = main([
        "--root", str(tmp_path), "--run", "r1", "--project", "tests.redtrunkproject",
        "trunk-gate", "-C", str(tmp_path), "--no-file-fix",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["green"] is False
    assert out["filed"] is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
