"""Triage queue for REVIEW process lessons + the disposition ledger (kb process-lessons)."""

from __future__ import annotations

import json

import pytest

from orchestrator import learnings_kb as kb
from orchestrator import meta_authoring as meta
from orchestrator import process_triage as pt
from orchestrator.cli import main as cli_main

TARGET = {"kind": "stage-template", "ref": "TEST"}
REF = "https://github.com/cperler/sous/issues/541"


def _lesson(run: str, task: str, text: str, *, target=None, ts: str) -> dict:
    return {"run_id": run, "task_id": task, "kind": "process", "text": text,
            "stage": "review", "target": target, "ts": ts}


def _seed(root) -> tuple[list[dict], dict]:
    """KB with one FILED targeted cluster (2 runs), one untargeted unique lesson, one
    single-run targeted lesson, and a non-process row. Returns (entries, by-text)."""
    path = root / "learnings-kb.jsonl"
    written = kb.append_learnings(path, [
        _lesson("r1", "#1", "TEST over-runs its budget", target=TARGET, ts="2026-01-01T00:00:00"),
        _lesson("r2", "#2", "TEST is slow again", target=TARGET, ts="2026-01-02T00:00:00"),
        _lesson("r2", "#3", "Intake should flag gated experiments", ts="2026-01-03T00:00:00"),
        _lesson("r3", "#4", "Reviewer persona over-asks",
                target={"kind": "agent", "ref": "code-reviewer"}, ts="2026-01-04T00:00:00"),
        {"run_id": "r1", "task_id": "#1", "kind": "review", "text": "not a process row"},
    ])
    proposal = meta.recurring_proposals(written)[0]
    assert meta.append_filing(root / "meta-proposals.jsonl", {
        "key": proposal["key"], "ref": REF, "action": "filed", "run_id": "r2",
        **meta.evidence_watermark(proposal),
    })
    return written, {e["text"]: e for e in written}


def _queue(root, **kw) -> dict:
    path = root / "learnings-kb.jsonl"
    return pt.read_queue(path, kb.read_entries(path), **kw)


def test_queue_lists_uncovered_lessons_and_filed_issues_with_provenance(tmp_path) -> None:
    _, by_text = _seed(tmp_path)
    q = _queue(tmp_path)

    assert q["process_lessons"] == 4
    [issue] = q["filed_issues"]
    assert issue["ref"] == REF and issue["key"] == f"issue:{REF}"
    assert issue["cluster_key"] == "stage-template:test" and issue["evidence_count"] == 2
    assert [e["run_id"] for e in issue["evidence"]] == ["r1", "r2"]
    assert q["excluded"]["covered_by_filed_issue"] == 2

    # Covered lessons are NOT in the uncovered list; withheld ones are, untargeted first.
    texts = [v["text"] for v in q["lessons"]]
    assert texts == ["Intake should flag gated experiments", "Reviewer persona over-asks"]
    untargeted = q["lessons"][0]
    src = by_text["Intake should flag gated experiments"]
    assert untargeted["untargeted"] is True and untargeted["cluster_key"].startswith("text:")
    assert untargeted["key"] == f"lesson:{src['id']}"
    assert (untargeted["run_id"], untargeted["task_id"], untargeted["ts"]) == (
        "r2", "#3", "2026-01-03T00:00:00")
    assert untargeted["target"] is None and untargeted["cluster_runs"] == 1
    assert q["lessons"][1]["target"] == {"kind": "agent", "ref": "code-reviewer"}
    assert q["untargeted"] == 1


def test_dispositioned_lessons_and_issues_drop_out_and_return_with_flag(tmp_path) -> None:
    _, by_text = _seed(tmp_path)
    ledger = pt.dispositions_path(tmp_path / "learnings-kb.jsonl")
    lid = by_text["Intake should flag gated experiments"]["id"]
    pt.record_dispositions(ledger, [
        {"key": f"lesson:{lid}", "subject": "lesson", "disposition": "filed", "ref": "#600"},
        {"key": f"issue:{REF}", "subject": "issue", "disposition": "kept",
         "evidence_count": 2},
    ])

    q = _queue(tmp_path)
    assert [v["text"] for v in q["lessons"]] == ["Reviewer persona over-asks"]
    assert q["filed_issues"] == []
    assert q["excluded"]["handled_lessons"] == 1 and q["excluded"]["handled_issues"] == 1

    full = _queue(tmp_path, include_handled=True)
    handled = [v for v in full["lessons"] if "disposition" in v]
    assert handled[0]["disposition"]["ref"] == "#600"
    assert full["filed_issues"][0]["disposition"]["disposition"] == "kept"


def test_kept_issue_resurfaces_when_new_evidence_arrives(tmp_path) -> None:
    _seed(tmp_path)
    path = tmp_path / "learnings-kb.jsonl"
    pt.record_dispositions(pt.dispositions_path(path), [
        {"key": f"issue:{REF}", "subject": "issue", "disposition": "kept", "evidence_count": 2},
    ])
    kb.append_learnings(path, [
        _lesson("r9", "#9", "TEST again", target=TARGET, ts="2026-02-01T00:00:00")])
    [issue] = _queue(tmp_path)["filed_issues"]
    assert issue["evidence_count"] == 3 and issue["new_evidence_since_disposition"] == 1


def test_refiled_cluster_reports_its_current_ref(tmp_path) -> None:
    written, _ = _seed(tmp_path)
    kb.append_learnings(tmp_path / "learnings-kb.jsonl", [
        _lesson("r9", "#9", "TEST again", target=TARGET, ts="2026-02-01T00:00:00")])
    proposal = meta.recurring_proposals(kb.read_entries(tmp_path / "learnings-kb.jsonl"))[0]
    meta.append_filing(tmp_path / "meta-proposals.jsonl", {
        "key": proposal["key"], "ref": "https://x/issues/700", "action": "refiled",
        **meta.evidence_watermark(proposal),
    })
    assert [i["ref"] for i in _queue(tmp_path)["filed_issues"]] == ["https://x/issues/700"]


def test_disposition_ledger_dedupes_supersedes_and_tolerates_torn_line(tmp_path) -> None:
    ledger = tmp_path / pt.DISPOSITIONS_FILE
    row = {"key": "lesson:lk-1", "subject": "lesson", "disposition": "closed",
           "reason": "already fixed"}
    assert len(pt.record_dispositions(ledger, [row])) == 1
    assert pt.record_dispositions(ledger, [row]) == []  # identical decision: idempotent
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write('{"key": "lesson:lk-2", "dispo')  # torn write from a crash
    later = {**row, "disposition": "filed", "ref": "#9", "reason": None}
    assert len(pt.record_dispositions(ledger, [later])) == 1

    rows = pt.read_dispositions(ledger)
    assert [r["disposition"] for r in rows] == ["closed", "filed"]
    current = pt.latest_dispositions(rows)
    assert current["lesson:lk-1"]["ref"] == "#9" and "reason" not in current["lesson:lk-1"]
    assert "lesson:lk-2" not in current


def test_lesson_key_falls_back_to_provenance_fingerprint() -> None:
    row = {"run_id": "r", "task_id": "#1", "ts": "t", "text": "x"}
    assert pt.lesson_key(row) == pt.lesson_key(dict(row))
    assert pt.lesson_key(row).startswith("lesson:fp-")
    assert pt.lesson_key({**row, "text": "y"}) != pt.lesson_key(row)
    assert pt.lesson_key({**row, "id": "lk-9"}) == "lesson:lk-9"


@pytest.mark.parametrize(("handle", "ok"), [
    (REF, True), ("#541", True), ("541", True), ("#54", False), ("", False)])
def test_ref_matches(handle, ok) -> None:
    assert pt.ref_matches(handle, REF) is ok


@pytest.mark.parametrize(("disp", "kw", "err"), [
    ("filed", {}, "--ref"), ("merged", {}, "--ref"), ("closed", {}, "--reason"),
    ("applied", {}, "--commit"), ("kept", {}, None), ("filed", {"ref": "#1"}, None),
    ("bogus", {}, "unknown disposition")])
def test_validate_disposition(disp, kw, err) -> None:
    out = pt.validate_disposition(disp, ref=kw.get("ref"), reason=None, commit=None)
    assert (out is None) if err is None else (err in out)


# --- CLI ---------------------------------------------------------------------------


def _cli(capsys, *argv) -> tuple[int, dict]:
    code = cli_main(list(argv))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


def test_cli_process_lessons_then_dispose_round_trip(tmp_path, capsys) -> None:
    _, by_text = _seed(tmp_path)
    root = str(tmp_path)
    code, q = _cli(capsys, "--root", root, "kb", "process-lessons")
    assert code == 0 and len(q["lessons"]) == 2 and len(q["filed_issues"]) == 1

    lid = by_text["Intake should flag gated experiments"]["id"]
    rid = by_text["Reviewer persona over-asks"]["id"]
    code, out = _cli(capsys, "--root", root, "kb", "triage-dispose", "--lesson", lid,
                     "--lesson", f"lesson:{rid}", "--disposition", "filed", "--ref", "#600")
    assert code == 0 and out["recorded"] == 2
    assert {r["run_id"] for r in out["rows"]} == {"r2", "r3"}

    code, out = _cli(capsys, "--root", root, "kb", "triage-dispose", "--issue", "#541",
                     "--disposition", "closed", "--reason", "fixed by #580")
    assert code == 0 and out["rows"][0]["key"] == f"issue:{REF}"
    assert out["rows"][0]["evidence_count"] == 2

    # Re-running the same decision is a no-op, not a duplicate row.
    code, out = _cli(capsys, "--root", root, "kb", "triage-dispose", "--issue", "#541",
                     "--disposition", "closed", "--reason", "fixed by #580")
    assert code == 0 and out["recorded"] == 0 and out["unchanged"] == 1

    code, q = _cli(capsys, "--root", root, "kb", "process-lessons")
    assert q["lessons"] == [] and q["filed_issues"] == []
    code, q = _cli(capsys, "--root", root, "kb", "process-lessons", "--include-handled")
    assert len(q["lessons"]) == 2 and len(q["filed_issues"]) == 1


def test_cli_dispose_refuses_unknown_handles_and_writes_nothing(tmp_path, capsys) -> None:
    _, by_text = _seed(tmp_path)
    lid = by_text["Reviewer persona over-asks"]["id"]
    code = cli_main(["--root", str(tmp_path), "kb", "triage-dispose", "--lesson", lid,
                     "--lesson", "lk-nope", "--disposition", "closed", "--reason", "x"])
    assert code == 1
    assert "lk-nope" in capsys.readouterr().err
    assert not (tmp_path / pt.DISPOSITIONS_FILE).exists()


@pytest.mark.parametrize("argv", [
    ["--disposition", "filed", "--lesson", "lk-1"],          # filed without --ref
    ["--disposition", "closed", "--lesson", "lk-1"],         # closed without --reason
    ["--disposition", "applied", "--issue", "#1"],           # applied without --commit
    ["--disposition", "kept"],                               # no subject at all
    ["--disposition", "nope", "--lesson", "lk-1"],           # not a choice
])
def test_cli_dispose_argument_errors(tmp_path, argv) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["--root", str(tmp_path), "kb", "triage-dispose", *argv])
    assert exc.value.code == 2
