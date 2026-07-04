"""Spec front door (#18): schema + DAG validation, topo order, and issue filing.

Covers the deterministic upstream that turns an idea's spec file into dependency-ordered
issues feeding the batch lane — ``orchestrator/spec_intake.py`` plus the ``spec`` CLI.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.spec_intake import (
    SpecError,
    file_spec,
    load_spec,
    plan,
    spec_label,
    topological_order,
    validate_spec,
)


def _spec(**over) -> dict:
    base = {
        "title": "Add Dark Mode",
        "summary": "Toggle a dark theme. Non-goal: per-component theming.",
        "tasks": [
            {"id": "t1", "title": "Theme tokens", "body": "define vars", "labels": ["frontend"]},
            {"id": "t2", "title": "Toggle UI", "body": "add toggle",
             "depends_on": ["t1"], "labels": ["frontend"], "pipeline": "lite"},
            {"id": "t3", "title": "Docs", "body": "document", "depends_on": ["t1", "t2"]},
        ],
    }
    base.update(over)
    return base


class _RecordingSource:
    """A fake task source that records create_task calls and hands back ``#N`` refs."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self._n = 100

    def create_task(self, title: str, body: str, labels=None) -> str:
        self._n += 1
        ref = f"#{self._n}"
        self.created.append({"ref": ref, "title": title, "body": body, "labels": labels})
        return ref


# --- schema validation -------------------------------------------------------------

def test_validate_accepts_a_well_formed_spec() -> None:
    validate_spec(_spec())  # no raise


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.pop("summary"), "summary"),
    (lambda s: s.pop("title"), "title"),
    (lambda s: s.update(tasks=[]), "non-empty"),
    (lambda s: s["tasks"][0].pop("body"), "body"),
    (lambda s: s["tasks"][0].update(title=123), "123"),
    (lambda s: s["tasks"][0].update(extra="nope"), "Additional properties"),
])
def test_schema_failures_are_clear(mutate, needle) -> None:
    spec = _spec()
    mutate(spec)
    with pytest.raises(SpecError) as exc:
        validate_spec(spec)
    assert "schema validation" in str(exc.value)
    assert needle in str(exc.value)


# --- DAG validation ----------------------------------------------------------------

def test_unknown_dependency_ref_is_reported() -> None:
    spec = _spec()
    spec["tasks"][1]["depends_on"] = ["t9"]
    with pytest.raises(SpecError, match="unknown task 't9'"):
        validate_spec(spec)


def test_duplicate_id_is_reported() -> None:
    spec = _spec()
    spec["tasks"][2]["id"] = "t1"
    with pytest.raises(SpecError, match="duplicate task id"):
        validate_spec(spec)


def test_self_dependency_is_reported() -> None:
    spec = _spec()
    spec["tasks"][0]["depends_on"] = ["t1"]
    with pytest.raises(SpecError, match="depends on itself"):
        validate_spec(spec)


def test_cycle_is_reported() -> None:
    spec = _spec(tasks=[
        {"id": "a", "title": "A", "body": "b", "depends_on": ["b"]},
        {"id": "b", "title": "B", "body": "b", "depends_on": ["a"]},
    ])
    with pytest.raises(SpecError, match="cycle"):
        validate_spec(spec)


# --- topological order -------------------------------------------------------------

def test_topological_order_places_deps_first() -> None:
    order = topological_order(_spec())
    assert order.index("t1") < order.index("t2") < order.index("t3")


def test_topological_order_is_stable_by_input_order() -> None:
    # Independent tasks keep their authored order (deterministic filing).
    spec = _spec(tasks=[
        {"id": "b", "title": "B", "body": "x"},
        {"id": "a", "title": "A", "body": "x"},
        {"id": "c", "title": "C", "body": "x"},
    ])
    assert topological_order(spec) == ["b", "a", "c"]


# --- filing ------------------------------------------------------------------------

def test_file_spec_files_in_topological_order_and_translates_deps() -> None:
    src = _RecordingSource()
    result = file_spec(_spec(), src, dry_run=False)

    # Filed deps-before-dependents.
    assert [c["title"] for c in src.created] == ["Theme tokens", "Toggle UI", "Docs"]
    assert result["order"] == ["t1", "t2", "t3"]

    # Local ids translate to the real refs of already-filed tasks.
    mapping = result["mapping"]
    assert set(mapping) == {"t1", "t2", "t3"}
    docs_body = src.created[2]["body"]
    assert f"Depends-on: {mapping['t1']}, {mapping['t2']}" in docs_body
    # The independent first task gets no Depends-on line.
    assert "Depends-on:" not in src.created[0]["body"]


def test_file_spec_applies_labels_plus_batch_label() -> None:
    src = _RecordingSource()
    file_spec(_spec(), src, dry_run=False)
    label = spec_label(_spec())
    assert label == "spec:add-dark-mode"
    for created in src.created:
        assert label in created["labels"]
    assert "frontend" in src.created[0]["labels"]


def test_dry_run_files_nothing() -> None:
    src = _RecordingSource()
    result = file_spec(_spec(), src, dry_run=True)
    assert src.created == []
    assert result["dry_run"] is True
    # Placeholder refs still translate into visible Depends-on previews.
    assert result["mapping"]["t1"] == "(dry-run:t1)"
    assert result["filed"][2]["depends_on_refs"] == ["(dry-run:t1)", "(dry-run:t2)"]


def test_file_spec_requires_create_task_capability() -> None:
    class _NoCreate:
        pass

    with pytest.raises(SpecError, match="no create_task"):
        file_spec(_spec(), _NoCreate(), dry_run=False)


def test_slug_override() -> None:
    assert spec_label(_spec(slug="dm")) == "spec:dm"


# --- load + plan -------------------------------------------------------------------

def test_load_spec_reports_missing_file(tmp_path) -> None:
    with pytest.raises(SpecError, match="not found"):
        load_spec(tmp_path / "nope.json")


def test_load_spec_reports_bad_json(tmp_path) -> None:
    p = tmp_path / "spec.json"
    p.write_text("{not json")
    with pytest.raises(SpecError, match="not valid JSON"):
        load_spec(p)


def test_plan_is_human_readable_and_ordered() -> None:
    text = plan(_spec())
    assert "Batch label: spec:add-dark-mode" in text
    # Filing order: t1 before t2 before t3.
    assert text.index("[t1]") < text.index("[t2]") < text.index("[t3]")
    assert "depends-on (local): t1, t2" in text


# --- CLI smoke ---------------------------------------------------------------------

def _write_spec(tmp_path, spec: dict):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec))
    return str(p)


def test_cli_spec_validate(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path, _spec())
    rc = main(["spec", "validate", path])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["order"] == ["t1", "t2", "t3"]


def test_cli_spec_validate_rejects_bad_spec(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path, _spec(tasks=[]))
    rc = main(["spec", "validate", path])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "schema validation" in out["error"]


def test_cli_spec_plan_prints_ordered_text(tmp_path, capsys) -> None:
    path = _write_spec(tmp_path, _spec())
    rc = main(["spec", "plan", path])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Batch label: spec:add-dark-mode" in out
    assert out.index("[t1]") < out.index("[t3]")
