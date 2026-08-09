"""Rate-limit classification across BOTH channels the provider CLI reports on.

Regression for the live ff-v1-b7 failure: claude reported "You've hit your session limit ·
resets 3:50pm" in its terminal ``result`` envelope (``is_error: true``) with an EMPTY
stderr. ``is_rate_limited`` scanned only ``error``, so the stage classified FAILURE, both
attempts fired 2.3s apart into a limit ~2h50m from reset, the breaker failed the task, and
the engine's cooldown path never engaged — ``--wait`` had nothing to wait on.
"""

from __future__ import annotations

from adapters.execution.transport import RawResult, classify_raw, is_rate_limited
from orchestrator.schemas.enums import ResultStatus

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
