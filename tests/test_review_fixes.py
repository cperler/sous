"""Regression tests for the second code-review pass (Phase 3b–5 fixes)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import adapters.execution.transport as T
from adapters.execution.codex import CodexRunner
from adapters.execution.runners import build_registry, registry_runner
from adapters.execution.transport import RawResult, _codex_usage
from orchestrator.cli import _engine
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import ContractError
from orchestrator.render import render_cost_summary
from orchestrator.scheduler import Scheduler
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)


def _wi(policy=CODEX, stage=Stage.IMPLEMENT) -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r", task_id="t", stage=stage, prompt="p", schema_ref="implement",
        model="m", lane_policy=policy, created_at="t",
    )


# #1 codex: non-dict output rejected; schema_provider wired through build_registry validates
def test_codex_rejects_non_dict_output() -> None:
    runner = CodexRunner(transport=lambda w: RawResult(structured_output=["not", "a", "dict"]))
    assert runner.dispatch(_wi()).status is ResultStatus.SCHEMA_VIOLATION


def test_build_registry_wires_codex_schema_validation() -> None:
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    reg = build_registry(
        codex_transport=lambda w: RawResult(structured_output={"ok": "nope"}),  # wrong type
        codex_schema_provider=lambda ref: schema,
        include_interactive=False,
    )
    sr = registry_runner(reg)([_wi()])[0]
    assert sr.status is ResultStatus.SCHEMA_VIOLATION  # full validation, via the wired provider


# #2 scheduler: a missing StageResult fails fast (no infinite re-dispatch)
def test_scheduler_raises_on_missing_result(tmp_path) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())
    eng.create_run("r1")
    eng.add_task("r1", "t1")

    def bad_runner(work):
        return []  # returns nothing for the dispatched item

    with pytest.raises(ContractError):
        Scheduler(eng).tick("r1", bad_runner)


# #3 CLI run-headless forces headless mode + wires the schema provider seam
def test_cli_run_headless_forces_headless(tmp_path) -> None:
    args = SimpleNamespace(root=str(tmp_path), run="r", project="tests.fakeproject",
                           mode="interactive", provider=None, cmd="run-headless")
    eng = _engine(args)
    assert eng.router.execution_mode is ExecutionMode.HEADLESS
    assert ("headless", "claude") in {(m.value, p.value) for (m, p) in eng.registry.sanctioned()}


# #4 claude transport: non-JSON stdout -> error result, not a raised exception
def test_claude_transport_non_json_is_failure(monkeypatch) -> None:
    monkeypatch.setattr(T.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="banner not json", stderr=""))
    raw = T.claude_cli_transport()(_wi())
    assert raw.structured_output is None and raw.error and "non-JSON" in raw.error


# #6 claude transport: a valid-but-empty {} structured_output is preserved
def test_claude_transport_keeps_empty_structured(monkeypatch) -> None:
    monkeypatch.setattr(T.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"structured_output": {}}', stderr=""))
    raw = T.claude_cli_transport()(_wi())
    assert raw.structured_output == {}  # not None (was dropped by `or`)


# #5 codex usage parser pulls tokens from the event stream
def test_codex_usage_parser() -> None:
    stdout = '{"type":"x"}\n{"msg":{"usage":{"input_tokens":120,"output_tokens":30}}}\n'
    u = _codex_usage(stdout)
    assert u.input == 120 and u.output == 30


# #7 render_cost_summary tolerates None totals + partial by_model bucket
def test_render_cost_summary_defensive() -> None:
    md = render_cost_summary("r", {"total_cost_usd": None, "by_model": {"m": {"invocations": 1}}})
    assert "$0.0000" in md and "| `m` |" in md  # no TypeError/KeyError


# registry_runner preserves order across the (now-parallel) batch
def test_registry_runner_preserves_order() -> None:
    reg = build_registry(headless_transport=lambda w: RawResult(structured_output={"id": w.id}),
                         include_interactive=False)
    H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
    items = [_wi(policy=H).model_copy(update={"id": f"w{i}"}) for i in range(5)]
    results = registry_runner(reg)(items)
    assert [r.work_item_id for r in results] == [f"w{i}" for i in range(5)]


# cost-summary.md is written at finalize (not per-record), and still exists after a run
def test_cost_summary_written_at_finalize(tmp_path) -> None:
    eng = Engine(StatusStore(tmp_path), CostLedger(tmp_path / "c.jsonl"), FakeProject())
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # not written before the run finalizes
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake only
    assert not (tmp_path / "cost-summary.md").exists()
    while (w := eng.next_work("r1", "t1")) is not None:
        eng.record("r1", make_result(w))
    assert (tmp_path / "cost-summary.md").exists()  # finalized
