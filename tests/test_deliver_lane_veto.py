"""#364: DELIVER never runs on codex — its sandbox cannot push the branch.

The failure this prevents is a SILENT STALL, which is why it is a veto and not a preference.
DELIVER pushes the task branch and then opens the PR. Inside codex's sandbox
`git-credential-osxkeychain` cannot reach the keychain, so git falls through to a credential
path that raises a GUI passkey dialog. Confirmed with a `git push --dry-run` in a real
`codex exec` sandbox: it succeeded only because a human was there to click the prompt. A
headless batch is unattended, so there the push blocks until the dispatch timeout — and the
retry blocks the same way, spending the budget on nothing.

#351 already granted the sandbox network egress; that is what lets the handshake get far
enough to prompt at all, so widening the sandbox is not the fix. The credential path itself
is interactive.

The load-bearing properties:

* a codex-provider DELIVER is rerouted to the deterministic ENGINE lane, which pushes and
  opens the PR from the engine's own unsandboxed process at $0;
* the reroute is EVENTED, warning-grade — a run that quietly stopped using the lane it was
  told to use is the drift `lane_audit` exists to catch;
* it is a VETO: it overrides even an explicit all-codex run, because the caller cannot opt
  into a capability the lane does not have;
* nothing else moves. Claude DELIVER stays on claude, and the other codex stages stay on
  codex — a `--provider codex` run must not quietly become a claude run.
"""

from __future__ import annotations

import json

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router, engine_lane_required
from orchestrator.schemas.enums import ExecutionLane, ExecutionMode, Provider, Stage
from orchestrator.schemas.work import LanePolicy
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
CLAUDE = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), project, **kw)


def _codex_engine(tmp_path, project) -> Engine:
    """An all-codex run — the `--provider codex` shape `batch-codex-3` used."""
    return _engine(
        tmp_path, project,
        router=Router(execution_mode=ExecutionMode.HEADLESS,
                      orchestrator_provider=Provider.CODEX),
    )


def _drive_to(eng, stage: Stage, *, run="r1", task="t1"):
    while (w := eng.next_work(run, task)) is not None:
        if w.stage is stage:
            return w
        eng.record(run, make_result(w))
    raise AssertionError(f"never reached {stage.value}")


def _events(tmp_path, run="r1") -> list[dict]:
    path = tmp_path / "runs" / run / "events.jsonl"
    if not path.exists():
        path = next(tmp_path.rglob("events.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- the pure veto ------------------------------------------------------------------------

def test_veto_names_codex_deliver_and_nothing_else() -> None:
    assert engine_lane_required(Stage.DELIVER, CODEX) == "codex_sandbox_cannot_push"
    # claude has no sandbox — its DELIVER pushes fine (this session's own push needed no dialog)
    assert engine_lane_required(Stage.DELIVER, CLAUDE) is None
    # the stages codex is actually good at are untouched
    for stage in (Stage.IMPLEMENT, Stage.TEST, Stage.REVIEW, Stage.SCOPE):
        assert engine_lane_required(stage, CODEX) is None


# --- wired into dispatch ------------------------------------------------------------------

def test_codex_deliver_is_rerouted_to_the_engine_lane(tmp_path, project) -> None:
    eng = _codex_engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    deliver = _drive_to(eng, Stage.DELIVER)
    assert deliver.lane_policy.execution_mode is ExecutionMode.ENGINE
    assert deliver.lane_policy.provider is Provider.NONE


def test_the_reroute_is_evented_not_silent(tmp_path, project) -> None:
    eng = _codex_engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    _drive_to(eng, Stage.DELIVER)

    rerouted = [e for e in _events(tmp_path) if e["type"] == "stage_rerouted_to_engine_lane"]
    assert len(rerouted) == 1
    assert rerouted[0]["stage"] == "deliver"
    assert rerouted[0]["reason"] == "codex_sandbox_cannot_push"
    assert rerouted[0]["from"] == "codex"
    assert rerouted[0]["level"] == "warning"  # a lane change is not routine information


def test_the_rest_of_an_all_codex_run_stays_on_codex(tmp_path, project) -> None:
    """The veto is surgical. If it quietly moved the whole run to claude it would be a worse
    bug than the one it fixes — and `--provider codex` would be a lie."""
    eng = _codex_engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    seen: dict[Stage, Provider] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen.setdefault(w.stage, w.lane_policy.provider)
        eng.record("r1", make_result(w))

    assert seen[Stage.IMPLEMENT] is Provider.CODEX
    assert seen[Stage.REVIEW] is Provider.CODEX
    assert seen[Stage.DELIVER] is Provider.NONE  # the only one moved


def test_claude_deliver_is_untouched(tmp_path, project) -> None:
    """No event, no reroute — the pre-#364 path stays byte-identical on the claude lane."""
    eng = _engine(
        tmp_path, project,
        router=Router(execution_mode=ExecutionMode.HEADLESS,
                      orchestrator_provider=Provider.CLAUDE),
    )
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    deliver = _drive_to(eng, Stage.DELIVER)
    assert deliver.lane_policy.provider is Provider.CLAUDE
    assert deliver.lane_policy.execution_mode is ExecutionMode.HEADLESS
    assert not [e for e in _events(tmp_path) if e["type"] == "stage_rerouted_to_engine_lane"]
