"""#319: a FAILED attempt is billed for what it actually spent, or says it doesn't know.

Live on ``batch-headless-2`` (2026-07-30): ``#317 implement`` attempt 0 died with
``API Error: Connection closed mid-response`` after 11.8 minutes of high-effort Opus. Its
terminal stream event carried complete usage (and a session id), but the transport returned
early on the non-zero exit — so the ledger row recorded ``cost_usd: 0.0`` with
``priced: true, metered: true``: a confident assertion that a $4.25 attempt was free. The
run reported $2.43 against ~$6.68 actually spent, and ``--budget-usd`` (which sums these
rows) could be overrun without bound, because retries are exactly the spend it missed.

Three properties are locked here:

* a call that fails AFTER the provider reported usage is billed for it (and keeps its
  session, which is what warm retry #8 needs for exactly this mechanical failure);
* a call whose usage genuinely cannot be read records ``priced/metered: false`` — an honest
  unknown that renders as unmetered, never as $0;
* the budget gate counts failed attempts.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import adapters.execution.transport as T
from adapters.execution.codex import CodexRunner
from adapters.execution.headless_claude import HeadlessClaudeRunner
from adapters.execution.transport import claude_cli_transport, codex_cli_transport
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.render import (
    render_completion_note,
    render_cost_report,
    render_cost_summary,
    render_progress,
    render_task_index,
)
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    StageStatus,
)
from orchestrator.schemas.status import Task
from orchestrator.schemas.work import LanePolicy, TokenUsage, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import FakeProject, make_result

_HEADLESS = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE)
_CODEX = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)

# The REAL terminal event of batch-headless-2 / #317 / implement attempt 0, verbatim from
# ``runs/batch-headless-2/stages/317/implement-attempt0.stream.jsonl`` (trimmed to the fields
# the transport reads). Note ``is_error: true`` riding alongside a full usage report — the
# provider bills the work it did before the connection dropped.
_REAL_FAILED_RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "duration_api_ms": 548231,
    "num_turns": 44,
    "session_id": "45a0b4b8-1f23-4f40-86ab-c20c9e6860e8",
    "total_cost_usd": 4.249816,
    "usage": {
        "input_tokens": 72,
        "cache_creation_input_tokens": 139113,
        "cache_read_input_tokens": 3750117,
        "output_tokens": 36210,
    },
}
_REAL_SESSION = "45a0b4b8-1f23-4f40-86ab-c20c9e6860e8"
# What the engine's model table prices that usage at on claude-opus-5 ($5/$25 per Mtok, with
# the table's cache multipliers). Deliberately asserted as a real figure: the ledger prices
# from the table, never from the provider's self-reported total_cost_usd.
_EXPECTED_COST = 3.650125


def _wi(*, stage=Stage.IMPLEMENT, policy=_HEADLESS, model="claude-opus-5") -> WorkItem:
    return WorkItem.create(
        id="wi-1", run_id="r1", task_id="#317", stage=stage, prompt="p",
        schema_ref="implement", model=model, lane_policy=policy,
        created_at="2026-07-30T00:00:00Z", timeout_s=60,
    )


def _stream(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def _failed_stream() -> str:
    return _stream(
        {"type": "system", "subtype": "init", "session_id": _REAL_SESSION},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "working on it"}]}},
        _REAL_FAILED_RESULT_EVENT,
    )


def _patch_teed(monkeypatch, stdout: str, *, stderr: str = "", returncode: int = 0) -> None:
    def fake(argv, *, timeout, cwd, tee_path, env=None):
        tee_path.parent.mkdir(parents=True, exist_ok=True)
        tee_path.write_text(stdout, encoding="utf-8")
        return returncode, stdout, stderr
    monkeypatch.setattr(T, "_run_teed", fake)


def _patch_teed_timeout(monkeypatch, partial: str) -> None:
    """``_run_teed`` killed the call on the watchdog, carrying the partial stdout out."""
    def fake(argv, *, timeout, cwd, tee_path, env=None):
        raise subprocess.TimeoutExpired(argv, timeout or 0, output=partial, stderr="")
    monkeypatch.setattr(T, "_run_teed", fake)


def _row_for(tmp_path, raw) -> dict:
    """Drive a RawResult through the runner + ledger the way a real dispatch does."""
    work = _wi()
    result = HeadlessClaudeRunner(transport=lambda w: raw).dispatch(work)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    return result, ledger.record(result, duration_s=707.011)


# --- the provider reported usage; the failure must not discard it -------------------------

def test_failed_call_keeps_provider_reported_usage_and_session(tmp_path, monkeypatch) -> None:
    # Exit 1 with the real `is_error: true` envelope in the stream. Before #319 this returned
    # a bare RawResult: zero usage, no session. Every number below was 0/None.
    _patch_teed(monkeypatch, _failed_stream(), stderr="API Error: Connection closed mid-response",
                returncode=1)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.exit_code == 1  # still a failure — this is about accounting, not verdicts
    assert raw.usage.input == 72
    assert raw.usage.output == 36210
    assert raw.usage.cache_read == 3750117
    assert raw.usage.cache_write == 139113
    assert raw.usage_recovered is True
    assert raw.session_ref == _REAL_SESSION


def test_failed_attempt_ledger_row_is_billed_not_free(tmp_path, monkeypatch) -> None:
    _patch_teed(monkeypatch, _failed_stream(), stderr="API Error: Connection closed mid-response",
                returncode=1)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())
    result, row = _row_for(tmp_path, raw)

    assert result.status is ResultStatus.FAILURE
    assert result.session_ref == _REAL_SESSION  # survives to the StageResult (warm retry #8)
    assert row["status"] == "failure"
    assert row["input_tokens"] == 72
    assert row["output_tokens"] == 36210
    assert row["cache_read_tokens"] == 3750117
    assert row["cache_write_tokens"] == 139113
    assert row["cost_usd"] == pytest.approx(_EXPECTED_COST)
    assert row["priced"] is True and row["metered"] is True
    # The exact defect shape: a priced, metered $0.00 for a call that spent real money.
    assert not (row["priced"] and row["cost_usd"] == 0.0)
    assert row["duration_s"] == 707.011  # the wall time that always proved work happened


def test_failed_attempt_counts_toward_metered_spend(tmp_path, monkeypatch) -> None:
    _patch_teed(monkeypatch, _failed_stream(), stderr="boom", returncode=1)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())
    _row_for(tmp_path, raw)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")

    assert ledger.metered_spend() == pytest.approx(_EXPECTED_COST)  # was 0.0
    assert ledger.summary()["unmetered_calls"] == 0  # recovered usage IS metered


def test_timeout_recovers_usage_from_the_partial_stream(tmp_path, monkeypatch) -> None:
    # A timeout is the other expensive mechanical failure. If the kill landed after the CLI
    # printed its terminal event, that spend is known and must be billed.
    _patch_teed_timeout(monkeypatch, _failed_stream())
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.exit_code == 124  # still classifies as TIMEOUT
    assert raw.usage.output == 36210
    assert raw.usage_recovered is True
    assert raw.session_ref == _REAL_SESSION


def test_run_teed_attaches_partial_output_to_the_timeout(tmp_path) -> None:
    # The plumbing the test above depends on: the watchdog kill must carry out what the
    # process had already printed, or every timeout looks free no matter what it spent.
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(_REAL_FAILED_RESULT_EVENT), encoding="utf-8")
    script = (
        "import sys, time, pathlib; "
        f"sys.stdout.write(pathlib.Path({str(event_file)!r}).read_text() + '\\n'); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        T._run_teed([sys.executable, "-c", script], timeout=2.0, cwd=None,
                    tee_path=tmp_path / "s.jsonl")

    partial = T._timeout_stdout(excinfo.value)
    assert '"type": "result"' in partial or '"type":"result"' in partial
    usage, session, recovered = T._recover_usage(partial, streaming=True)
    assert recovered is True and usage.output == 36210 and session == _REAL_SESSION


def test_single_shot_failure_recovers_envelope_usage(tmp_path, monkeypatch) -> None:
    # The non-streaming `--output-format json` path (no run dir) has the same failure shape.
    envelope = json.dumps({**_REAL_FAILED_RESULT_EVENT, "result": "partial answer"})

    class _Proc:
        returncode = 1
        stdout = envelope
        stderr = "API Error: Connection closed mid-response"

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _Proc())
    raw = claude_cli_transport()(_wi())

    assert raw.exit_code == 1
    assert raw.usage.output == 36210 and raw.usage_recovered is True
    assert raw.session_ref == _REAL_SESSION


# --- usage genuinely unrecoverable: an honest unknown, never a confident $0 ----------------

def test_stream_with_no_terminal_event_is_unpriced_and_unmetered(tmp_path, monkeypatch) -> None:
    # Killed mid-stream (the SIGINT-to-the-process-group case): no usage report was ever
    # printed, so the spend is UNKNOWN. Zeros here must not masquerade as a metered $0.
    partial = _stream(
        {"type": "system", "subtype": "init", "session_id": "s-dead"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "half a"}]}},
    )
    _patch_teed(monkeypatch, partial, stderr="killed", returncode=143)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())
    assert raw.usage_recovered is False

    _, row = _row_for(tmp_path, raw)
    assert row["cost_usd"] == 0.0
    assert row["priced"] is False
    assert row["metered"] is False


def test_timeout_without_any_terminal_event_is_unmetered(tmp_path, monkeypatch) -> None:
    _patch_teed_timeout(monkeypatch, "")
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())

    assert raw.exit_code == 124
    assert raw.usage_recovered is False
    _, row = _row_for(tmp_path, raw)
    assert row["priced"] is False and row["metered"] is False


def test_unrecoverable_row_renders_as_unmetered_not_free(tmp_path, monkeypatch) -> None:
    # cost-report/cost-summary must not present the run as free when a call's cost is unknown.
    _patch_teed(monkeypatch, _stream({"type": "system", "subtype": "init"}),
                stderr="killed", returncode=143)
    raw = claude_cli_transport(run_log_root=tmp_path)(_wi())
    _row_for(tmp_path, raw)
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")

    summary = ledger.summary()
    assert summary["unmetered_calls"] == 1
    assert ledger.metered_spend() == 0.0  # unknown spend is EXCLUDED, not counted as $0
    md = render_cost_summary("r1", summary)
    assert "unmetered" in md and "not $0" in md


def test_missing_cli_binary_is_a_measured_zero(monkeypatch) -> None:
    # Boundary: nothing ever dispatched, so $0 is a MEASUREMENT, not an unknown — this row
    # must stay priced/metered or every environment miss would pollute the unmetered count.
    def boom(*a, **k):
        raise FileNotFoundError("claude: command not found")

    monkeypatch.setattr(T.subprocess, "run", boom)
    raw = claude_cli_transport()(_wi())
    assert raw.exit_code == 127 and raw.usage_recovered is True


# --- the budget gate now sees failed attempts ----------------------------------------------

class _NotifyProject(FakeProject):
    def __init__(self) -> None:
        super().__init__()
        self.notifications: list[tuple[str, dict]] = []

    def notify(self, kind: str, payload: dict) -> None:
        self.notifications.append((kind, payload))


def _engine(tmp_path, project) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project)


# scope runs on opus at $5/Mtok input, so 200k input tokens == exactly $1.00.
_ONE_USD = TokenUsage(input=200_000, output=0)


def test_budget_trips_on_spend_from_failed_attempts(tmp_path) -> None:
    # The unsoundness in one test: a run whose ONLY spend is failed attempts must still hit
    # its ceiling. Before #319 these rows were $0.00, so the gate never fired and the
    # overshoot was unbounded — retries being precisely the uncounted, expensive path.
    proj = _NotifyProject()
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=0.5)
    eng.add_task("r1", "t1")

    intake = eng.next_work("r1", "t1")  # $0 deterministic engine stage
    eng.record("r1", make_result(intake))
    scope = eng.next_work("r1", "t1")
    assert scope.stage is Stage.SCOPE
    # the attempt FAILS, having burned $1.00 of provider-reported usage
    eng.record("r1", make_result(scope, status=ResultStatus.FAILURE,
                                 error="API Error: Connection closed mid-response",
                                 tokens=_ONE_USD))

    assert eng.ledger.metered_spend() == 1.0  # the failed attempt is counted
    assert eng.next_work("r1", "t1") is None  # gate holds the retry back
    run = eng.store.load_run("r1")
    assert run.state.value == "paused"
    paused = [e for e in eng.store.read_events("r1") if e["type"] == "run_paused"]
    assert paused and "budget_exhausted" in paused[-1]["reason"]


def test_scheduler_pauses_when_every_attempt_fails_with_usage(tmp_path) -> None:
    # End-to-end through the batch loop with a fake transport whose every attempt fails
    # while reporting real usage.
    from orchestrator.scheduler import Scheduler

    proj = _NotifyProject()
    eng = _engine(tmp_path, proj)
    eng.create_run("r1", ExecutionLane.FULL, budget_usd=1.5)
    eng.add_task("r1", "t1")
    eng.add_task("r1", "t2")  # still has work when t1's failures exhaust the budget

    def failing_runner(work):
        # Deterministic ENGINE-lane stages (intake) still succeed at $0 — otherwise the task
        # dies before any MODEL stage runs and there is no spend to test. Every model call
        # fails mid-response having already burned $1.00.
        return [
            make_result(w) if w.lane_policy.execution_mode is ExecutionMode.ENGINE
            else make_result(w, status=ResultStatus.FAILURE, error="connection closed",
                             tokens=_ONE_USD)
            for w in work
        ]

    status = Scheduler(eng).run("r1", failing_runner)
    assert status["budget"]["spent_usd"] >= 1.5
    assert status["budget"]["exhausted"] is True
    assert status["run_state"] == "paused"


# --- session continuity across a mechanical failure (the #8 half) --------------------------

def test_timed_out_attempt_hands_its_session_to_a_warm_retry(tmp_path, project, monkeypatch) -> None:
    # The secondary finding, end to end: the transport now reports the failed attempt's own
    # session, so warm retry (#8) has something to resume. With session_ref dropped this
    # test could not exist — the engine's warm gate had nothing to keep.
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL, warm_retry=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    scope = eng.next_work("r1", "t1")
    assert scope.stage is Stage.SCOPE  # non-checkpoint stage: warm is safe without salvage

    _patch_teed_timeout(monkeypatch, _failed_stream())
    result = HeadlessClaudeRunner(
        transport=claude_cli_transport(run_log_root=tmp_path)
    ).dispatch(scope)
    assert result.status is ResultStatus.TIMEOUT
    assert result.session_ref == _REAL_SESSION
    eng.record("r1", result)

    task = eng.store.load_task("r1", "t1")
    assert task.session_ref == _REAL_SESSION
    assert [e for e in eng.store.read_events("r1") if e["type"] == "warm_retry_used"]


# --- codex lane: already correct on failure; keep it that way -------------------------------

def _codex_wi() -> WorkItem:
    return WorkItem.create(
        id="wi-2", run_id="r1", task_id="t1", stage=Stage.IMPLEMENT, prompt="p",
        schema_ref="implement", model="gpt-5.5", lane_policy=_CODEX,
        created_at="2026-07-30T00:00:00Z", timeout_s=60,
    )


def _codex_stream() -> str:
    return _stream(
        {"type": "thread.started", "thread_id": "th-77"},
        {"type": "turn.completed",
         "usage": {"input_tokens": 900, "cached_input_tokens": 300, "output_tokens": 400}},
    )


def test_codex_failed_call_keeps_usage_and_session(monkeypatch) -> None:
    class _Proc:
        returncode = 1
        stdout = _codex_stream()
        stderr = "stream error"

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _Proc())
    raw = codex_cli_transport()(_codex_wi())

    assert raw.exit_code == 1
    # codex's input_tokens INCLUDES cached_input_tokens, so the engine's disjoint
    # convention is 900 - 300 fresh (#350); the old pass-through billed the 300 twice.
    assert raw.usage.input == 600 and raw.usage.output == 400 and raw.usage.cache_read == 300
    assert raw.session_ref == "th-77"
    assert raw.usage_recovered is True


def test_codex_timeout_recovers_partial_usage(monkeypatch) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("codex", 60, output=_codex_stream(), stderr="")

    monkeypatch.setattr(T.subprocess, "run", boom)
    raw = codex_cli_transport()(_codex_wi())

    assert raw.exit_code == 124
    assert raw.usage.output == 400 and raw.usage_recovered is True
    assert raw.session_ref == "th-77"


def test_codex_timeout_with_no_events_is_unmetered(monkeypatch) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("codex", 60)

    monkeypatch.setattr(T.subprocess, "run", boom)
    raw = codex_cli_transport()(_codex_wi())

    assert raw.exit_code == 124 and raw.usage_recovered is False


def test_codex_failure_with_no_events_is_unmetered(tmp_path, monkeypatch) -> None:
    # The non-timeout twin of the case above: codex exits non-zero having printed nothing
    # usable (killed mid-dispatch — the shape this run's own driver hit — or truncated
    # stdout). The event stream has no terminal envelope to point at, so there is no
    # evidence a usage report ever landed and the zeros are UNKNOWN, not measured. Without
    # this the row reads `cost_usd: 0.0, priced: true, metered: true` — the exact defect
    # #319 exists to kill, one lane over.
    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "killed"

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _Proc())
    raw = codex_cli_transport()(_codex_wi())

    assert raw.exit_code == 1
    assert raw.usage_recovered is False

    result = CodexRunner(transport=lambda w: raw).dispatch(_codex_wi())
    row = CostLedger(tmp_path / "stage-costs.jsonl").record(result, duration_s=42.0)
    assert row["cost_usd"] == 0.0
    assert row["priced"] is False and row["metered"] is False


def test_codex_success_stays_measured(monkeypatch) -> None:
    # Exit 0 IS the provider's report: a successful codex call is measured, never flagged
    # unmetered — the honest-unknown rule above must not swallow the metered path.
    class _Proc:
        returncode = 0
        stdout = _codex_stream()
        stderr = ""

    monkeypatch.setattr(T.subprocess, "run", lambda *a, **k: _Proc())
    raw = codex_cli_transport()(_codex_wi())

    assert raw.exit_code == 0 and raw.usage_recovered is True


# --- the flag reaches the TASK DOC and every cost surface --------------------------------
#
# The ledger row knowing the truth is only half of it: `cost-summary.md` and `--budget-usd`
# read the ledger, but `_cost_cell`, `render_progress`'s live "Cost to date" (the most-visible
# cost surface — upserted onto the driving issue/PR mid-run), `render_completion_note` (the
# PR evidence artifact) and `cost-report.md` all read `StageRecord.cost_usd` off the task doc
# instead. Before the flag was folded onto the record they had no way to tell a measured $0
# from an unknown, so a failed metered-lane attempt rendered as a confident `$0.0000` — the
# same defect this issue exists to remove, relocated one layer out.


def _unmetered_result(work):
    """A FAILED attempt whose usage report was never recoverable (killed mid-dispatch).

    Pinned to the HEADLESS lane on purpose: the interactive lane has its own long-standing
    unmetered rule (#54), and letting these stages default to it would make every assertion
    below pass through that older branch instead of the one #319 adds — green for the wrong
    reason. A metered lane is exactly where a confident $0.00 is a lie."""
    return make_result(
        work, status=ResultStatus.FAILURE, error="killed before any usage report",
        tokens=TokenUsage(), mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE,
    ).model_copy(update={"usage_recovered": False})


def _failed_unmetered_task(tmp_path) -> tuple[Engine, Task]:
    """Drive a real FAILED, usage-unrecoverable metered-lane attempt through the engine and
    hand back the resulting task doc — no hand-built fixture, so this pins the whole thread
    (transport flag -> ledger row -> apply_result fold -> renderers), not just the renderer."""
    eng = _engine(tmp_path, FakeProject())
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake, $0 engine lane
    scope = eng.next_work("r1", "t1")
    assert scope.stage is Stage.SCOPE
    eng.record("r1", _unmetered_result(scope))
    task = eng.store.load_task("r1", "t1")
    assert task.stages[Stage.SCOPE].lane is ExecutionMode.HEADLESS  # not the #54 branch
    return eng, task


def _stage_row(md: str, stage: Stage) -> str:
    """The stage's row out of a rendered stage table — the ``_cost_cell`` surface.

    Covers both table shapes: render_progress leads with the stage name, while the task
    index and completion note lead with a sequence number."""
    return next(ln for ln in md.splitlines()
                if ln.startswith("|") and f"| {stage.value} |" in ln)


def test_unrecoverable_usage_marks_the_stage_record_unmetered(tmp_path) -> None:
    eng, task = _failed_unmetered_task(tmp_path)
    rec = task.stages[Stage.SCOPE]
    assert rec.status is StageStatus.FAILED
    assert rec.cost_usd == 0.0
    # the ledger's own honesty flag survived the fold onto the task doc
    assert rec.metered is False
    assert eng.ledger.rows()[-1]["metered"] is False


def test_a_measured_stage_stays_metered_on_the_task_doc(tmp_path) -> None:
    # Guard the other direction: the honest-unknown flag must not leak onto normal stages,
    # or every ordinary row would render as "unmetered" and the signal would be worthless.
    eng = _engine(tmp_path, FakeProject())
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    scope = eng.next_work("r1", "t1")
    eng.record("r1", make_result(scope, tokens=TokenUsage(input=200_000, output=0),
                                 mode=ExecutionMode.HEADLESS, provider=Provider.CLAUDE))

    task = eng.store.load_task("r1", "t1")
    rec = task.stages[Stage.SCOPE]
    assert rec.metered is True and rec.cost_usd == 1.0
    assert "$1.0000" in _stage_row(render_progress(task), Stage.SCOPE)
    assert "unmetered" not in render_progress(task)  # no caveat noise on a measured run


def test_stage_tables_flag_an_unmetered_stage_instead_of_showing_zero(tmp_path) -> None:
    # _cost_cell feeds all three stage tables (progress, per-task index, completion note),
    # so the PR evidence artifact inherits whatever the live progress body says.
    _, task = _failed_unmetered_task(tmp_path)
    for md in (render_progress(task), render_task_index(task), render_completion_note(task)):
        row = _stage_row(md, Stage.SCOPE)
        assert "$0.0000" not in row  # THE regression: a confident zero for an unknown cost
        assert "n/a (unmetered)" in row
        # the deterministic ENGINE-lane stage keeps its genuine, MEASURED $0 tag — the flag
        # must not turn a real zero into an unknown
        assert "$0 (engine)" in _stage_row(md, Stage.INTAKE)


def test_progress_cost_to_date_does_not_report_unknown_spend_as_free(tmp_path) -> None:
    # The live body upserted onto the driving issue/PR while the run is in flight. The only
    # metered stage here is the deterministic engine intake (a true $0), so the headline
    # figure is $0.0000 — but it must never stand BARE, which is what read as "free".
    _, task = _failed_unmetered_task(tmp_path)
    line = next(ln for ln in render_progress(task).splitlines() if "Cost to date" in ln)
    assert line.strip() != "- **Cost to date:** $0.0000"
    assert "1 unmetered stage(s) of UNKNOWN cost" in line
    assert "the true total is higher" in line


def test_progress_says_n_a_when_no_stage_is_metered(tmp_path) -> None:
    # Nothing measured at all: the headline is an explicit unknown, not a $0 figure.
    _, task = _failed_unmetered_task(tmp_path)
    task.stages[Stage.INTAKE].cost_usd = None  # drop the engine lane's measured $0
    line = next(ln for ln in render_progress(task).splitlines() if "Cost to date" in ln)
    assert "$0.0000" not in line
    assert "cost unknown (not $0)" in line


def test_progress_cost_to_date_separates_metered_spend_from_unknowns(tmp_path) -> None:
    # A mixed task: the metered figure is still shown, but it is labelled as a FLOOR rather
    # than silently absorbing the unknown stage as $0.
    eng, _ = _failed_unmetered_task(tmp_path)
    task = eng.store.load_task("r1", "t1")
    task.stages[Stage.IMPLEMENT].status = StageStatus.COMPLETED
    task.stages[Stage.IMPLEMENT].lane = ExecutionMode.HEADLESS
    task.stages[Stage.IMPLEMENT].cost_usd = 2.5

    line = next(ln for ln in render_progress(task).splitlines() if "Cost to date" in ln)
    assert "$2.5000 metered" in line
    assert "1 unmetered stage(s)" in line and "the true total is higher" in line


def test_cost_report_calls_out_unmetered_rows(tmp_path) -> None:
    # The artifact this issue's acceptance criteria names explicitly: cost-report must
    # surface an unrecoverable-usage row as unmetered rather than free. analysis() summed
    # `cost_usd` across every row with no notion of metered, so its bottom line read as
    # complete while quietly including calls of unknown cost at $0.
    eng, _ = _failed_unmetered_task(tmp_path)
    implement = eng.next_work("r1", "t1")  # SCOPE failed -> retry is dispatched
    eng.record("r1", make_result(implement, tokens=TokenUsage(input=200_000, output=0)))

    analysis = eng.ledger.analysis()
    assert analysis["unmetered_calls"] == 1
    assert analysis["total_invocations"] == 3  # engine intake + the two model calls

    md = render_cost_report("r1", analysis)
    assert "1 unmetered call(s) have UNKNOWN cost" in md
    assert "the true total is higher" in md
    assert "$1.0000" in md  # the metered spend is still reported, just qualified


def test_cost_report_says_n_a_when_nothing_is_metered(tmp_path) -> None:
    md = render_cost_report("r1", {"total_cost_usd": 0.0, "total_invocations": 2,
                                   "unmetered_calls": 2, "by_stage": {}, "by_task": {},
                                   "session_reuse": {}})
    assert "all 2 call(s) are unmetered" in md and "not $0" in md
    assert "Total cost: **$0.0000**" not in md


def test_cost_report_is_unchanged_when_every_call_is_metered(tmp_path) -> None:
    # No caveat noise on a clean run: the honesty line appears only when it is true.
    eng = _engine(tmp_path, FakeProject())
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    eng.record("r1", make_result(eng.next_work("r1", "t1"),
                                 tokens=TokenUsage(input=200_000, output=0)))

    md = render_cost_report("r1", eng.ledger.analysis())
    assert "Total cost: **$1.0000**" in md
    assert "unmetered" not in md


def test_abandoned_dispatch_records_an_honest_unknown(tmp_path) -> None:
    # An orphaned dispatch is the canonical unrecoverable case — it is literally how the
    # #317 attempt that motivated this issue ended (the driver's SIGINT killed the `claude -p`
    # child mid-stage). The engine has nothing to bill, but the provider may have burned
    # minutes of Opus first, so the row must read UNKNOWN rather than a metered $0.00.
    eng = _engine(tmp_path, FakeProject())
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))
    eng.next_work("r1", "t1")  # SCOPE dispatched; the lease is never answered
    task = eng.abandon("r1", "t1", reason="driver killed mid-dispatch", force=True)
    assert task.stages[Stage.SCOPE].status is StageStatus.FAILED
    assert task.stages[Stage.SCOPE].metered is False
    row = next(r for r in eng.ledger.rows() if r["stage"] == "scope")
    assert row["cost_usd"] == 0.0 and row["metered"] is False and row["priced"] is False
    assert eng.ledger.summary()["unmetered_calls"] == 1
    # ...and the progress body counts it as an unknown rather than a free stage.
    md = render_progress(task)
    assert "$0.0000" not in _stage_row(md, Stage.SCOPE)
    assert "1 unmetered stage(s) of UNKNOWN cost" in md
