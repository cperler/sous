"""The selfhost adapter's deterministic review gate (#65 seam).

CLAUDE.md's first norm is that pytest + ruff + mypy all stay green, but only pytest ran
during a task: ``DeterministicTestRunner`` shells the ``test_*_cmd`` family and nothing
else, so ``typecheck_cmd``/``types_cmd`` were reached only by the POST-MERGE trunk gate.
A task could therefore be approved with red ruff or red mypy and the first signal would
be CI on an already-open PR.

``SelfHostConfig.review_findings`` closes that. These tests pin the behaviour that makes
it worth having — a red leg BLOCKS approval, a leg that cannot run degrades to advisory
rather than to silence — and the end-to-end case proves the engine really does override
an approving model verdict, which is the whole point of a deterministic gate.

The suite never shells out to real ruff/mypy: ``_run_gate`` is the injection seam.
"""

from __future__ import annotations

import subprocess

import pytest

from adapters.project.selfhost.config import SelfHostConfig
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import Stage
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _config(monkeypatch, results: dict[str, subprocess.CompletedProcess | Exception]):
    """A SelfHostConfig whose gate legs return canned results keyed by tool name."""
    cfg = SelfHostConfig(tasks_path=None, repo="x/y")

    def fake(argv, cwd):  # noqa: ANN001 - mirrors subprocess.run's positional shape
        tool = next((t for t in ("ruff", "mypy") if t in argv), "?")
        outcome = results[tool]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cfg, "_run_gate", fake)
    return cfg


# --- the gate itself ------------------------------------------------------------------

def test_both_legs_green_yields_no_findings(tmp_path, monkeypatch) -> None:
    cfg = _config(monkeypatch, {"ruff": _proc(0), "mypy": _proc(0)})
    assert cfg.review_findings(worktree=str(tmp_path)) == []


def test_red_lint_blocks_and_carries_the_output(tmp_path, monkeypatch) -> None:
    cfg = _config(monkeypatch, {
        "ruff": _proc(1, "orchestrator/x.py:3:1: F401 unused import\nFound 1 error."),
        "mypy": _proc(0),
    })
    findings = cfg.review_findings(worktree=str(tmp_path))
    assert len(findings) == 1
    f = findings[0]
    assert f["blocking"] is True and f["severity"] == "critical"
    # The reviewer (and the fix cycle's learnings) must see WHAT failed, not just that it did.
    assert "F401 unused import" in f["description"]
    assert "uv run ruff check ." in f["description"]


def test_red_types_blocks_independently_of_lint(tmp_path, monkeypatch) -> None:
    cfg = _config(monkeypatch, {"ruff": _proc(0), "mypy": _proc(1, "x.py:1: error: bad\nFound 1")})
    findings = cfg.review_findings(worktree=str(tmp_path))
    assert [f["blocking"] for f in findings] == [True]
    assert "mypy" in findings[0]["description"]


def test_both_red_reports_both(tmp_path, monkeypatch) -> None:
    cfg = _config(monkeypatch, {"ruff": _proc(1, "lint bad"), "mypy": _proc(1, "type bad")})
    assert len(cfg.review_findings(worktree=str(tmp_path))) == 2


def test_output_is_capped_keeping_the_tail(tmp_path, monkeypatch) -> None:
    # mypy's verdict line lands LAST, so a head-truncation would cut the useful part.
    noise = "\n".join(f"file{i}.py:1: error: something" for i in range(400))
    cfg = _config(monkeypatch, {"ruff": _proc(0),
                                "mypy": _proc(1, noise + "\nFound 400 errors in 400 files")})
    desc = cfg.review_findings(worktree=str(tmp_path))[0]["description"]
    assert "Found 400 errors in 400 files" in desc
    assert len(desc) < 3000


def test_unrunnable_gate_is_advisory_not_silent_and_not_blocking(tmp_path, monkeypatch) -> None:
    """A missing binary or a timeout means UNVERIFIED. Silence would read as green;
    blocking would deadlock the task behind a gate a flaky env can't satisfy."""
    cfg = _config(monkeypatch, {
        "ruff": subprocess.TimeoutExpired(cmd="ruff", timeout=300),
        "mypy": _proc(0),
    })
    findings = cfg.review_findings(worktree=str(tmp_path))
    assert len(findings) == 1
    assert findings[0]["blocking"] is False
    assert "UNVERIFIED" in findings[0]["description"]


@pytest.mark.parametrize("worktree", [None, "", "/nope/does/not/exist"])
def test_no_worktree_is_silent(worktree, monkeypatch) -> None:
    cfg = _config(monkeypatch, {})  # _run_gate must never be reached
    assert cfg.review_findings(worktree=worktree) == []


# --- the payoff: the engine overrides an approving reviewer ---------------------------

def test_red_gate_overrides_an_approving_review(tmp_path, monkeypatch) -> None:
    """The reason the seam exists: a model that approves cannot ship past a red gate."""

    class _GatedProject(FakeProject):
        def review_findings(self, *, worktree=None):  # noqa: ANN001, ARG002
            return [{"description": "mypy (types) gate is RED on this change",
                     "severity": "critical", "blocking": True}]

    project = _GatedProject()
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    for _ in range(5):
        eng.record("r1", make_result(eng.next_work("r1", "t1")))
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.REVIEW

    out = eng.record("r1", make_result(w, structured_output={"approved": True, "issues": []}))

    # The model said approved; the deterministic gate says otherwise, so the task does NOT
    # complete — it re-opens the bounded fix cycle instead...
    assert out["outcome"] == "review_rejected_fix_cycle"
    task = eng.store.load_task("r1", "t1")
    # ...and the gate's text rides into the retry as a learning, so the fix is informed.
    assert "mypy" in task.learnings[-1]
    events = [e for e in eng.store.read_events("r1") if e["type"] == "policy_findings_merged"]
    assert events and events[0]["blocking"] == 1
