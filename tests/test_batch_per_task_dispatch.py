"""Regression tests for #97 (per-task Workflow dispatch instead of a per-round
barrier) and #102 (the batch skill passes ``--shared-root`` on every engine call).

#97: the batch supervisor now launches one Workflow invocation PER task and advances
each task independently as its invocation returns — a fast task's next stage must be
dispatchable while a slow sibling is still leased/RUNNING. The engine already supports
that interleaving; the new ``dispatchable`` CLI output surfaces ``in_flight`` so the
supervisor can size remaining capacity as ``limit - in_flight_count`` across concurrent
background invocations (re-checked before every follow-on dispatch, not once per round).

#102: every ``uv run orchestrator`` engine call in the batch SKILL.md carries
``--shared-root`` so the batch lane nests on a fresh runs-root from day one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.project_loader import load_project
from orchestrator.schemas.enums import ExecutionLane, TaskState
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

_SKILL = (
    Path(__file__).resolve().parent.parent
    / ".claude" / "skills" / "orchestrate-batch-interactive" / "SKILL.md"
)


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)


# --- #102: every engine call in the batch skill carries --shared-root ----------


def test_batch_skill_engine_calls_pass_shared_root() -> None:
    text = _SKILL.read_text()
    calls = [
        line for line in text.splitlines()
        if "orchestrator" in line and re.search(r"\borchestrator\b", line)
        and ("uv run orchestrator" in line or "orchestrator --root" in line)
    ]
    # There is at least one real engine call to assert against (guard against a
    # future rewrite that silently drops every concrete example).
    assert calls, "batch SKILL.md has no concrete `uv run orchestrator` engine call"
    for line in calls:
        assert "--shared-root" in line, (
            f"batch SKILL.md engine call missing --shared-root (#102): {line!r}"
        )


def test_batch_skill_documents_shared_root_rationale() -> None:
    # The skill must explain WHY --shared-root is always passed (mirrors the task skill),
    # not just show it — so a reader knows it's deliberate and safe on every call.
    text = _SKILL.read_text()
    assert "--shared-root" in text
    assert "#102" in text


# --- #97: per-task interleaving + in_flight capacity accounting ----------------


def test_in_flight_accessor_tracks_leases(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")

    # Nothing dispatched yet: both ready, none in flight.
    assert eng.dispatchable("r1") == ["t1", "t2"]
    assert eng.in_flight("r1") == []

    # Lease both (two concurrent background invocations out).
    w1 = eng.next_work("r1", "t1")
    w2 = eng.next_work("r1", "t2")
    assert eng.dispatchable("r1") == []            # both leased -> none re-dispatchable
    assert eng.in_flight("r1") == ["t1", "t2"]     # ...and both counted in flight

    # Record t1 mid-flight: its lease releases and it advances, while t2 stays leased.
    eng.record("r1", make_result(w1))
    assert eng.store.load_task("r1", "t1").pending_work_item_id is None
    assert eng.store.load_task("r1", "t2").pending_work_item_id == w2.id
    # t1's NEXT stage is immediately dispatchable; t2 remains RUNNING/in-flight.
    assert eng.dispatchable("r1") == ["t1"]
    assert eng.in_flight("r1") == ["t2"]
    assert eng.store.load_task("r1", "t2").state is TaskState.RUNNING


def test_dispatchable_cli_reports_in_flight_for_capacity(tmp_path, project, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]

    def run_json(*argv):
        assert main(list(base + list(argv))) == 0
        out = capsys.readouterr().out.strip()
        return json.loads(out) if out and out != "null" else None

    run_json("init-run", "--lane", "full")
    run_json("add-task", "--task", "t1")
    run_json("add-task", "--task", "t2")

    # Lease t1 and t2 out of band so the CLI observes a live in-flight count.
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "cli-c.jsonl"),
                 load_project("tests.fakeproject"))
    eng.next_work("r1", "t1")
    eng.next_work("r1", "t2")

    d = run_json("dispatchable", "--max-concurrent", "3", "--util", "0")
    # dispatchable EXCLUDES leased tasks; in_flight surfaces them so the supervisor
    # can size remaining headroom as `limit - in_flight_count` (#97).
    assert d["dispatchable"] == []
    assert sorted(d["in_flight"]) == ["t1", "t2"]
    assert d["in_flight_count"] == 2
    # limit - in_flight_count is the real remaining capacity: 3 - 2 == 1 free slot.
    assert d["limit"] - d["in_flight_count"] == 1


def test_dispatch_now_subtracts_in_flight_from_capacity(tmp_path, project, capsys) -> None:
    # #135: dispatch_now must slice `dispatchable` by the REMAINING headroom after
    # in-flight leases (`limit - in_flight_count`), not by the raw `limit`. With N
    # tasks leased, dispatch_now == ready[:limit - N] — never the pre-#97 ready[:limit].
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]

    def run_json(*argv):
        assert main(list(base + list(argv))) == 0
        out = capsys.readouterr().out.strip()
        return json.loads(out) if out and out != "null" else None

    run_json("init-run", "--lane", "full")
    for t in ("t1", "t2", "t3", "t4"):
        run_json("add-task", "--task", t)

    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "cli-c.jsonl"),
                 load_project("tests.fakeproject"))
    # Lease t1 out of band: 1 in flight, {t2,t3,t4} remain DAG-ready and unleased.
    eng.next_work("r1", "t1")

    limit = 3
    d = run_json("dispatchable", "--max-concurrent", str(limit), "--util", "0")
    ready = d["dispatchable"]
    assert ready == ["t2", "t3", "t4"]
    assert d["in_flight_count"] == 1
    # headroom = limit - in_flight_count = 3 - 1 = 2, so only the first 2 ready run now.
    assert d["dispatch_now"] == ready[:limit - d["in_flight_count"]]
    assert d["dispatch_now"] == ["t2", "t3"]

    # Now saturate: lease t2 and t3 too (3 in flight >= limit) -> zero headroom.
    eng.next_work("r1", "t2")
    eng.next_work("r1", "t3")
    d2 = run_json("dispatchable", "--max-concurrent", str(limit), "--util", "0")
    assert d2["in_flight_count"] == 3
    assert d2["dispatchable"] == ["t4"]           # t4 still DAG-ready and unleased
    # limit - in_flight_count == 0 -> dispatch_now clamps to empty (never negative).
    assert d2["dispatch_now"] == []
