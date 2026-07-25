"""Post-merge trunk gate (#229): run the project adapter's verification commands over a
merged-trunk checkout and auto-file a single remediation task when trunk is red.

The engine stays project-agnostic (only adapter argv, no hardcoded pytest/ruff/mypy) and
never calls a model. Filing is best-effort and deduped so a re-invocation never files the
fix twice; a raising task source is swallowed, never a crash.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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


def test_long_output_sets_truncated_flag(tmp_path):
    """_tail's truncation flag is never-silent: when a command emits more than
    _TRUNK_GATE_TAIL_LINES (40) lines, the command entry must carry truncated=True
    AND the filed body must include the "(last lines only)" marker so readers know
    they're not seeing the full output."""

    # Emit 50 lines (> 40) to stderr; the command still exits 0 so we need the
    # engine to run to completion for both truncated=True and then also force a
    # red gate to check the body marker, hence we mix with a second red command.
    class _LongOutputRedProject(FakeProject):
        def test_unit_cmd(self, files=None):
            # 50 lines to stderr, then fail so the body is actually rendered.
            return ["sh", "-c",
                    "for i in $(seq 1 50); do echo \"line $i\" >&2; done; exit 1"]

    project = _LongOutputRedProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is False
    cmd = next(c for c in result["commands"] if c["name"] == "test_unit")
    # The crucial assertion: truncation flag must be True when output > 40 lines.
    assert cmd["truncated"] is True, (
        f"expected truncated=True for 50-line output; got {cmd['truncated']!r}"
    )
    # The tail itself should contain the LAST lines, not the first ones that were dropped.
    # With 50 lines and a 40-line tail, lines 1-10 are dropped.
    assert "line 50" in cmd["output_tail"]
    assert "line 10\n" not in cmd["output_tail"]  # dropped; "line 10" not a substring of higher lines

    # The filed body should carry the "(last lines only)" marker (never-silent in body).
    assert len(project.task_source.followups) == 1
    body = project.task_source.followups[0]["body"]
    assert "(last lines only)" in body


def test_nonexistent_cwd_reports_red_and_files_nothing(tmp_path):
    """A cwd that does not exist is a misconfigured invocation: the gate must NOT fall back
    to running the verification commands against the process's own cwd (which could report
    green while having tested the wrong tree). It reports red, emits trunk_gate_error with a
    cwd_not_found reason, and files nothing — there is nothing to remediate."""
    project = _GreenProject()  # commands would all pass if they ran against the wrong tree
    eng = _engine(tmp_path, project)

    missing = Path("/nonexistent/path")
    result = eng.trunk_gate("r1", cwd=missing)

    assert result["green"] is False
    assert result["failing"] == ["cwd_check"]
    assert result["filed"] is None
    assert result["deduped"] is False
    # The offending path is surfaced on the synthetic command's tail (never-silent).
    cmd = next(c for c in result["commands"] if c["name"] == "cwd_check")
    assert cmd["rc"] == -1
    assert str(missing) in cmd["output_tail"]
    # Nothing filed; the error event fired and no verification command ever ran.
    assert project.task_source.followups == []
    types = _types(tmp_path)
    assert "trunk_gate_error" in types
    assert "trunk_gate_ran" not in types
    assert "trunk_gate_fix_filed" not in types
    error = next(e for e in _events(tmp_path) if e["type"] == "trunk_gate_error")
    assert error["reason"] == "cwd_not_found"


def test_cli_nonexistent_cwd_exits_nonzero(tmp_path, capsys):
    """The CLI must exit non-zero for a missing cwd so a caller cannot mistake a
    misconfigured invocation for a pass."""
    rc = main([
        "--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject",
        "trunk-gate", "-C", "/nonexistent/path",
    ])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["green"] is False
    assert out["filed"] is None


# --- static-typing leg (#243): the third CI command must be in the gate -------------


class _TypedGreenProject(FakeProject):
    """Declares a distinct static-typing command (the mypy analogue) that passes."""

    def types_cmd(self):
        return ["echo", "types-ok"]


class _RedTypesProject(FakeProject):
    """The static-typing command is red even though unit/typecheck are green — the exact
    false-negative #243 fixes: a trunk CI's type checker fails would have reported green."""

    def types_cmd(self):
        return ["sh", "-c", "echo mypy-boom >&2; exit 1"]


def test_types_command_runs_when_adapter_declares_one(tmp_path):
    """(a) A distinct static-typing command joins the gate's command set when declared."""
    project = _TypedGreenProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is True
    names = [c["name"] for c in result["commands"]]
    assert "types" in names, f"types leg missing from gate commands: {names}"
    types_cmd = next(c for c in result["commands"] if c["name"] == "types")
    assert types_cmd["argv"] == ["echo", "types-ok"]
    # It is not in skipped (it actually ran).
    assert "types" not in [s["name"] for s in result["skipped"]]


def test_red_types_command_makes_gate_red_and_files(tmp_path):
    """(b) A red static-typing command reddens the gate and files exactly one fix,
    mirroring the red test_unit path — this is the case that was slipping through."""
    project = _RedTypesProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is False
    assert "types" in result["failing"]
    assert result["filed"] is not None
    assert len(project.task_source.followups) == 1
    body = project.task_source.followups[0]["body"]
    assert "types" in body
    assert "mypy-boom" in body
    red_cmd = next(c for c in result["commands"] if c["name"] == "types")
    assert red_cmd["rc"] != 0
    assert "mypy-boom" in red_cmd["output_tail"]
    types = _types(tmp_path)
    assert types.count("trunk_gate_red") == 1
    assert types.count("trunk_gate_fix_filed") == 1


def test_adapter_without_types_method_runs_and_records_skip(tmp_path):
    """(c) Backward compat: a legacy/external adapter WITHOUT ``types_cmd`` must not crash
    the gate — it degrades to a skip, recorded (never-silent) as reason ``absent``."""
    project = _GreenProject()  # FakeProject has no types_cmd
    assert not hasattr(project, "types_cmd")  # the pre-#243 shape
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is True  # ran fine, no crash
    assert "types" not in [c["name"] for c in result["commands"]]
    skipped = {s["name"]: s["reason"] for s in result["skipped"]}
    assert skipped.get("types") == "absent"
    # The skip is observable on the ran event too.
    ran = next(e for e in _events(tmp_path) if e["type"] == "trunk_gate_ran")
    assert {"name": "types", "reason": "absent"} in ran["skipped"]


def test_noop_types_command_is_skipped_like_other_sentinels(tmp_path):
    """(d) A ``['true']`` static-typing command is skipped exactly like the other no-op
    sentinels — recorded under skipped with reason ``noop``, not run, not red."""

    class _NoopTypesProject(FakeProject):
        def types_cmd(self):
            return ["true"]

    project = _NoopTypesProject()
    eng = _engine(tmp_path, project)

    result = eng.trunk_gate("r1", cwd=tmp_path)

    assert result["green"] is True
    assert "types" not in [c["name"] for c in result["commands"]]
    skipped = {s["name"]: s["reason"] for s in result["skipped"]}
    assert skipped.get("types") == "noop"


def _gate_argvs(project) -> list[list[str]]:
    """The non-noop commands ``Engine.trunk_gate`` would run for ``project`` — the same
    leg selection the engine loop performs, without executing anything."""
    legs = [
        project.test_unit_cmd,
        getattr(project, "test_e2e_cmd", None),
        getattr(project, "test_shell_cmd", None),
        project.typecheck_cmd,
        getattr(project, "types_cmd", None),
    ]
    argvs: list[list[str]] = []
    for getter in legs:
        if getter is None:
            continue
        argv = getter()
        if not argv or argv == ["true"]:
            continue
        argvs.append(list(argv))
    return argvs


def test_gate_covers_every_ci_command_for_selfhost():
    """(#243 assertion) The trunk gate's command set must cover every verification command
    this project's CI runs — so a future CI addition can't silently fall out of the gate
    the way mypy did. Parses .github/workflows/ci.yml's `uv run` steps and asserts each is
    a prefix of some gate leg for the real self-host adapter."""
    from adapters.project.selfhost.config import SelfHostConfig

    repo_root = Path(__file__).resolve().parents[1]
    ci = (repo_root / ".github/workflows/ci.yml").read_text()
    # This parser only sees SINGLE-LINE `run:` steps. A YAML block step (`run: |`) puts its
    # commands on following lines, where the scan below would miss them — the guard against
    # silent drift would itself drift silently (#250). Rather than grow a YAML parser, make
    # the limitation ENFORCED: if a block step ever appears, fail here and say what to do.
    assert not re.search(r"^\s*(-\s*)?run:\s*[|>]", ci, re.M), (
        "ci.yml now uses a multi-line `run: |` step, which this guard cannot parse — its "
        "commands would silently fall out of trunk-gate coverage (the #243 gap reopening). "
        "Either keep CI steps single-line, or extend this parser to read block scalars."
    )
    # The verification commands CI executes: every `run: uv run ...` step (skip `uv sync`).
    ci_cmds = [
        line.split("run:", 1)[1].strip().split()
        for line in ci.splitlines()
        if "run:" in line and "uv run" in line
    ]
    ci_cmds = [c for c in ci_cmds if c[:2] == ["uv", "run"]]
    assert ci_cmds, "expected to parse `uv run` verification steps from ci.yml"

    gate_argvs = _gate_argvs(SelfHostConfig(tasks_path="/dev/null"))
    for ci_cmd in ci_cmds:
        assert any(argv[: len(ci_cmd)] == ci_cmd for argv in gate_argvs), (
            f"CI command {' '.join(ci_cmd)!r} is not covered by any trunk-gate leg "
            f"{gate_argvs} — it would run in CI but not in the gate (the #243 gap)."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
