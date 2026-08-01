"""Reasoning-effort per stage (#96): the Effort vocabulary, the WorkItem/content-hash seam,
engine precedence (task pin > stage-spec default), per-transport argv translation (claude
``--effort`` / codex ``model_reasoning_effort``), audit threading (events + ledger row), and
the unset-effort backward-compatibility guarantee (byte-identical hash and argv).

The capacity lever ORDERING (effort downshifts before the model downgrades, pins exempt)
is covered in test_fallback.py alongside the #12 band tests it extends.
"""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from adapters.execution.interactive import build_stage_result
from adapters.execution.transport import claude_cli_transport, codex_cli_transport
from orchestrator.cli import main
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.schemas.enums import (
    Effort,
    ExecutionMode,
    Provider,
    Stage,
    effort_below,
    resolve_effort,
)
from orchestrator.schemas.work import (
    LanePolicy,
    TokenUsage,
    WorkItem,
    compute_content_hash,
)
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

H = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _work(effort: str | None = None, **kw) -> WorkItem:
    args = dict(
        id="wi-1", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="do it",
        schema_ref="implement", model="claude-opus-5", created_at="now",
        lane_policy=H, effort=effort,
    )
    args.update(kw)
    return WorkItem.create(**args)


# --- vocabulary ----------------------------------------------------------------

def test_effort_below_walks_one_step_down() -> None:
    assert effort_below("high") == "medium"
    assert effort_below(Effort.MEDIUM) == "low"
    assert effort_below("low") is None  # the floor
    assert effort_below(None) is None  # unset: nothing to downshift


def test_resolve_effort_normalizes_and_rejects() -> None:
    assert resolve_effort(" HIGH ") is Effort.HIGH
    with pytest.raises(ValueError, match="unknown effort.*low, medium, high"):
        resolve_effort("xhigh")


def test_stage_spec_effort_defaults() -> None:
    """The plan's table: hard stages high, judgment stages medium, mechanical prose low;
    the deterministic intake carries none (the ENGINE lane has no model to throttle)."""
    expect = {
        Stage.INTAKE: None,
        Stage.SCOPE: Effort.HIGH,
            Stage.IMPLEMENT: Effort.HIGH,
            Stage.SIMPLIFY: Effort.MEDIUM,
        Stage.TEST: Effort.MEDIUM,
        Stage.DELIVER: Effort.LOW,
        Stage.REVIEW: Effort.MEDIUM,
    }
    assert {s: spec.effort for s, spec in STAGE_SPECS.items()} == expect


# --- content hash: effort is dispatch identity; unset is byte-compatible --------

def test_effort_changes_the_content_hash_like_model_does() -> None:
    base = dict(stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
                model="claude-opus-5", lane_policy=H, attempt=0)
    assert compute_content_hash(**base, effort="high") != compute_content_hash(**base)
    assert compute_content_hash(**base, effort="high") != compute_content_hash(**base, effort="low")


def test_unset_effort_hash_matches_the_pre_96_formula() -> None:
    """Backward compatibility: an effort-less dispatch hashes EXACTLY as before #96, so an
    in-flight pre-#96 lease still verifies on record after an engine upgrade."""
    blob = "\x1f".join(["implement", "p", "implement", "claude-opus-5",
                        "headless:claude", "0"])
    legacy = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert compute_content_hash(
        stage=Stage.IMPLEMENT, prompt="p", schema_ref="implement",
        model="claude-opus-5", lane_policy=H, attempt=0,
    ) == legacy


def test_workitem_create_threads_effort_into_field_and_hash() -> None:
    w = _work(effort="high")
    assert w.effort == "high"
    assert w.content_hash == compute_content_hash(
        stage=w.stage, prompt=w.prompt, schema_ref=w.schema_ref, model=w.model,
        lane_policy=w.lane_policy, attempt=w.attempt, effort="high",
    )
    assert _work().effort is None  # default: provider default / pre-#96 shape


# --- engine precedence: pin > spec default; deterministic stages carry none ------

def test_spec_default_effort_rides_each_model_stage(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    seen: dict[Stage, str | None] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.effort
        eng.record("r1", make_result(w))
    assert seen[Stage.INTAKE] is None  # deterministic ENGINE lane: no effort
    assert seen[Stage.SCOPE] == "high" and seen[Stage.IMPLEMENT] == "high"
    assert seen[Stage.TEST] == "medium" and seen[Stage.REVIEW] == "medium"
    assert seen[Stage.DELIVER] == "low"


def test_effort_pin_wins_over_spec_default_across_stages(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", effort="low")
    assert task.effort_pin == "low"
    assert eng.store.load_task("r1", "t1").effort_pin == "low"  # round-trips
    seen: dict[Stage, str | None] = {}
    while (w := eng.next_work("r1", "t1")) is not None:
        seen[w.stage] = w.effort
        eng.record("r1", make_result(w))
    assert seen[Stage.INTAKE] is None  # the pin never reaches the deterministic lane
    for stage in (Stage.SCOPE, Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER, Stage.REVIEW):
        assert seen[stage] == "low"


def test_effort_pin_is_typed_as_effort_enum(tmp_path, project) -> None:
    """#161: Task.effort_pin is tightened from ``str | None`` to ``Effort | None`` (the
    sibling of the #147 StageRecord.effort migration). A pin is the enum member at
    construction AND after a store round-trip (a stored "low" coerces to Effort.LOW),
    while still comparing/serializing as its "low" value for backward compatibility."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    task = eng.add_task("r1", "t1", effort="low")
    assert task.effort_pin is Effort.LOW  # not the bare string
    reloaded = eng.store.load_task("r1", "t1")
    assert reloaded.effort_pin is Effort.LOW  # coerces from the stored "low" on load
    assert reloaded.effort_pin == "low"  # StrEnum still compares as its value
    # An unpinned task keeps None (no effort), unchanged.
    unpinned = eng.add_task("r1", "t2", effort=None)
    assert unpinned.effort_pin is None


def test_deterministic_stage_carries_no_effort_even_pinned(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1", effort="high", deterministic_stages=[Stage.TEST])
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.TEST:
        eng.record("r1", make_result(w))
    assert w is not None and w.lane_policy.execution_mode is ExecutionMode.ENGINE
    assert w.effort is None


def test_unknown_effort_raises_at_add_time(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    with pytest.raises(ValueError, match="unknown effort"):
        eng.add_task("r1", "t1", effort="turbo")


# --- audit threading: dispatch/record events + the ledger row --------------------

def test_effort_recorded_on_events_and_ledger_row(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake (no effort)
    w = eng.next_work("r1", "t1")  # scope at spec-default high
    eng.record("r1", make_result(w))
    events = eng.store.read_events("r1")
    dispatched = [e for e in events if e["type"] == "stage_dispatched" and e["stage"] == "scope"]
    recorded = [e for e in events if e["type"] == "stage_recorded" and e["stage"] == "scope"]
    assert dispatched[0]["effort"] == "high" and recorded[0]["effort"] == "high"
    rows = eng.ledger.rows()
    by_stage = {r["stage"]: r for r in rows}
    assert by_stage["scope"]["effort"] == "high"
    assert by_stage["intake"]["effort"] is None  # deterministic: effort-less row


def test_effort_persisted_on_the_stage_record(tmp_path, project) -> None:
    """#139: the dispatched effort is durable on the per-stage record — stamped at
    begin_stage (visible while RUNNING, before the result returns) and folded from the
    result at record, mirroring ``model``. Deterministic ENGINE-lane stages carry None."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # intake (deterministic ENGINE lane): no model call, no effort on the record
    intake = eng.next_work("r1", "t1")
    eng.record("r1", make_result(intake))
    assert eng.store.load_task("r1", "t1").stages[Stage.INTAKE].effort is None
    # scope (model lane, spec-default high): stamped at dispatch, before any result
    w = eng.next_work("r1", "t1")
    running = eng.store.load_task("r1", "t1").stages[Stage.SCOPE]
    assert running.effort == "high" and running.status.value == "running"
    # ...and folded from the echoed result at record (stays high)
    eng.record("r1", make_result(w))
    assert eng.store.load_task("r1", "t1").stages[Stage.SCOPE].effort == "high"


def test_apply_result_re_syncs_effort_on_downshift() -> None:
    """#150: apply_result re-syncs rec.effort from result.effort, mirroring rec.model. The
    other record tests fold an *echoed* effort (dispatched == run); this covers the asymmetric
    case — a capacity downshift between begin_stage and the runner returning would otherwise
    leave the record at the pre-downshift value while result.effort holds the value run."""
    from orchestrator.schemas.enums import ResultStatus
    from orchestrator.schemas.status import Task
    from orchestrator.schemas.work import LaneUsed, StageResult
    from orchestrator.state_machine import apply_result, begin_stage

    task = Task(task_id="t1", run_id="r1", created_at="t0", updated_at="t0")
    begin_stage(task, Stage.IMPLEMENT, now="t1", model="claude-opus-5", effort="high")
    assert task.stages[Stage.IMPLEMENT].effort is Effort.HIGH  # dispatched value

    # The runner returns having actually run at a lower effort (downshifted after begin_stage).
    result = StageResult(
        work_item_id="w1", content_hash="h1", run_id="r1", task_id="t1",
        stage=Stage.IMPLEMENT, attempt=0, model="claude-opus-5", effort="medium",
        status=ResultStatus.SUCCESS,
        structured_output={"files_changed": ["a.py"], "summary": "done", "committed": True},
        lane_used=LaneUsed(execution_mode=ExecutionMode.INTERACTIVE, provider=Provider.CLAUDE,
                           invocation="agent"),
        token_usage=TokenUsage(input=10, output=2), completed_at="t2",
    )
    apply_result(task, result, now="t2", cost_usd=0.01)
    assert task.stages[Stage.IMPLEMENT].effort is Effort.MEDIUM  # re-synced from the result


def test_stage_record_effort_is_the_effort_enum(tmp_path, project) -> None:
    """#147: StageRecord.effort is typed as the Effort enum, not a bare str. The value is a
    genuine Effort instance at the begin_stage stamp, after the result fold, AND after a
    store round-trip (pydantic coerces the persisted "high" back to Effort.HIGH). None stays
    None on a deterministic ENGINE-lane stage. StrEnum keeps the == "high" comparisons true."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    intake = eng.next_work("r1", "t1")
    eng.record("r1", make_result(intake))
    assert eng.store.load_task("r1", "t1").stages[Stage.INTAKE].effort is None
    # stamped at begin_stage (in-flight, before any result)
    w = eng.next_work("r1", "t1")
    running = eng.store.load_task("r1", "t1").stages[Stage.SCOPE].effort
    assert running is Effort.HIGH
    # folded from the echoed result, then round-tripped through the store (load coercion)
    eng.record("r1", make_result(w))
    recorded = eng.store.load_task("r1", "t1").stages[Stage.SCOPE].effort
    assert recorded is Effort.HIGH


def test_begin_stage_coerces_str_effort_to_enum() -> None:
    """#147/#172: begin_stage stamps a genuine Effort even from a bare-str effort — the
    status models' validate_assignment convention coerces at the write, so the record's
    enum typing holds without any explicit Effort(...) wrap at the use site."""
    from orchestrator.schemas.status import Task
    from orchestrator.state_machine import begin_stage

    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    begin_stage(task, Stage.SCOPE, now="x", model="claude-opus-5", effort="high")
    assert task.stages[Stage.SCOPE].effort is Effort.HIGH
    # an effort-less dispatch (ENGINE lane / spec without a default) stays None
    begin_stage(task, Stage.INTAKE, now="x", model="engine", effort=None)
    assert task.stages[Stage.INTAKE].effort is None


# --- transport translation: claude --effort / codex model_reasoning_effort -------

def _stub_run(calls: list):
    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok"}), stderr="")
    return fake_run


def test_claude_transport_adds_effort_flag(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work(effort="high"))
    argv = calls[0]
    assert argv[argv.index("--effort") + 1] == "high"


def test_claude_transport_unset_effort_is_byte_identical(monkeypatch) -> None:
    """Zero behavior change without effort: the argv is EXACTLY the pre-#96 argv."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    claude_cli_transport()(_work())
    w = _work()
    assert calls[0] == ["claude", "-p", w.prompt, "--model", w.model,
                        "--dangerously-skip-permissions", "--output-format", "json"]


def test_codex_transport_adds_reasoning_effort_config(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(effort="low", model="gpt-5.5"))
    argv = calls[0]
    i = argv.index("-c")
    assert argv[i + 1] == 'model_reasoning_effort="low"'


def test_codex_transport_unset_effort_omits_the_config(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _stub_run(calls))
    codex_cli_transport()(_work(model="gpt-5.5"))
    assert not any(str(a).startswith("model_reasoning_effort") for a in calls[0])
    assert "-c" not in calls[0]  # fresh call without effort has no -c overrides at all


def test_interactive_lane_echoes_effort() -> None:
    w = _work(effort="medium")
    res = build_stage_result(
        work_item=w, structured_output={"ok": True}, usage=TokenUsage(), completed_at="now"
    )
    assert res.effort == "medium"
    assert build_stage_result(
        work_item=_work(), structured_output={"ok": True}, usage=TokenUsage(), completed_at="now"
    ).effort is None


# --- CLI: --effort pin on add-task ----------------------------------------------

def test_cli_add_task_accepts_effort_pin(tmp_path, capsys) -> None:
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "full"])
    capsys.readouterr()
    main([*base, "add-task", "--task", "t1", "--effort", "low"])
    out = json.loads(capsys.readouterr().out)
    assert out["effort_pin"] == "low"


def test_cli_add_task_rejects_unknown_effort(tmp_path) -> None:
    base = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject"]
    main([*base, "init-run", "--lane", "full"])
    with pytest.raises(SystemExit):  # argparse choices reject it before the engine
        main([*base, "add-task", "--task", "t1", "--effort", "turbo"])
