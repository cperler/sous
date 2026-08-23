"""Rate-limit classification across BOTH channels the provider CLI reports on.

Regression for the live ff-v1-b7 failure: claude reported "You've hit your session limit ·
resets 3:50pm" in its terminal ``result`` envelope (``is_error: true``) with an EMPTY
stderr. ``is_rate_limited`` scanned only ``error``, so the stage classified FAILURE, both
attempts fired 2.3s apart into a limit ~2h50m from reset, the breaker failed the task, and
the engine's cooldown path never engaged — ``--wait`` had nothing to wait on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import adapters.execution.transport as transport_module
import orchestrator.engine as engine_module
from adapters.execution.transport import (
    RawResult,
    classify_raw,
    is_rate_limited,
    parse_rate_limit_reset_at,
    to_stage_result,
)
from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.errors import CapacityExhausted
from orchestrator.routing import Router
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

# Verbatim from runs/ff-v1-b7/stages/10/test-attempt{0,1}.stream.jsonl.
_LIVE_NOTICE = "You've hit your session limit · resets 3:50pm (America/New_York)"


def test_session_limit_on_stdout_with_empty_stderr_is_rate_limited() -> None:
    """The live case: the notice rides raw_output, stderr is empty."""
    raw = RawResult(None, exit_code=1, error="", raw_output=_LIVE_NOTICE)
    assert is_rate_limited(raw)
    assert classify_raw(raw) is ResultStatus.RATE_LIMITED


def test_usage_limit_notice_on_stdout_is_rate_limited() -> None:
    raw = RawResult(None, exit_code=1, error="",
                    raw_output="You've hit your usage limit · resets 9:00am")
    assert classify_raw(raw) is ResultStatus.RATE_LIMITED


def test_bare_reset_uses_local_timezone() -> None:
    raw = RawResult(
        None,
        exit_code=1,
        raw_output="You've hit your usage limit · resets 9:00am",
    )
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)  # 8am in New York
    assert parse_rate_limit_reset_at(
        raw, now=now, local_timezone=ZoneInfo("America/New_York")
    ) == datetime(2026, 8, 23, 13, 0, tzinfo=UTC).isoformat()


def test_past_reset_resolves_to_next_occurrence() -> None:
    raw = RawResult(None, exit_code=1, raw_output=_LIVE_NOTICE)
    now = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)  # 4pm in New York
    assert parse_rate_limit_reset_at(raw, now=now) == datetime(
        2026, 8, 24, 19, 50, tzinfo=UTC
    ).isoformat()


@pytest.mark.parametrize(
    "notice",
    [
        "You've hit your session limit · resets later",
        "You've hit your session limit · resets 3:50pm (Not/A_Zone)",
        "FAILED tests/test_limits.py - service resets 3:50pm",
    ],
)
def test_unparseable_or_task_authored_reset_is_ignored(notice: str) -> None:
    raw = RawResult(None, exit_code=1, raw_output=notice)
    assert parse_rate_limit_reset_at(raw) is None


def test_stderr_rate_limit_still_classifies() -> None:
    """The pre-existing stderr channel is unchanged."""
    raw = RawResult(None, exit_code=1, error="API error 429: rate limit exceeded")
    assert classify_raw(raw) is ResultStatus.RATE_LIMITED


def test_task_output_mentioning_rate_limits_is_still_a_failure() -> None:
    """The guard that keeps the raw_output channel narrow.

    raw_output carries TASK content on a failing stage. A task whose own tests exercise a
    rate limiter must not be reclassified out of the retry/breaker accounting.
    """
    raw = RawResult(
        None, exit_code=1, error="",
        raw_output=(
            "FAILED tests/test_api.py::test_rate_limit_retries - AssertionError: "
            "expected 429 to be retried; server returned 'too many requests' and the "
            "client did not back off. usage limit handling is untested."
        ),
    )
    assert not is_rate_limited(raw)
    assert classify_raw(raw) is ResultStatus.FAILURE


def test_clean_failure_is_untouched() -> None:
    raw = RawResult(None, exit_code=1, error="TypeError: undefined")
    assert classify_raw(raw) is ResultStatus.FAILURE


def test_success_is_untouched() -> None:
    raw = RawResult({"ok": True}, exit_code=0)
    assert classify_raw(raw) is ResultStatus.SUCCESS


def test_live_notice_parks_until_reset_then_resumes_same_attempt(
    tmp_path, project, monkeypatch,
) -> None:
    """Live failure path: transport classification through post-reset re-dispatch."""
    issued_at = datetime(2026, 8, 23, 17, 0, tzinfo=UTC)  # 1pm in New York
    reset_at = datetime(2026, 8, 23, 19, 50, tzinfo=UTC)

    class FrozenClock(datetime):
        current = issued_at

        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return cls.current.astimezone(tz)
            return cls.current.replace(tzinfo=None)

    monkeypatch.setattr(transport_module, "datetime", FrozenClock)
    monkeypatch.setattr(engine_module, "datetime", FrozenClock)

    eng = Engine(
        StatusStore(tmp_path),
        CostLedger(tmp_path / "stage-costs.jsonl"),
        project,
        router=Router(allow_fallback=False),
        rate_limit_cooldown_s=900,
        # A stated reset is known-duration work, so even no blind-poll budget must park it.
        max_rate_limit_waits=0,
    )
    eng.create_run("r1")
    eng.add_task("r1", "t1")
    eng.record("r1", make_result(eng.next_work("r1", "t1")))  # deterministic intake
    work = eng.next_work("r1", "t1")
    assert work.stage is Stage.SCOPE and work.attempt == 0

    raw = RawResult(None, exit_code=1, error="", raw_output=_LIVE_NOTICE)
    status = classify_raw(raw)
    assert status is ResultStatus.RATE_LIMITED
    result = to_stage_result(
        work,
        raw,
        status,
        mode=ExecutionMode.HEADLESS,
        provider=Provider.CLAUDE,
    )
    assert result.rate_limit_reset_at == reset_at.isoformat()

    recorded = eng.record("r1", result)
    assert recorded["outcome"] == "stage_rate_limited_cooldown"
    task = eng.store.load_task("r1", "t1")
    assert task.not_before == reset_at.isoformat()
    assert task.rate_limit_waits == 0
    assert eng.dispatchable("r1", now=reset_at - timedelta(microseconds=1)) == []
    with pytest.raises(CapacityExhausted, match="cooldown until"):
        eng.next_work("r1", "t1")

    FrozenClock.current = reset_at
    assert eng.dispatchable("r1") == ["t1"]
    resumed = eng.next_work("r1", "t1")
    assert resumed.stage is Stage.SCOPE
    assert resumed.attempt == work.attempt == 0

    cooldown = [
        event for event in eng.store.read_events("r1")
        if event["type"] == "rate_limit_cooldown"
    ]
    assert len(cooldown) == 1
    assert cooldown[0]["wait_source"] == "provider_reset"
    assert cooldown[0]["provider_reset_at"] == reset_at.isoformat()
    assert cooldown[0]["budget_charged"] is False
    assert cooldown[0]["waits_used"] == 0
