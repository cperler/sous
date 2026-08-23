"""The capacity sensor + actuator (audit gap 3): the usage probe feeds --util, and a
floor-of-chain rate limit is WAITED OUT (bounded cooldown, retry the original model)
instead of instantly burning the attempt — the old check_capacity/handle_rate_limit
behavior, rebuilt on the engine's persisted state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

import orchestrator.engine as engine_module
import orchestrator.scheduler as scheduler_module
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import CapacityExhausted
from orchestrator.scheduler import EXIT_MAX_TICKS, Scheduler
from orchestrator.schemas.enums import ResultStatus, Stage
from orchestrator.status_store import StatusStore
from orchestrator.usage_probe import Usage, fetch_usage, read_usage, resolve_util
from tests.conftest import make_result

# --- usage probe -------------------------------------------------------------

_PAYLOAD = json.dumps({
    "five_hour": {"utilization": 87.4, "resets_at": "2026-07-03T18:00:00+00:00"},
    "seven_day": {"utilization": 41.0, "resets_at": "2026-07-08T00:00:00+00:00"},
})


def test_fetch_usage_parses_endpoint_payload() -> None:
    u = fetch_usage(token_provider=lambda: "tok", http_get=lambda url, headers: _PAYLOAD)
    assert u == Usage(87.4, 41.0, "2026-07-03T18:00:00+00:00", "2026-07-08T00:00:00+00:00")


def test_fetch_usage_is_none_on_any_miss() -> None:
    assert fetch_usage(token_provider=lambda: None, http_get=lambda u, h: _PAYLOAD) is None
    assert fetch_usage(token_provider=lambda: "tok", http_get=lambda u, h: None) is None
    assert fetch_usage(token_provider=lambda: "tok", http_get=lambda u, h: "not json") is None
    assert fetch_usage(token_provider=lambda: "tok", http_get=lambda u, h: "{}") is None


def test_read_usage_serves_cache_within_ttl(tmp_path) -> None:
    cache = tmp_path / "usage.json"
    calls = []

    def fake_fetch():
        calls.append(1)
        return Usage(10.0, 5.0)

    u1 = read_usage(cache, ttl_s=3600, fetch=fake_fetch)
    u2 = read_usage(cache, ttl_s=3600, fetch=fake_fetch)  # served from cache
    assert u1 == u2 == Usage(10.0, 5.0)
    assert len(calls) == 1
    u3 = read_usage(cache, ttl_s=0, fetch=fake_fetch)  # ttl 0 -> always refetch
    assert u3 == Usage(10.0, 5.0) and len(calls) == 2


def test_resolve_util_auto_and_explicit() -> None:
    pct, meta = resolve_util("auto", reader=lambda: Usage(66.0, 20.0))
    assert pct == 66.0 and meta["util_source"] == "auto"
    pct, meta = resolve_util("auto", reader=lambda: None)
    assert pct == 0.0 and "unavailable" in str(meta["probe"])  # stated, not silent
    pct, meta = resolve_util("42.5", reader=lambda: pytest.fail("must not probe"))
    assert pct == 42.5 and meta["util_source"] == "explicit"


# --- floor rate-limit cooldown ------------------------------------------------

def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"), project, **kw)


def _walk_to_floor(eng, run="r1", task="t1"):
    """Drive scope to the fallback floor: opus -> sonnet -> haiku, all rate-limited."""
    eng.create_run(run)
    eng.add_task(run, task)
    eng.record(run, make_result(eng.next_work(run, task)))  # intake (deterministic)
    for expected in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"):
        w = eng.next_work(run, task)
        assert w.stage is Stage.SCOPE and w.model == expected
        out = eng.record(run, make_result(w, status=ResultStatus.RATE_LIMITED,
                                          structured_output={}))
    return out


def test_floor_rate_limit_cooldowns_then_retries_original_model(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, rate_limit_cooldown_s=0)  # elapses immediately
    out = _walk_to_floor(eng)
    assert out["outcome"] == "stage_rate_limited_cooldown"
    task = eng.store.load_task("r1", "t1")
    assert task.rate_limit_waits == 1 and task.not_before is not None
    # cooldown elapsed (0s): the SAME stage re-dispatches on the ORIGINAL role model
    # (not the floor model) at the SAME attempt — the old wait-then-retry semantics.
    w = eng.next_work("r1", "t1")
    assert w.stage is Stage.SCOPE and w.model == "claude-opus-5" and w.attempt == 0
    assert eng.store.load_task("r1", "t1").not_before is None  # stamp cleared on dispatch


def test_cooldown_blocks_dispatch_until_elapsed(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, rate_limit_cooldown_s=3600)
    _walk_to_floor(eng)
    with pytest.raises(CapacityExhausted, match="cooldown until"):
        eng.next_work("r1", "t1")
    assert eng.dispatchable("r1") == []  # the scheduler won't spin on it either
    events = [e for e in eng.store.read_events("r1") if e["type"] == "rate_limit_cooldown"]
    assert len(events) == 1 and events[0]["waits_used"] == 1


def test_cooldown_budget_exhaustion_degrades_to_failure(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, rate_limit_cooldown_s=0, max_rate_limit_waits=1,
                  breaker_threshold=9)
    _walk_to_floor(eng)  # wait #1 consumed
    w = eng.next_work("r1", "t1")  # back on opus after the (0s) cooldown
    # walk the chain to the floor again: opus -> sonnet -> haiku
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    w = eng.next_work("r1", "t1")
    out = eng.record("r1", make_result(w, status=ResultStatus.RATE_LIMITED, structured_output={}))
    assert out["outcome"] == "stage_failed_will_retry"  # budget spent -> real failure
    assert "cooldown budget exhausted" in (eng.store.load_task("r1", "t1").last_error or "")


def test_success_refreshes_cooldown_budget(tmp_path, project) -> None:
    eng = _engine(tmp_path, project, rate_limit_cooldown_s=0)
    _walk_to_floor(eng)
    w = eng.next_work("r1", "t1")
    eng.record("r1", make_result(w))  # scope succeeds after the wait
    assert eng.store.load_task("r1", "t1").rate_limit_waits == 0


# --- scheduler sleeps through cooldowns ----------------------------------------

def test_scheduler_run_sleeps_through_cooldown(tmp_path, project) -> None:
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    # Park t1 in a future cooldown by hand (as a floor rate-limit would).
    until = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

    def _park(t):
        t.not_before = until

    eng.store.update_task("r1", "t1", _park)
    assert eng.dispatchable("r1") == []

    slept: list[int] = []

    def sleeper(s: int) -> None:
        slept.append(s)
        eng.store.update_task("r1", "t1", lambda t: setattr(t, "not_before", None))

    runner = lambda work: [make_result(w) for w in work]  # noqa: E731
    status = Scheduler(eng, max_concurrent=1).run("r1", runner, sleeper=sleeper)
    assert slept and 0 < slept[0] <= 121  # slept the cooldown remainder, then resumed
    assert status["run_state"] == "completed"


def test_scheduler_uses_one_clock_read_at_cooldown_boundary(
    tmp_path, project, monkeypatch,
) -> None:
    eng = _engine(tmp_path, project)
    _walk_to_floor(eng)
    boundary = datetime(2030, 1, 1, tzinfo=UTC)
    before = boundary - timedelta(microseconds=100)
    after = boundary + timedelta(microseconds=100)
    eng.store.update_task(
        "r1", "t1", lambda t: setattr(t, "not_before", boundary.isoformat())
    )

    class TickClock(datetime):
        reads = 0

        @classmethod
        def now(cls, tz=None):
            cls.reads += 1
            return before if cls.reads == 1 else after

    class EngineClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # Before the fix, the second plan still sees the cooldown while the following
            # scheduler clock read sees it expired. Once two tick clocks have been read,
            # next_work() must also see the stamp as expired.
            return before if TickClock.reads < 2 else after

    monkeypatch.setattr(engine_module, "datetime", EngineClock)
    monkeypatch.setattr(scheduler_module, "datetime", TickClock)
    dispatched = []
    slept = []

    def runner(work):
        dispatched.extend(work)
        return [make_result(w) for w in work]

    status = Scheduler(eng, max_concurrent=1).run(
        "r1",
        runner,
        sleeper=slept.append,
        max_ticks=2,
    )

    assert slept == [1]
    assert [work.stage for work in dispatched] == [Stage.SCOPE]
    assert status["scheduler"]["exit_reason"] == EXIT_MAX_TICKS


def test_scheduler_run_without_sleeper_returns_on_cooldown(tmp_path, project) -> None:
    """Default behavior unchanged: no sleeper -> a cooldown stall returns to the caller."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    until = (datetime.now(UTC) + timedelta(seconds=3600)).isoformat()
    eng.store.update_task("r1", "t1", lambda t: setattr(t, "not_before", until))
    runner = lambda work: [make_result(w) for w in work]  # noqa: E731
    status = Scheduler(eng, max_concurrent=1).run("r1", runner)
    assert status["run_state"] == "running"  # nothing forced; caller retries later
