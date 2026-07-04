"""Spec-conformance gate (#18 bullet 2): archived provenance, criteria extraction, and the
deterministic whole-spec checklist.

Covers ``orchestrator/spec_conformance.py``, the ``spec file --archive-dir`` provenance
write, and the ``spec conformance`` CLI. Mirrors ``test_spec_intake.py`` patterns: a fake
recording task source, no network.
"""

from __future__ import annotations

import json

from orchestrator.cli import main
from orchestrator.spec_conformance import (
    conformance_report,
    extract_criteria,
    render_conformance,
)
from orchestrator.spec_intake import archive_spec, file_spec


def _body(criteria_heading: bool = True) -> str:
    if criteria_heading:
        return (
            "## Scope\n"
            "Define the theme tokens.\n\n"
            "## Acceptance criteria\n"
            "- tokens exist for fg/bg\n"
            "- documented in the theme guide\n\n"
            "## Out of scope\n"
            "Per-component theming."
        )
    return "Scope: just do the thing. Acceptance: it works."


def _spec(**over) -> dict:
    base = {
        "title": "Add Dark Mode",
        "summary": "Toggle a dark theme. Non-goal: per-component theming.",
        "tasks": [
            {"id": "t1", "title": "Theme tokens", "body": _body(), "labels": ["frontend"]},
            {"id": "t2", "title": "Toggle UI", "body": _body(criteria_heading=False),
             "depends_on": ["t1"]},
        ],
    }
    base.update(over)
    return base


class _RecordingSource:
    """Records create_task calls and answers describe_issue from a per-ref state map."""

    def __init__(self, states: dict | None = None) -> None:
        self.created: list[dict] = []
        self._n = 100
        self.states = states or {}
        self.described: list[str] = []

    def create_task(self, title: str, body: str, labels=None) -> str:
        self._n += 1
        ref = f"#{self._n}"
        self.created.append({"ref": ref, "title": title, "body": body, "labels": labels})
        return ref

    def describe_issue(self, ref: str) -> dict:
        self.described.append(ref)
        s = self.states.get(ref, {})
        return {"ref": ref, "state": s.get("state", "open"),
                "body": s.get("body", ""), "pr": s.get("pr")}


# --- criteria extraction -----------------------------------------------------------

def test_extract_criteria_from_acceptance_section() -> None:
    crit = extract_criteria(_body())
    assert crit == ["tokens exist for fg/bg", "documented in the theme guide"]


def test_extract_criteria_stops_at_next_heading() -> None:
    body = "## Acceptance criteria\n- one\n- two\n\n## Notes\n- ignore me"
    assert extract_criteria(body) == ["one", "two"]


def test_extract_criteria_heading_is_case_insensitive() -> None:
    body = "### ACCEPTANCE CRITERIA\n- x\nplain prose line"
    assert extract_criteria(body) == ["x", "plain prose line"]


def test_extract_criteria_falls_back_to_full_body() -> None:
    body = _body(criteria_heading=False)
    assert extract_criteria(body) == [body.strip()]


def test_extract_criteria_empty_body() -> None:
    assert extract_criteria("   ") == []


# --- archive ------------------------------------------------------------------------

def test_archive_writes_filed_mapping(tmp_path) -> None:
    src = _RecordingSource()
    result = file_spec(_spec(), src, dry_run=False)
    path = archive_spec(_spec(), result, tmp_path / "specs")

    assert path.name == "add-dark-mode.json"
    archived = json.loads(path.read_text())
    assert archived["title"] == "Add Dark Mode"
    assert archived["filed"]["spec_label"] == "spec:add-dark-mode"
    assert archived["filed"]["mapping"] == {"t1": "#101", "t2": "#102"}


def test_archive_is_skipped_on_dry_run(tmp_path, capsys) -> None:
    # The CLI, not archive_spec itself, skips archiving on --dry-run.
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_spec()))
    rc = main(["--project", "adapters.project.selfhost", "spec", "file", str(p),
               "--dry-run", "--archive-dir", str(tmp_path / "specs")])
    assert rc == 0
    assert not (tmp_path / "specs").exists()
    out = json.loads(capsys.readouterr().out)
    assert "archived" not in out


# --- conformance report --------------------------------------------------------------

def _archived_spec(tmp_path, states: dict) -> str:
    src = _RecordingSource()
    result = file_spec(_spec(), src, dry_run=False)
    path = archive_spec(_spec(), result, tmp_path / "specs")
    return str(path)


def test_report_incomplete_when_an_issue_is_open(tmp_path) -> None:
    path = _archived_spec(tmp_path, {})
    source = _RecordingSource({"#101": {"state": "closed"}, "#102": {"state": "open"}})
    report = conformance_report(path, source)

    assert report["complete"] is False
    assert [t["state"] for t in report["tasks"]] == ["closed", "open"]
    assert report["tasks"][0]["issue"] == "#101"
    # Criteria carried through from the archived body.
    assert report["tasks"][0]["criteria"] == [
        "tokens exist for fg/bg", "documented in the theme guide"]
    assert report["unverified"] == []


def test_report_complete_when_all_issues_closed(tmp_path) -> None:
    path = _archived_spec(tmp_path, {})
    source = _RecordingSource({
        "#101": {"state": "closed", "pr": "https://github.com/o/r/pull/9"},
        "#102": {"state": "closed"},
    })
    report = conformance_report(path, source)

    assert report["complete"] is True
    assert report["unverified"] == []
    assert report["tasks"][0]["pr"] == "https://github.com/o/r/pull/9"


def test_report_unverified_without_a_source(tmp_path) -> None:
    path = _archived_spec(tmp_path, {})
    report = conformance_report(path, task_source=None)
    assert report["complete"] is False
    assert set(report["unverified"]) == {"t1", "t2"}
    assert all(t["state"] == "unknown" for t in report["tasks"])


def test_report_unverified_when_no_filed_provenance(tmp_path) -> None:
    # A plain (un-archived) spec has no `filed` mapping — nothing to look up.
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_spec()))
    report = conformance_report(str(p), _RecordingSource())
    assert report["complete"] is False
    assert set(report["unverified"]) == {"t1", "t2"}
    assert all(t["issue"] is None for t in report["tasks"])


# --- render -------------------------------------------------------------------------

def test_render_includes_per_criterion_lines(tmp_path) -> None:
    path = _archived_spec(tmp_path, {})
    source = _RecordingSource({"#101": {"state": "closed"}, "#102": {"state": "open"}})
    md = render_conformance(conformance_report(path, source))

    assert "# Spec conformance — Add Dark Mode" in md
    assert "Batch label: spec:add-dark-mode" in md
    assert "INCOMPLETE (1/2 issue(s) closed)" in md
    assert "  - tokens exist for fg/bg" in md
    assert "  - documented in the theme guide" in md
    assert "Issue: #101" in md


# --- CLI smoke ----------------------------------------------------------------------

class _StubConfig:
    name = "stub"

    def __init__(self, source) -> None:
        self.task_source = source


def test_cli_conformance_exit_1_when_open(tmp_path, capsys, monkeypatch) -> None:
    path = _archived_spec(tmp_path, {})
    source = _RecordingSource({"#101": {"state": "closed"}, "#102": {"state": "open"}})
    monkeypatch.setattr("orchestrator.cli.load_project", lambda _a: _StubConfig(source))

    rc = main(["--project", "x", "spec", "conformance", path, "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["complete"] is False


def test_cli_conformance_exit_0_when_all_closed(tmp_path, capsys, monkeypatch) -> None:
    path = _archived_spec(tmp_path, {})
    source = _RecordingSource({"#101": {"state": "closed"}, "#102": {"state": "closed"}})
    monkeypatch.setattr("orchestrator.cli.load_project", lambda _a: _StubConfig(source))

    rc = main(["--project", "x", "spec", "conformance", path])
    assert rc == 0
    assert "COMPLETE (2/2 issue(s) closed)" in capsys.readouterr().out


def test_cli_conformance_without_project_prints_and_exits_1(tmp_path, capsys) -> None:
    path = _archived_spec(tmp_path, {})
    rc = main(["spec", "conformance", path])
    assert rc == 1
    assert "Spec conformance" in capsys.readouterr().out
