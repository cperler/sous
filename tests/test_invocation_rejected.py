"""A provider CLI rejecting the ARGV is terminal, not retryable (#375).

`codex exec` on codex-cli 0.147.0 refused the removed `--full-auto` flag; the transport
reported an ordinary failure, the engine re-dispatched a byte-identical command, and run
`batch-369-371` spent a whole attempt budget in under a second before the breaker failed a
task whose DELIVER had already pushed its PR. A command the CLI cannot parse is a HARNESS
bug: it is classified `INVOCATION_ERROR`, fails the task on the first attempt, and gets its
own error-grade event instead of hiding in the per-stage Markdown.
"""

from __future__ import annotations

from adapters.execution.codex import CodexRunner
from adapters.execution.transport import (
    RawResult,
    classify_raw,
    is_invocation_rejected,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.retrospective import build_retrospective
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
    TaskState,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# The verbatim stderr codex printed on run batch-369-371, task #370.
CODEX_FULL_AUTO_STDERR = (
    "error: unexpected argument '--full-auto' found\n\n"
    "  tip: to pass '--full-auto' as a value, use '-- --full-auto'\n\n"
    "Usage: codex exec [OPTIONS] [PROMPT]\n"
    "       codex exec [OPTIONS] <COMMAND> [ARGS]\n\n"
    "For more information, try '--help'."
)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


# --- the detector ------------------------------------------------------------

def test_detects_a_clap_usage_error() -> None:
    assert is_invocation_rejected(RawResult(None, exit_code=2, error=CODEX_FULL_AUTO_STDERR))
    # clap exits 2 on a usage error; the marker carries it even at another exit code.
    assert is_invocation_rejected(RawResult(None, exit_code=1, error=CODEX_FULL_AUTO_STDERR))
    # exit 2 + a bare Usage: block (no marker phrasing we listed) still classifies.
    assert is_invocation_rejected(RawResult(
        None, exit_code=2, error="Usage: codex exec [OPTIONS] [PROMPT]"))


def test_detects_a_commander_argv_error_on_the_claude_lane() -> None:
    """The claude CLI exits 1 like any run failure, so the MARKER is the signal there."""
    assert is_invocation_rejected(RawResult(
        None, exit_code=1, error="error: unknown option '--json-schemaa'"))
    assert is_invocation_rejected(RawResult(
        None, exit_code=1, error="error: unknown command 'exeq'"))
    assert is_invocation_rejected(RawResult(
        None, exit_code=1, error="error: missing required argument 'prompt'"))


def test_ordinary_failures_are_not_invocation_errors() -> None:
    assert not is_invocation_rejected(RawResult(None, exit_code=1, error="2 tests failed"))
    assert not is_invocation_rejected(RawResult(None, exit_code=1, error="TypeError: undefined"))
    assert not is_invocation_rejected(RawResult(None, exit_code=1, error="429 rate limit"))
    assert not is_invocation_rejected(RawResult(None, exit_code=1, error="please run `codex login`"))
    assert not is_invocation_rejected(RawResult(None, exit_code=0, error=None))
    # a missing binary is provider-unavailable's case (nothing parsed the argv at all)
    assert not is_invocation_rejected(RawResult(None, exit_code=127, error="No such file"))


def test_task_output_quoting_usage_text_is_not_reclassified() -> None:
    """The narrowness guard: the task's OWN output must never look like a bad argv."""
    # a failing test that prints its CLI's help text, at an ordinary failure exit code
    assert not is_invocation_rejected(RawResult(
        None, exit_code=1,
        error="AssertionError: expected 'Usage: mytool [OPTIONS]' in captured stderr"))
    # ... and the same text on raw_output (never scanned) with an empty stderr
    assert not is_invocation_rejected(RawResult(
        None, exit_code=1, error="",
        raw_output="the CLI printed: error: unexpected argument '--fast'"))


# --- both model lanes classify it --------------------------------------------

def _work(provider: Provider) -> WorkItem:
    policy = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=provider)
    return WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
                           schema_ref="implement", model="gpt-5-codex", lane_policy=policy,
                           created_at="t")


def test_codex_runner_maps_invocation_error() -> None:
    runner = CodexRunner(
        transport=lambda w: RawResult(None, exit_code=2, error=CODEX_FULL_AUTO_STDERR)
    )
    assert runner.dispatch(_work(Provider.CODEX)).status is ResultStatus.INVOCATION_ERROR


def test_claude_lane_classify_raw_maps_invocation_error() -> None:
    assert classify_raw(
        RawResult(None, exit_code=1, error="error: unknown option '--agentt'")
    ) is ResultStatus.INVOCATION_ERROR
    # regressions: the other classes keep their as-was verdicts
    assert classify_raw(RawResult(None, exit_code=124, error="timed out")) is ResultStatus.TIMEOUT
    assert classify_raw(RawResult(None, exit_code=1, error="429")) is ResultStatus.RATE_LIMITED
    assert classify_raw(RawResult(None, exit_code=1, error="boom")) is ResultStatus.FAILURE


# --- the engine fails it immediately -----------------------------------------

def _implement_work(eng) -> WorkItem:
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.IMPLEMENT:
        eng.record("r1", make_result(w))
    assert w is not None
    return w


def test_invocation_error_fails_the_task_on_the_first_attempt(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _implement_work(eng)
    assert w.attempt == 0

    out = eng.record("r1", make_result(
        w, status=ResultStatus.INVOCATION_ERROR, structured_output={},
        error=CODEX_FULL_AUTO_STDERR))

    assert out["outcome"] == "task_failed_invocation_rejected"
    assert out["task_state"] == "failed"
    task = eng.store.load_task("r1", "t1")
    assert task.state is TaskState.FAILED
    assert eng.next_work("r1", "t1") is None       # no second, identical dispatch
    # the breaker streak is never stacked — this was not a code failure
    assert task.error_signatures == []
    # the learning names it as a harness bug so the fix-forward is obvious
    assert any("rejected the invocation itself" in ln for ln in task.learnings)


def test_invocation_error_gets_its_own_error_grade_event(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _implement_work(eng)
    eng.record("r1", make_result(
        w, status=ResultStatus.INVOCATION_ERROR, structured_output={},
        error=CODEX_FULL_AUTO_STDERR))

    evs = [e for e in eng.store.read_events("r1") if e["type"] == "stage_invocation_rejected"]
    assert len(evs) == 1
    ev = evs[0]
    assert ev["level"] == "error"          # a harness bug that kills a task is not a nit
    assert ev["stage"] == "implement" and ev["attempt"] == 0
    assert "--full-auto" in ev["error"]    # the actual clap message, not just a stage .md
    assert ev["invocation"]                # the command we built is named
    assert ev["lane"].endswith(":claude")   # the lane the rejected call ran on


def test_invocation_error_reads_as_a_task_failure_in_the_retrospective(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _implement_work(eng)
    eng.record("r1", make_result(
        w, status=ResultStatus.INVOCATION_ERROR, structured_output={},
        error=CODEX_FULL_AUTO_STDERR))

    run = eng.store.load_run("r1")
    logs = {"t1": eng.store.read_stage_logs("t1")}
    retro = build_retrospective(
        run, [eng.store.load_task("r1", "t1")], eng.store.read_events("r1"), logs
    )
    failed = [t for t in retro["failed_tasks"] if t["task_id"] == "t1"]
    assert failed and failed[0]["terminal_reason"] == "task_failed_invocation_rejected"


def test_an_ordinary_failure_still_retries(tmp_path, project) -> None:
    """The regression guard: only an argv rejection is terminal-on-first-failure."""
    eng = _engine(tmp_path, project)
    w = _implement_work(eng)
    out = eng.record("r1", make_result(
        w, status=ResultStatus.FAILURE, structured_output={}, error="2 tests failed"))
    assert out["outcome"] == "stage_failed_will_retry"
    nxt = eng.next_work("r1", "t1")
    assert nxt is not None and nxt.stage is Stage.IMPLEMENT and nxt.attempt == 1
