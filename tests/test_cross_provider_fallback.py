"""Cross-provider fallthrough: codex→claude when the provider is out (#7).

When a codex-routed stage exhausts its SAME-PROVIDER options (floor rate-limit with the
wait budget spent) or codex is provider-unavailable (CLI missing / auth expired), an
opted-in run re-routes that stage's NEXT dispatch to the equivalent claude lane instead of
failing/cooling forever. One-way (codex→claude, never the reverse), once per stage, and the
run flag is the human's blanket consent (even a :codex-pinned task falls through).
"""

from __future__ import annotations

from adapters.execution.codex import CodexRunner
from adapters.execution.transport import RawResult, is_provider_unavailable
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.routing import Router
from orchestrator.schemas.enums import (
    ExecutionLane,
    ExecutionMode,
    Provider,
    ResultStatus,
    Stage,
)
from orchestrator.schemas.work import LanePolicy, WorkItem
from orchestrator.stages import STAGE_SPECS
from orchestrator.status_store import StatusStore
from tests.conftest import make_result


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _codex_result(work: WorkItem, **kw):
    """A StageResult reporting a CODEX dispatch (lane_used.provider = codex is the ground
    truth the engine keys fallthrough off)."""
    return make_result(work, provider=Provider.CODEX, mode=ExecutionMode.HEADLESS, **kw)


def _advance_codex_task_to(eng, target: Stage, *, mint_codex_session: bool = False):
    """Create a codex-pinned task and drive it to ``target``. IMPLEMENT/TEST route to codex;
    intake is the ENGINE lane and SCOPE is claude. Optionally mint a codex session on the
    successful IMPLEMENT so a later TEST fallthrough has a codex ref to (not) leak."""
    eng.create_run("r1", ExecutionLane.FULL, cross_provider_fallback=True)
    eng.add_task("r1", "t1", provider_tag="codex")
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not target:
        sess = "c-impl" if (mint_codex_session and w.stage is Stage.IMPLEMENT) else None
        prov = Provider.CODEX if w.lane_policy.provider is Provider.CODEX else None
        eng.record("r1", make_result(w, provider=prov, session_ref=sess))
    assert w is not None and w.stage is target
    return w


def _fallthrough_events(eng, run_id="r1"):
    return [e for e in eng.store.read_events(run_id) if e.get("type") == "provider_fallthrough"]


# --- transport / runner classification ---------------------------------------

def test_is_provider_unavailable_detects_cli_and_auth() -> None:
    assert is_provider_unavailable(RawResult(None, exit_code=127, error="No such file"))
    assert is_provider_unavailable(RawResult(None, exit_code=1, error="Error: not logged in"))
    assert is_provider_unavailable(RawResult(None, exit_code=1, error="401 Unauthorized"))
    # #80: a configured-model-not-available refusal is provider-unavailable (the codex 400 that
    # never matched the auth-only markers). Both the codex plan-mismatch wording and the bare
    # "model is not supported" phrasing classify.
    assert is_provider_unavailable(RawResult(
        None, exit_code=1,
        error="The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account"))
    assert is_provider_unavailable(RawResult(None, exit_code=1, error="model is not supported"))
    # #87: the "…when using…" marker is pinned to the codex/ChatGPT-plan phrasing — a generic
    # "X is not supported when using Y" that is NOT codex/chatgpt-related no longer reclassifies.
    assert not is_provider_unavailable(RawResult(
        None, exit_code=1, error="feature Foo is not supported when using bar mode"))
    # a genuine task/tool failure is NOT provider-unavailable
    assert not is_provider_unavailable(RawResult(None, exit_code=1, error="TypeError: undefined"))
    assert not is_provider_unavailable(RawResult(None, exit_code=1, error="2 tests failed"))
    # a bare invalid_request_error (no model/plan phrasing) is NOT reclassified — keeps the guard
    assert not is_provider_unavailable(RawResult(None, exit_code=1, error="invalid_request_error"))
    # a rate-limit is its own class, not provider-unavailable
    assert not is_provider_unavailable(RawResult(None, exit_code=1, error="429 rate limit"))


def test_codex_runner_maps_provider_unavailable(monkeypatch) -> None:
    C = LanePolicy(execution_mode=ExecutionMode.HEADLESS, provider=Provider.CODEX)
    wi = WorkItem.create(id="wi", run_id="r", task_id="t", stage=Stage.IMPLEMENT, prompt="p",
                         schema_ref="implement", model="gpt-5-codex", lane_policy=C, created_at="t")
    # CLI missing (exit 127) -> PROVIDER_UNAVAILABLE
    r_missing = CodexRunner(transport=lambda w: RawResult(None, exit_code=127, error="not found"))
    assert r_missing.dispatch(wi).status is ResultStatus.PROVIDER_UNAVAILABLE
    # auth expired -> PROVIDER_UNAVAILABLE
    r_auth = CodexRunner(
        transport=lambda w: RawResult(None, exit_code=1, error="Error: please run `codex login`")
    )
    assert r_auth.dispatch(wi).status is ResultStatus.PROVIDER_UNAVAILABLE
    # #80: the configured model is refused by the provider/plan -> PROVIDER_UNAVAILABLE (so the
    # engine can fall through) instead of a plain FAILURE that just burns codex retries.
    r_model = CodexRunner(transport=lambda w: RawResult(
        None, exit_code=1,
        error="The 'gpt-5-codex' model is not supported when using Codex with a ChatGPT account"))
    assert r_model.dispatch(wi).status is ResultStatus.PROVIDER_UNAVAILABLE
    # a genuine failure stays FAILURE (not reclassified)
    r_fail = CodexRunner(transport=lambda w: RawResult(None, exit_code=1, error="tests failed"))
    assert r_fail.dispatch(wi).status is ResultStatus.FAILURE
    # a rate-limit stays RATE_LIMITED
    r_rl = CodexRunner(transport=lambda w: RawResult(None, exit_code=1, error="429 rate limit"))
    assert r_rl.dispatch(wi).status is ResultStatus.RATE_LIMITED


# --- provider-unavailable short-circuits straight to fallthrough (flag on) ----

def test_provider_unavailable_falls_through_to_claude(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    w = _advance_codex_task_to(eng, Stage.IMPLEMENT)
    assert w.lane_policy.provider is Provider.CODEX and w.model == "gpt-5.6-sol"

    out = eng.record("r1", _codex_result(
        w, status=ResultStatus.PROVIDER_UNAVAILABLE, structured_output={},
        error="codex: not logged in"))
    assert out["outcome"] == "provider_fallthrough"
    assert out["task_state"] == "retrying"

    task = eng.store.load_task("r1", "t1")
    assert Stage.IMPLEMENT in task.fallthrough_stages
    assert task.not_before is None                 # no pointless cooldown wait
    assert task.rate_limit_waits == 0
    assert task.pending_fallback_model is None
    # a fallthrough learning tells the claude attempt the codex context is gone
    assert any("codex provider was UNAVAILABLE" in ln for ln in task.learnings)

    ev = _fallthrough_events(eng)
    assert len(ev) == 1
    assert ev[0]["from"] == "codex" and ev[0]["to"] == "claude"
    assert ev[0]["stage"] == "implement" and "not logged in" in ev[0]["reason"]

    # the NEXT dispatch of this stage is headless×claude at the role default, SAME attempt
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.IMPLEMENT
    assert nxt.lane_policy.provider is Provider.CLAUDE
    assert nxt.model == "claude-opus-5"           # claude DEEP_REASON default
    assert nxt.attempt == w.attempt                 # provider was out, not the task — no burn


def test_no_cooldown_before_fallthrough_on_provider_unavailable(tmp_path, project) -> None:
    """Provider-unavailable short-circuits: no rate-limit cooldown park is entered first."""
    eng = _engine(tmp_path, project)
    w = _advance_codex_task_to(eng, Stage.IMPLEMENT)
    eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                   structured_output={}, error="auth error"))
    assert not [e for e in eng.store.read_events("r1") if e.get("type") == "rate_limit_cooldown"]


# --- session / context integrity on fallthrough (#9 / #3) --------------------

def test_codex_session_is_not_leaked_to_claude_on_fallthrough(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    # IMPLEMENT (codex) succeeds and mints a codex session; TEST (codex) then goes out.
    w = _advance_codex_task_to(eng, Stage.TEST, mint_codex_session=True)
    task = eng.store.load_task("r1", "t1")
    assert task.session_ref == "c-impl" and task.session_provider is Provider.CODEX
    # the TEST dispatch chained the codex session (same provider)
    assert w.lane_policy.provider is Provider.CODEX and w.session_ref == "c-impl"

    eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                   structured_output={}, error="codex CLI not found"))
    task = eng.store.load_task("r1", "t1")
    # the codex session is CLEARED — it must not ride onto claude
    assert task.session_ref is None and task.session_provider is None

    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.TEST and nxt.lane_policy.provider is Provider.CLAUDE
    assert nxt.session_ref is None
    # #4 schema parity: nothing provider-specific leaks — the claude WorkItem validates
    # against the SAME canonical stage schema codex used, only the lane provider swapped.
    assert nxt.schema_ref == STAGE_SPECS[Stage.TEST].schema_ref
    assert nxt.model.startswith("claude-")


# --- flag off -> existing behavior unchanged ---------------------------------

def test_flag_off_provider_unavailable_is_a_normal_failure(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    eng.create_run("r1", ExecutionLane.FULL)  # cross_provider_fallback defaults off
    eng.add_task("r1", "t1", provider_tag="codex")
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.IMPLEMENT:
        prov = Provider.CODEX if w.lane_policy.provider is Provider.CODEX else None
        eng.record("r1", make_result(w, provider=prov))

    out = eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                         structured_output={}, error="not logged in"))
    assert out["outcome"] == "stage_failed_will_retry"   # degrades to a normal failure
    assert _fallthrough_events(eng) == []
    task = eng.store.load_task("r1", "t1")
    assert task.fallthrough_stages == ()
    # the retry stays on codex (flag off — no cross-provider re-route)
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.IMPLEMENT and nxt.lane_policy.provider is Provider.CODEX


# --- a genuine codex task failure does NOT fall through ----------------------

def test_genuine_codex_failure_retries_codex_not_claude(tmp_path, project) -> None:
    """A real task failure (tests failed) is the task's problem, not the provider's — it
    retries within codex normally even with the flag on."""
    eng = _engine(tmp_path, project, max_attempts=3, breaker_threshold=9)
    w = _advance_codex_task_to(eng, Stage.IMPLEMENT)
    out = eng.record("r1", _codex_result(w, status=ResultStatus.FAILURE,
                                         structured_output={"failures": ["t1"]}, error="2 failed"))
    assert out["outcome"] == "stage_failed_will_retry"
    assert _fallthrough_events(eng) == []
    assert eng.store.load_task("r1", "t1").fallthrough_stages == ()
    nxt = eng.next_work("r1", "t1")
    assert nxt.lane_policy.provider is Provider.CODEX   # provider is fine — stay on codex


# --- floor rate-limit with the wait budget exhausted -> fallthrough ----------

def test_rate_limit_floor_exhausted_falls_through(tmp_path, project) -> None:
    """Fallthrough happens at the codex FLOOR, not on the first rate-limit. The 5.6 ladder
    (sol -> terra -> luna) means a rate-limited head degrades within codex twice before the
    provider is exhausted; only a luna rate-limit with no wait budget re-routes to claude.
    Guards the seam that used to be untestable when the chain was single-entry."""
    eng = _engine(tmp_path, project, max_rate_limit_waits=0, breaker_threshold=9)
    w = _advance_codex_task_to(eng, Stage.IMPLEMENT)
    assert w.model == "gpt-5.6-sol"

    # sol and terra rate-limits degrade DOWN the codex chain — no fallthrough yet.
    for expect_next in ("gpt-5.6-terra", "gpt-5.6-luna"):
        out = eng.record("r1", _codex_result(w, status=ResultStatus.RATE_LIMITED,
                                             structured_output={}))
        assert out["outcome"] != "provider_fallthrough", f"fell through before {expect_next}"
        assert not _fallthrough_events(eng)
        w = eng.next_work("r1", "t1")
        assert w.lane_policy.provider is Provider.CODEX and w.model == expect_next

    # at the floor (luna) with waits exhausted -> fallthrough to claude
    out = eng.record("r1", _codex_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "provider_fallthrough"
    ev = _fallthrough_events(eng)
    assert len(ev) == 1 and "cooldown budget exhausted" in ev[0]["reason"]
    nxt = eng.next_work("r1", "t1")
    assert nxt.lane_policy.provider is Provider.CLAUDE and nxt.model == "claude-opus-5"


# --- no ping-pong: claude never falls through, one-way only ------------------

def test_claude_failure_never_falls_through(tmp_path, project) -> None:
    """claude is the home provider: a claude dispatch failing never routes to codex, even
    with the flag on and even for a rate-limit at the claude floor."""
    eng = _engine(tmp_path, project, max_rate_limit_waits=0, breaker_threshold=9)
    eng.create_run("r1", ExecutionLane.FULL, cross_provider_fallback=True)
    eng.add_task("r1", "t1")  # no codex tag -> all claude
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake
    w = eng.next_work("r1", "t1")  # scope (claude, opus)
    assert w.lane_policy.provider is Provider.CLAUDE
    # rate-limit claude down its OWN chain to the floor (opus -> sonnet -> haiku)
    for expect in ("claude-sonnet-5", "claude-haiku-4-5"):
        eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
        w = eng.next_work("r1", "t1")
        assert w.model == expect
    # a floor rate-limit with no wait budget is a normal FAILURE — never a fallthrough to codex
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_failed_will_retry"
    assert _fallthrough_events(eng) == []


def test_fallthrough_is_once_per_stage_no_repeat(tmp_path, project) -> None:
    """After codex→claude, a subsequent claude failure retries claude — it does NOT fall
    through again (one-way, bounded; the stage is pinned to claude)."""
    eng = _engine(tmp_path, project, max_attempts=5, breaker_threshold=9)
    w = _advance_codex_task_to(eng, Stage.IMPLEMENT)
    eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                   structured_output={}, error="auth error"))
    claude_w = eng.next_work("r1", "t1")
    assert claude_w.lane_policy.provider is Provider.CLAUDE
    # the claude attempt now genuinely fails -> normal retry on claude, no second fallthrough
    out = eng.record("r1", make_result(claude_w, status=ResultStatus.FAILURE,
                                       structured_output={}, error="boom"))
    assert out["outcome"] == "stage_failed_will_retry"
    assert len(_fallthrough_events(eng)) == 1          # still just the one
    nxt = eng.next_work("r1", "t1")
    assert nxt.lane_policy.provider is Provider.CLAUDE  # stays on claude


# --- provider-tag pin honors the run flag (blanket consent) ------------------

def test_pinned_codex_task_honors_flag_off(tmp_path, project) -> None:
    """A :codex-PINNED task does not fall through when the flag is off (no consent)."""
    eng = _engine(tmp_path, project, breaker_threshold=9)
    eng.create_run("r1", ExecutionLane.FULL)  # flag off
    eng.add_task("r1", "t1", provider_tag="codex")
    while (w := eng.next_work("r1", "t1")) is not None and w.stage is not Stage.IMPLEMENT:
        prov = Provider.CODEX if w.lane_policy.provider is Provider.CODEX else None
        eng.record("r1", make_result(w, provider=prov))
    out = eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                         structured_output={}, error="not logged in"))
    assert out["outcome"] == "stage_failed_will_retry"
    assert _fallthrough_events(eng) == []


def test_global_codex_provider_falls_through_when_opted_in(tmp_path, project) -> None:
    """A run routed entirely to codex (global provider switch) also falls through per-stage
    when the flag is on — the mechanism keys off the lane actually used, not the tag."""
    eng = _engine(tmp_path, project, router=Router(orchestrator_provider=Provider.CODEX,
                                                   execution_mode=ExecutionMode.HEADLESS))
    eng.create_run("r1", ExecutionLane.FULL, cross_provider_fallback=True)
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # intake (engine lane)
    w = eng.next_work("r1", "t1")  # scope -> codex under the global switch
    assert w.lane_policy.provider is Provider.CODEX
    out = eng.record("r1", _codex_result(w, status=ResultStatus.PROVIDER_UNAVAILABLE,
                                         structured_output={}, error="codex login required"))
    assert out["outcome"] == "provider_fallthrough"
    nxt = eng.next_work("r1", "t1")
    assert nxt.stage is Stage.SCOPE and nxt.lane_policy.provider is Provider.CLAUDE


# --- CLI flag wiring ---------------------------------------------------------

def test_cli_init_run_accepts_cross_provider_fallback(tmp_path, capsys) -> None:
    import json

    from orchestrator.cli import main

    args = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject",
            "init-run", "--cross-provider-fallback"]
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cross_provider_fallback"] is True


def test_cli_init_run_cross_provider_fallback_defaults_off(tmp_path, capsys) -> None:
    import json

    from orchestrator.cli import main

    args = ["--root", str(tmp_path), "--run", "r1", "--project", "tests.fakeproject", "init-run"]
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cross_provider_fallback"] is False
