"""Brainstorm front door (#2): schema validation, deterministic ranking, and filing.

Covers the deterministic half of the front door ABOVE spec-intake — a fuzzy area →
ranked shortlist of ideas → filed enhancement issues. ``orchestrator/brainstorm.py`` plus
the ``brainstorm`` CLI. The divergent generation itself is the skill's job (model work);
here we exercise only the code around it. Mirrors test_spec_intake.py / test_batch_plan.py.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.brainstorm import (
    BRAINSTORM_LABEL,
    BrainstormError,
    file_selected,
    load_brainstorm,
    rank_ideas,
    render_shortlist,
    score_idea,
    validate_brainstorm,
)
from orchestrator.cli import main


def _idea(**over) -> dict:
    base = {
        "title": "Idea", "problem": "a gap", "proposal": "a fix",
        "impact": "medium", "effort": "medium", "risk": "medium", "evidence": [],
    }
    base.update(over)
    return base


def _doc(**over) -> dict:
    base = {
        "area": "reduce batch-run cost",
        "ideas": [
            _idea(title="Cheap big win", impact="high", effort="small", risk="low",
                  evidence=["orchestrator/engine.py", "#42"]),
            _idea(title="Costly big win", impact="high", effort="large", risk="high"),
            _idea(title="Small nicety", impact="low", effort="small", risk="low"),
        ],
    }
    base.update(over)
    return base


class _RecordingSource:
    """A fake task source that records create_task calls and hands back ``#N`` refs."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self._n = 200

    def create_task(self, title: str, body: str, labels=None) -> str:
        self._n += 1
        ref = f"#{self._n}"
        self.created.append({"ref": ref, "title": title, "body": body, "labels": labels})
        return ref


# --- schema validation -------------------------------------------------------------

def test_validate_accepts_a_well_formed_doc() -> None:
    validate_brainstorm(_doc())  # no raise


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("area"), "area"),
    (lambda d: d.update(ideas=[]), "non-empty"),
    (lambda d: d["ideas"][0].pop("problem"), "problem"),
    (lambda d: d["ideas"][0].pop("proposal"), "proposal"),
    (lambda d: d["ideas"][0].update(impact="huge"), "huge"),
    (lambda d: d["ideas"][0].update(effort="tiny"), "tiny"),
    (lambda d: d["ideas"][0].update(risk="scary"), "scary"),
    (lambda d: d["ideas"][0].update(extra="nope"), "Additional properties"),
])
def test_schema_failures_are_clear(mutate, needle) -> None:
    doc = _doc()
    mutate(doc)
    with pytest.raises(BrainstormError) as exc:
        validate_brainstorm(doc)
    assert "schema validation" in str(exc.value)
    assert needle in str(exc.value)


# --- deterministic ranking ---------------------------------------------------------

def test_ranking_orders_by_impact_then_effort_then_risk() -> None:
    # high/small/low > high/large/high > low/small/low.
    order = [i["title"] for i in rank_ideas(_doc())]
    assert order == ["Cheap big win", "Costly big win", "Small nicety"]


def test_ranking_effort_breaks_impact_ties_ascending() -> None:
    doc = _doc(ideas=[
        _idea(title="big-large", impact="high", effort="large", risk="low"),
        _idea(title="big-small", impact="high", effort="small", risk="low"),
    ])
    assert [i["title"] for i in rank_ideas(doc)] == ["big-small", "big-large"]


def test_ranking_risk_breaks_remaining_ties_ascending() -> None:
    doc = _doc(ideas=[
        _idea(title="risky", impact="high", effort="small", risk="high"),
        _idea(title="safe", impact="high", effort="small", risk="low"),
    ])
    assert [i["title"] for i in rank_ideas(doc)] == ["safe", "risky"]


def test_ranking_ties_are_stable_by_authored_order() -> None:
    # Three identically-scored ideas keep their authored order.
    doc = _doc(ideas=[
        _idea(title="b"), _idea(title="a"), _idea(title="c"),
    ])
    assert all(score_idea(i) == score_idea(doc["ideas"][0]) for i in doc["ideas"])
    assert [i["title"] for i in rank_ideas(doc)] == ["b", "a", "c"]


def test_score_is_monotonic_in_impact() -> None:
    hi = score_idea(_idea(impact="high", effort="large", risk="high"))
    lo = score_idea(_idea(impact="low", effort="small", risk="low"))
    # A high-impact idea outranks a low-impact one even at worst effort/risk.
    assert hi > lo


# --- render ------------------------------------------------------------------------

def test_render_shortlist_is_ordered_and_shows_area() -> None:
    text = render_shortlist(_doc())
    assert "Brainstorm area: reduce batch-run cost" in text
    assert text.index("Cheap big win") < text.index("Costly big win") < text.index("Small nicety")
    assert "1. " in text and "3. " in text


# --- filing ------------------------------------------------------------------------

def test_file_selected_files_exactly_the_chosen_in_order() -> None:
    src = _RecordingSource()
    # Ranks 1 and 3 of the shortlist = "Cheap big win" and "Small nicety".
    result = file_selected(_doc(), src, [1, 3], dry_run=False)
    assert [c["title"] for c in src.created] == ["Cheap big win", "Small nicety"]
    assert [f["rank"] for f in result["filed"]] == [1, 3]
    assert result["selected"] == [1, 3]


def test_file_selected_applies_brainstorm_label_and_provenance_body() -> None:
    src = _RecordingSource()
    file_selected(_doc(), src, [1], dry_run=False)
    created = src.created[0]
    assert created["labels"] == [BRAINSTORM_LABEL]
    # Body carries problem, proposal, evidence, and a provenance line.
    body = created["body"]
    assert "## Problem" in body and "## Proposal" in body
    assert "## Evidence" in body and "orchestrator/engine.py" in body
    assert "Provenance:" in body
    assert "reduce batch-run cost" in body


def test_file_selected_selection_order_is_preserved_not_rank_order() -> None:
    src = _RecordingSource()
    # Human picks rank 3 before rank 1 — file in the order given.
    file_selected(_doc(), src, [3, 1], dry_run=False)
    assert [c["title"] for c in src.created] == ["Small nicety", "Cheap big win"]


def test_dry_run_files_nothing() -> None:
    src = _RecordingSource()
    result = file_selected(_doc(), src, [1, 2], dry_run=True)
    assert src.created == []
    assert result["dry_run"] is True
    assert [f["ref"] for f in result["filed"]] == ["(dry-run)", "(dry-run)"]


def test_dry_run_needs_no_create_task_capability() -> None:
    class _NoCreate:
        pass

    result = file_selected(_doc(), _NoCreate(), [1], dry_run=True)
    assert result["filed"][0]["ref"] == "(dry-run)"


def test_file_selected_requires_create_task_capability() -> None:
    class _NoCreate:
        pass

    with pytest.raises(BrainstormError, match="no create_task"):
        file_selected(_doc(), _NoCreate(), [1], dry_run=False)


@pytest.mark.parametrize("selected, needle", [
    ([], "no ideas selected"),
    ([0], "out of range"),
    ([4], "out of range"),
    ([1, 1], "more than once"),
])
def test_bad_selection_is_reported(selected, needle) -> None:
    src = _RecordingSource()
    with pytest.raises(BrainstormError, match=needle):
        file_selected(_doc(), src, selected, dry_run=False)
    assert src.created == []  # nothing filed on a bad selection


# --- load --------------------------------------------------------------------------

def test_load_reports_missing_file(tmp_path) -> None:
    with pytest.raises(BrainstormError, match="not found"):
        load_brainstorm(tmp_path / "nope.json")


def test_load_reports_bad_json(tmp_path) -> None:
    p = tmp_path / "b.json"
    p.write_text("{not json")
    with pytest.raises(BrainstormError, match="not valid JSON"):
        load_brainstorm(p)


# --- CLI smoke ---------------------------------------------------------------------

def _write(tmp_path, doc: dict) -> str:
    p = tmp_path / "brainstorm.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_cli_validate(tmp_path, capsys) -> None:
    rc = main(["brainstorm", "validate", _write(tmp_path, _doc())])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["order"] == ["Cheap big win", "Costly big win", "Small nicety"]


def test_cli_validate_rejects_bad_doc(tmp_path, capsys) -> None:
    rc = main(["brainstorm", "validate", _write(tmp_path, _doc(ideas=[]))])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "schema validation" in out["error"]


def test_cli_capture_prints_ranked_shortlist(tmp_path, capsys) -> None:
    rc = main(["brainstorm", "capture", _write(tmp_path, _doc())])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Brainstorm area: reduce batch-run cost" in out
    assert out.index("Cheap big win") < out.index("Small nicety")


def test_cli_capture_dry_run_files_nothing(tmp_path, capsys) -> None:
    rc = main(["brainstorm", "capture", _write(tmp_path, _doc()),
               "--file-selected", "1,2", "--dry-run"])
    assert rc == 0
    # Last JSON object on stdout is the file_selected result.
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["dry_run"] is True
    assert [f["ref"] for f in payload["filed"]] == ["(dry-run)", "(dry-run)"]


def test_cli_capture_bad_selection_rank(tmp_path, capsys) -> None:
    rc = main(["brainstorm", "capture", _write(tmp_path, _doc()),
               "--file-selected", "9", "--dry-run"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["ok"] is False
    assert "out of range" in payload["error"]
