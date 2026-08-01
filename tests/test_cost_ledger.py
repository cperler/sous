"""Tests for the cost ledger (target.md §6.4, closes as-built D6).

The invariant under test: EVERY model call produces exactly one ledger row, and
the recorded cost is computed by the engine's single price table — never trusted
from a runner-supplied value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.cost_ledger import CostLedger
from orchestrator.model_table import DEFAULT_MODEL_TABLE
from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus, Stage
from orchestrator.schemas.work import LaneUsed, StageResult, SubCall, TokenUsage


def make_result(
    *,
    model: str = "claude-opus-5",
    stage: Stage = Stage.IMPLEMENT,
    status: ResultStatus = ResultStatus.SUCCESS,
    provider: Provider = Provider.CLAUDE,
    execution_mode: ExecutionMode = ExecutionMode.HEADLESS,
    input: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    attempt: int = 0,
    cost_usd: float | None = None,
    run_id: str = "run-1",
    task_id: str = "task-1",
    work_item_id: str = "wi-1",
    completed_at: str = "2026-06-20T00:00:00Z",
    session_ref: str | None = None,
) -> StageResult:
    """Build a small StageResult fixture."""
    return StageResult(
        work_item_id=work_item_id,
        content_hash="hash-" + work_item_id,
        run_id=run_id,
        task_id=task_id,
        stage=stage,
        attempt=attempt,
        model=model,
        status=status,
        lane_used=LaneUsed(
            execution_mode=execution_mode,
            provider=provider,
            invocation=f"agent(model={model})",
        ),
        token_usage=TokenUsage(
            input=input,
            output=output,
            cache_read=cache_read,
            cache_write=cache_write,
        ),
        cost_usd=cost_usd,
        session_ref=session_ref,
        completed_at=completed_at,
    )


def test_record_uses_table_pricing_exact(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(model="claude-opus-5", input=1_000_000, output=0)
    row = ledger.record(result)
    # opus input is $5.0 / Mtok -> exactly 5.0 for 1M input tokens.
    assert row["cost_usd"] == 5.0
    assert row["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-5", result.token_usage
    )


def test_record_ignores_runner_supplied_cost(tmp_path: Path) -> None:
    """Authoritative pricing: the engine's table wins over a runner's cost."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-opus-5", input=1_000_000, output=0, cost_usd=999.99
    )
    row = ledger.record(result)
    assert row["cost_usd"] == 5.0  # not 999.99


def test_record_returns_full_row_fields(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-sonnet-5",
        stage=Stage.REVIEW,
        status=ResultStatus.SUCCESS,
        provider=Provider.CLAUDE,
        execution_mode=ExecutionMode.INTERACTIVE,
        input=10,
        output=20,
        cache_read=30,
        cache_write=40,
        attempt=2,
        run_id="run-X",
        task_id="task-X",
        work_item_id="wi-X",
        completed_at="2026-06-20T12:34:56Z",
    )
    row = ledger.record(result)
    assert row["ts"] == "2026-06-20T12:34:56Z"
    assert row["run_id"] == "run-X"
    assert row["task_id"] == "task-X"
    assert row["stage"] == "review"
    assert row["attempt"] == 2
    assert row["model"] == "claude-sonnet-5"
    assert row["provider"] == "claude"
    assert row["lane"] == "interactive"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["cache_read_tokens"] == 30
    assert row["cache_write_tokens"] == 40
    assert row["status"] == "success"
    assert row["work_item_id"] == "wi-X"
    assert row["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-sonnet-5", result.token_usage
    )


def test_every_record_writes_one_row(tmp_path: Path) -> None:
    """N records -> N rows (the every-call invariant)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    n = 5
    for i in range(n):
        ledger.record(make_result(work_item_id=f"wi-{i}", input=100))
    rows = ledger.rows()
    assert len(rows) == n
    # File is genuinely one JSONL line per call.
    text = (tmp_path / "stage-costs.jsonl").read_text().strip().splitlines()
    assert len(text) == n


def test_codex_zero_token_result_still_writes_row(tmp_path: Path) -> None:
    """A codex/zero-token result still records a row (cost 0)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-haiku-4-5",
        provider=Provider.CODEX,
        input=0,
        output=0,
    )
    row = ledger.record(result)
    assert row["cost_usd"] == 0.0
    assert row["provider"] == "codex"
    assert len(ledger.rows()) == 1


def test_no_bypass_two_results_two_rows(tmp_path: Path) -> None:
    """Explicit no-bypass test: a cheap one-shot-looking result and a normal
    result both produce rows, so total_invocations == 2. There is no code path
    that runs a model without a ledger row (closes as-built D6)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    # Looks like a cheap one-shot (the path that used to bypass the ledger).
    one_shot = make_result(
        work_item_id="wi-oneshot",
        model="claude-haiku-4-5",
        stage=Stage.INTAKE,
        input=10,
        output=1,
    )
    normal = make_result(
        work_item_id="wi-normal",
        model="claude-opus-5",
        stage=Stage.IMPLEMENT,
        input=5_000,
        output=2_000,
    )
    ledger.record(one_shot)
    ledger.record(normal)
    summary = ledger.summary()
    assert summary["total_invocations"] == 2
    assert len(ledger.rows()) == 2


# --- sub-call rows (#73 design §4) ------------------------------------------------


def _panel_result(**kw) -> StageResult:
    """A plan-bearing REVIEW result: 3 model calls inside ONE dispatch (2 finders + a
    verifier), each with its own model/usage/duration — the shape §4's ledger rows split."""
    base = make_result(
        model="claude-opus-5",
        stage=Stage.REVIEW,
        work_item_id="wi-panel",
        input=0,
        output=0,
        **kw,
    )
    return base.model_copy(
        update={
            "sub_calls": (
                SubCall(phase="find:code", model="claude-opus-5",
                        usage=TokenUsage(input=1_000_000), duration_s=12.0),
                SubCall(phase="find:tests", model="claude-sonnet-5",
                        usage=TokenUsage(input=1_000_000), duration_s=9.0,
                        schema_retries=2),
                SubCall(phase="verify:3", model="claude-sonnet-5",
                        usage=TokenUsage(output=1_000_000), duration_s=4.0),
            )
        }
    )


def test_sub_calls_write_one_row_each_sharing_work_item_and_no_aggregate(
    tmp_path: Path,
) -> None:
    """Design test (d): one row per sub-call, all sharing work_item_id/stage/attempt,
    distinct phases — and NO aggregate row on top (that would double-count)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(_panel_result(), duration_s=13.0)

    rows = ledger.rows()
    assert len(rows) == 3
    assert [r["phase"] for r in rows] == ["find:code", "find:tests", "verify:3"]
    # No aggregate row: every written row IS a sub-call row.
    assert all("phase" in r for r in rows)
    # The dispatch-level identity is shared, so a report can regroup them as one stage.
    assert {r["work_item_id"] for r in rows} == {"wi-panel"}
    assert {r["stage"] for r in rows} == {"review"}
    assert {r["attempt"] for r in rows} == {0}
    assert {r["run_id"] for r in rows} == {"run-1"}
    # Per-sub-call attribution, not the dispatch's: model, usage and duration differ.
    assert [r["model"] for r in rows] == [
        "claude-opus-5", "claude-sonnet-5", "claude-sonnet-5"
    ]
    assert [r["duration_s"] for r in rows] == [12.0, 9.0, 4.0]
    # A schema-retry loop inside ONE finder rides that finder's row only.
    assert [r["schema_retries"] for r in rows] == [0, 2, 0]
    # File is genuinely three JSONL lines (no hidden fourth).
    assert len((tmp_path / "stage-costs.jsonl").read_text().strip().splitlines()) == 3


def test_sub_call_rows_priced_per_sub_call_model_from_the_table(tmp_path: Path) -> None:
    """Each row is priced from the engine table on ITS OWN model+usage — a panel that
    mixes tiers is not billed at the dispatch's model."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(_panel_result())
    rows = ledger.rows()
    assert rows[0]["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-5", TokenUsage(input=1_000_000)
    )
    assert rows[1]["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-sonnet-5", TokenUsage(input=1_000_000)
    )
    assert rows[2]["cost_usd"] == DEFAULT_MODEL_TABLE.cost_usd(
        "claude-sonnet-5", TokenUsage(output=1_000_000)
    )
    # Pricing the whole dispatch at the dispatch model would give a different number —
    # this is the mis-attribution the per-sub-call rows exist to prevent.
    assert rows[1]["cost_usd"] != DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-5", TokenUsage(input=1_000_000)
    )


def test_sub_calls_summary_total_equals_sum_of_sub_calls(tmp_path: Path) -> None:
    """Design test (d): cost-summary total == Σ sub-calls, with no double-count. A single
    dispatch counts as 3 invocations because 3 model calls actually ran."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    view = ledger.record(_panel_result(), duration_s=13.0)

    rows = ledger.rows()
    expected = round(sum(r["cost_usd"] for r in rows), 6)
    summary = ledger.summary()
    assert summary["total_cost_usd"] == expected
    assert summary["total_invocations"] == 3
    assert ledger.metered_spend() == expected
    # The returned dispatch view sums the rows exactly — and is NOT itself a row.
    assert view["cost_usd"] == expected
    assert view["sub_calls"] == 3
    assert view["input_tokens"] == 2_000_000 and view["output_tokens"] == 1_000_000
    assert view["schema_retries"] == 2
    assert view["duration_s"] == 13.0  # engine-measured dispatch wall time, not 25.0
    assert "phase" not in view
    # Recording nothing further: the view was never appended (still 3 lines).
    assert len(ledger.rows()) == 3
    # Per-model rollup splits the panel by tier rather than billing it all to the dispatch model.
    assert summary["by_model"]["claude-opus-5"]["invocations"] == 1
    assert summary["by_model"]["claude-sonnet-5"]["invocations"] == 2


def test_sub_call_rows_regroup_as_one_stage_in_analysis(tmp_path: Path) -> None:
    """`cost-report` groups by stage/task over the shared dispatch fields, so a workflow
    review reads as ONE stage whose internal breakdown is visible in the raw rows."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(_panel_result())
    analysis = ledger.analysis()
    assert list(analysis["by_stage"]) == ["review"]
    assert analysis["by_stage"]["review"]["invocations"] == 3
    assert analysis["by_stage"]["review"]["cost_usd"] == analysis["total_cost_usd"]
    assert list(analysis["by_task"]) == ["task-1"]


def test_empty_sub_calls_tuple_still_writes_the_dispatch_row(tmp_path: Path) -> None:
    """Defensive: a result carrying an EMPTY sub_calls tuple must not vanish from the
    ledger — the every-call invariant beats the sub-call split."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(model="claude-opus-5", input=1_000_000).model_copy(
        update={"sub_calls": ()}
    )
    row = ledger.record(result)
    assert row["cost_usd"] == 5.0
    assert len(ledger.rows()) == 1
    assert "phase" not in ledger.rows()[0]
    assert "sub_calls" not in row  # an empty tuple is the plain path, not a 0-row panel


# --- crash-replay idempotency on (work_item_id, phase) (#277) --------------------


def test_record_replay_plain_row_converges(tmp_path: Path) -> None:
    """Replaying the SAME StageResult (a crash between the ledger append and the
    task-doc commit) answers from the on-disk row and appends nothing."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(model="claude-opus-5", input=1_000_000)
    first = ledger.record(result)
    replay = ledger.record(result)
    assert replay == first  # the ORIGINAL row, not a re-priced copy
    assert len(ledger.rows()) == 1  # the model call is charged exactly once
    assert ledger.summary()["total_invocations"] == 1


def test_record_replay_panel_converges(tmp_path: Path) -> None:
    """A sub_calls panel replay converges per (work_item_id, phase): still one row per
    sub-call, and the recomputed dispatch view sums the SAME on-disk rows."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    first = ledger.record(_panel_result(), duration_s=13.0)
    replay = ledger.record(_panel_result(), duration_s=13.0)
    assert len(ledger.rows()) == 3  # not 6
    assert replay["cost_usd"] == first["cost_usd"]
    assert replay["sub_calls"] == 3
    assert ledger.summary()["total_invocations"] == 3


def test_record_replay_partial_prior_rows_appends_only_missing_phases(tmp_path: Path) -> None:
    """A crash MID-append (some of a panel's rows on disk) replays to exactly the full
    set: the surviving prior row is kept as-is and only the missing phases append."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(_panel_result())
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n")  # simulate: only the first sub-call row survived

    rows = ledger.record_rows(_panel_result())
    assert [r["phase"] for r in rows] == ["find:code", "find:tests", "verify:3"]
    assert rows[0] == json.loads(lines[0])  # the prior row, untouched
    on_disk = ledger.rows()
    assert len(on_disk) == 3
    assert [r["phase"] for r in on_disk] == ["find:code", "find:tests", "verify:3"]


# --- torn-final-line self-healing (#277 fix cycle 1) ------------------------------


def test_record_replay_after_mid_panel_torn_append_converges(tmp_path: Path) -> None:
    """Failure injection at the ledger append itself: the panel's final row is torn
    mid-write (crash/ENOSPC — a partial line with no terminating newline). The replay
    must converge to the FULL row set — surviving rows kept as-is, the torn row
    truncated under the lock and re-appended — not wedge every later record() on a
    ``JSONDecodeError``."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(_panel_result())
    lines = path.read_text().splitlines()
    # The tear: rows 1-2 landed whole; row 3's append was interrupted mid-line.
    path.write_text(lines[0] + "\n" + lines[1] + "\n" + lines[2][: len(lines[2]) // 2])

    rows = ledger.record_rows(_panel_result())
    assert [r["phase"] for r in rows] == ["find:code", "find:tests", "verify:3"]
    # The surviving prior rows are the ORIGINALS, not re-priced copies.
    assert rows[0] == json.loads(lines[0]) and rows[1] == json.loads(lines[1])
    # Physically repaired: every line decodes, newline-terminated, no duplicates.
    raw = path.read_text()
    assert raw.endswith("\n")
    assert [json.loads(line)["phase"] for line in raw.splitlines()] == [
        "find:code", "find:tests", "verify:3"
    ]


def test_torn_tail_from_another_dispatch_does_not_wedge_record(tmp_path: Path) -> None:
    """The wedge the review rejected: after a torn append, every subsequent record()
    for the run used to raise scanning the file. A later DIFFERENT dispatch must
    record cleanly (repairing the tail), and the torn call's own replay then
    re-appends its lost row — genuinely convergent, no human file surgery."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"work_item_id": "wi-b", "cost_')  # wi-b's append, interrupted

    ledger.record(make_result(work_item_id="wi-c", input=100))  # must not raise
    assert [r["work_item_id"] for r in ledger.rows()] == ["wi-a", "wi-c"]
    ledger.record(make_result(work_item_id="wi-b", input=100))  # wi-b's replay
    assert [r["work_item_id"] for r in ledger.rows()] == ["wi-a", "wi-c", "wi-b"]


def test_rows_skips_torn_tail_without_repairing(tmp_path: Path) -> None:
    """``rows()`` is a read: it tolerates the torn tail (reporting and the engine's
    replay pre-check keep working) but does NOT rewrite the file — only the locked
    record path truncates."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(input=100))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn')
    before = path.read_bytes()
    assert len(ledger.rows()) == 1
    assert path.read_bytes() == before


def test_unterminated_decodable_tail_is_terminated_not_welded(tmp_path: Path) -> None:
    """Review cycle 2's wedge: an append can persist EVERY content byte and lose only
    the trailing newline (ENOSPC/crash at the content/newline boundary). That final
    line decodes, so ``_scan`` reports the file clean — the torn-tail branch never
    fires — and an unguarded append would weld the next row onto it, producing the
    newline-TERMINATED mid-file corruption the cycle-1 repair correctly refuses to
    heal: a permanent, unrecoverable wedge. The guard must newline-terminate the
    valid line in place (never truncate it — it is good data) before appending."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    path.write_bytes(raw[:-1])  # strip ONLY the terminator: all content persisted

    ledger.record(make_result(work_item_id="wi-b", input=100))  # must not weld
    assert [r["work_item_id"] for r in ledger.rows()] == ["wi-a", "wi-b"]
    # Physically two terminated lines: wi-a's row repaired in place, not destroyed.
    text = path.read_text()
    assert text.endswith("\n") and len(text.splitlines()) == 2
    ledger.record(make_result(work_item_id="wi-c", input=100))  # still records
    assert [r["work_item_id"] for r in ledger.rows()] == ["wi-a", "wi-b", "wi-c"]


def test_mid_panel_unterminated_row_replay_converges(tmp_path: Path) -> None:
    """The MULTI-ROW variant of the content/newline-boundary offset: a panel append
    interrupted after row 2's final content byte — row 1 whole, row 2 complete but
    unterminated, row 3 never persisted. The guard must terminate row 2 in place
    (good data, never truncated) and the converge pass re-append ONLY row 3."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(_panel_result())
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n" + lines[1])  # row 2 unterminated, row 3 lost

    rows = ledger.record_rows(_panel_result())
    assert [r["phase"] for r in rows] == ["find:code", "find:tests", "verify:3"]
    # Rows 1-2 are the ORIGINAL on-disk rows — repaired/kept, not re-priced copies.
    assert rows[0] == json.loads(lines[0]) and rows[1] == json.loads(lines[1])
    raw = path.read_text()
    assert raw.endswith("\n")
    assert [json.loads(line)["phase"] for line in raw.splitlines()] == [
        "find:code", "find:tests", "verify:3"
    ]


def test_panel_losing_only_final_terminator_replay_restores_byte_identical(
    tmp_path: Path,
) -> None:
    """Interruption at the very last byte of a panel append: every row's content
    persisted, only the FINAL newline lost. The replay must append no rows — the
    guard restores the terminator and the converge pass finds every phase on disk —
    leaving the file byte-identical to the uninterrupted original."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(_panel_result())
    clean = path.read_bytes()
    path.write_bytes(clean[:-1])  # strip ONLY the final terminator

    rows = ledger.record_rows(_panel_result())
    assert [r["phase"] for r in rows] == ["find:code", "find:tests", "verify:3"]
    assert path.read_bytes() == clean  # newline restored, nothing re-appended


def test_newline_guard_never_fires_on_clean_or_empty_file(tmp_path: Path) -> None:
    """The guard's no-op cases: a pre-existing ZERO-BYTE file and a clean newline-
    terminated multi-row file must gain no stray blank line (and no crash on the
    empty-file seek) — the guard writes only when a terminator is genuinely missing."""
    path = tmp_path / "stage-costs.jsonl"
    path.write_bytes(b"")  # exists but empty: no terminator needed, no seek crash
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    first = path.read_bytes()
    assert not first.startswith(b"\n") and first.endswith(b"\n")
    assert len(first.decode().splitlines()) == 1

    ledger.record(make_result(work_item_id="wi-b", input=100))
    before = path.read_bytes()
    ledger.record(make_result(work_item_id="wi-c", input=100))
    after = path.read_bytes()
    # Exactly one new terminated line, appended directly onto the clean bytes.
    assert after.startswith(before)
    added = after[len(before):]
    assert json.loads(added)["work_item_id"] == "wi-c" and added.endswith(b"\n")


def test_no_strict_prefix_of_a_serialized_row_decodes(tmp_path: Path) -> None:
    """The premise the torn-tail truncate branch rests on (``record_rows``'s offset
    enumeration): a strict prefix of a row's content is NEVER valid JSON, so every
    mid-content interruption is caught by the decode-failure branch. True because a
    row serializes as a JSON object whose closing brace is its final byte; this test
    pins that against writer drift (e.g. a future bare-scalar row, whose prefixes
    could decode and would silently scan clean as a wrong row)."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    ledger.record(_panel_result())
    for line in path.read_text().splitlines():
        assert line.startswith("{") and line.endswith("}")
        for i in range(1, len(line)):
            with pytest.raises(json.JSONDecodeError):
                json.loads(line[:i])


def test_newline_guard_runs_under_the_append_lock(tmp_path: Path, monkeypatch) -> None:
    """The tail check-then-repair must be atomic with the append (same file lock):
    a racy check would let two record() calls both observe a missing terminator and
    double-repair or weld. Instrumented so each concurrent call waits at a barrier
    inside the guard: under the lock the second caller cannot arrive until the first
    finishes, so the barrier must ALWAYS time out — an overlap means the check
    escaped the lock."""
    import threading

    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    raw = path.read_bytes()
    path.write_bytes(raw[:-1])  # unterminated tail: the guard has real work to do

    barrier = threading.Barrier(2)
    overlaps: list[bool] = []
    original = CostLedger._tail_missing_newline

    def instrumented(self: CostLedger) -> bool:
        try:
            barrier.wait(timeout=0.3)
            overlaps.append(True)  # two calls inside the guarded section at once
        except threading.BrokenBarrierError:
            barrier.reset()  # lone arrival timed out: mutual exclusion held
        return original(self)

    monkeypatch.setattr(CostLedger, "_tail_missing_newline", instrumented)
    threads = [
        threading.Thread(
            target=ledger.record, args=(make_result(work_item_id=w, input=100),)
        )
        for w in ("wi-b", "wi-c")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps
    text = path.read_text()
    lines = text.splitlines()
    assert text.endswith("\n") and "" not in lines  # no double-repair blank line
    assert {json.loads(line)["work_item_id"] for line in lines} == {
        "wi-a", "wi-b", "wi-c"
    }


def test_mid_file_undecodable_line_still_raises(tmp_path: Path) -> None:
    """Only the FINAL, unterminated line may be torn (the interrupted-append
    signature). An undecodable line anywhere else is real corruption and must raise —
    silently skipping it would mask damage as convergence."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)
    ledger.record(make_result(work_item_id="wi-a", input=100))
    ledger.record(make_result(work_item_id="wi-b", input=100))
    lines = path.read_text().splitlines()
    path.write_text(lines[0][: len(lines[0]) // 2] + "\n" + lines[1] + "\n")
    with pytest.raises(json.JSONDecodeError):
        ledger.rows()
    with pytest.raises(json.JSONDecodeError):
        ledger.record(make_result(work_item_id="wi-c", input=100))


def test_distinct_work_items_still_append_normally(tmp_path: Path) -> None:
    """The idempotency key is the dispatch id — different dispatches of the same
    stage/model/usage must never converge onto each other."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(make_result(work_item_id="wi-a", input=100))
    ledger.record(make_result(work_item_id="wi-b", input=100))
    assert len(ledger.rows()) == 2


def test_single_sub_call_still_returns_the_aggregate_view(tmp_path: Path) -> None:
    """The return shape keys on the RESULT (has sub_calls?), not on the row count — a
    one-finder panel answers with the same aggregate view a three-finder one does."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(model="claude-opus-5", input=0).model_copy(
        update={
            "sub_calls": (
                SubCall(phase="find:code", model="claude-opus-5",
                        usage=TokenUsage(input=1_000_000), duration_s=3.0),
            )
        }
    )
    view = ledger.record(result, duration_s=4.0)
    assert view["sub_calls"] == 1 and "phase" not in view
    assert view["cost_usd"] == 5.0 == ledger.rows()[0]["cost_usd"]
    assert view["duration_s"] == 4.0 and ledger.rows()[0]["duration_s"] == 3.0
    assert len(ledger.rows()) == 1 and ledger.rows()[0]["phase"] == "find:code"


def test_plain_result_row_is_byte_identical_to_pre_change(tmp_path: Path) -> None:
    """Regression: a result with no sub_calls writes exactly ONE row — same keys, same
    order, no `phase`. This literal is the expected output; if the sub-call split ever leaks
    into the plain path, it fails here. (`session_ref` joined the row in #350, where the
    ledger began de-cumulating session-total usage reports and needed the session key.)"""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    result = make_result(
        model="claude-sonnet-5",
        stage=Stage.REVIEW,
        execution_mode=ExecutionMode.INTERACTIVE,
        input=10, output=20, cache_read=30, cache_write=40,
        attempt=2,
        run_id="run-X", task_id="task-X", work_item_id="wi-X",
        completed_at="2026-06-20T12:34:56Z",
    )
    row = ledger.record(result, duration_s=1.2345)
    expected = {
        "ts": "2026-06-20T12:34:56Z",
        "run_id": "run-X",
        "task_id": "task-X",
        "stage": "review",
        "attempt": 2,
        "model": "claude-sonnet-5",
        "effort": None,
        "provider": "claude",
        "lane": "interactive",
        "session_ref": None,
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 40,
        "cost_usd": DEFAULT_MODEL_TABLE.cost_usd("claude-sonnet-5", result.token_usage),
        "priced": True,
        "metered": True,
        "duration_s": 1.234,
        "status": "success",
        "work_item_id": "wi-X",
        "schema_retries": 0,
    }
    assert row == expected
    assert list(row) == list(expected)  # key ORDER, not just membership
    assert (tmp_path / "stage-costs.jsonl").read_text() == json.dumps(expected) + "\n"


def test_summary_aggregates_per_model_and_totals(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    # Distinct work_item_ids: each is a distinct model call, and recording is
    # idempotent on (work_item_id, phase) — a reused id would converge, not append (#277).
    r1 = make_result(model="claude-opus-5", input=1_000_000, output=0, work_item_id="wi-1")
    r2 = make_result(model="claude-opus-5", input=0, output=1_000_000, work_item_id="wi-2")
    r3 = make_result(model="claude-sonnet-5", input=1_000_000, output=0, work_item_id="wi-3")
    for r in (r1, r2, r3):
        ledger.record(r)

    summary = ledger.summary()
    assert summary["total_invocations"] == 3

    opus_cost = DEFAULT_MODEL_TABLE.cost_usd(
        "claude-opus-5", r1.token_usage
    ) + DEFAULT_MODEL_TABLE.cost_usd("claude-opus-5", r2.token_usage)
    sonnet_cost = DEFAULT_MODEL_TABLE.cost_usd("claude-sonnet-5", r3.token_usage)

    by_model = summary["by_model"]
    assert by_model["claude-opus-5"]["invocations"] == 2
    assert by_model["claude-opus-5"]["input_tokens"] == 1_000_000
    assert by_model["claude-opus-5"]["output_tokens"] == 1_000_000
    assert by_model["claude-opus-5"]["cost_usd"] == round(opus_cost, 6)
    assert by_model["claude-sonnet-5"]["invocations"] == 1
    assert by_model["claude-sonnet-5"]["input_tokens"] == 1_000_000
    assert by_model["claude-sonnet-5"]["cost_usd"] == round(sonnet_cost, 6)

    assert summary["total_cost_usd"] == round(opus_cost + sonnet_cost, 6)
    assert ledger.total_attributed() == summary["total_cost_usd"]


def test_summary_by_effort_spend_and_engine_lane(tmp_path: Path) -> None:
    """summary() rolls spend up by reasoning effort and tags the ENGINE-lane $0 win (#169)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"model": "claude-opus-5", "effort": "high", "lane": "headless", "cost_usd": 5.0},
        {"model": "claude-opus-5", "effort": "high", "lane": "headless", "cost_usd": 1.0},
        {"model": "claude-sonnet-5", "effort": "medium", "lane": "headless", "cost_usd": 0.5},
        # an effort-less deterministic ENGINE-lane row: no model cost, buckets under (default)
        {"model": "none", "effort": None, "lane": "engine", "cost_usd": 0.0},
    ]
    summary = ledger.summary(rows=rows)

    by_effort = summary["by_effort_spend"]
    assert by_effort["high"] == {"invocations": 2, "cost_usd": 6.0}
    assert by_effort["medium"] == {"invocations": 1, "cost_usd": 0.5}
    # None effort normalizes to the (default) bucket
    assert by_effort["(default)"] == {"invocations": 1, "cost_usd": 0.0}

    # ENGINE-lane attribution: one deterministic, genuinely-$0 invocation
    assert summary["engine_lane"] == {"invocations": 1, "cost_usd": 0.0}


def test_summary_empty_effort_surfaces_as_anomaly_not_default(tmp_path: Path) -> None:
    """A present-but-empty '' effort buckets under '' — the data anomaly it is — while a
    genuinely-absent (None/missing) effort still normalizes to '(default)' (#180)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"model": "m", "effort": "", "lane": "headless", "cost_usd": 2.0},
        {"model": "m", "effort": None, "lane": "engine", "cost_usd": 0.0},
        {"model": "m", "lane": "engine", "cost_usd": 0.0},  # effort key absent entirely
    ]
    by_effort = ledger.summary(rows=rows)["by_effort_spend"]
    # '' does NOT collapse into '(default)': it surfaces as its own bucket
    assert by_effort[""] == {"invocations": 1, "cost_usd": 2.0}
    # None + missing both normalize to '(default)'
    assert by_effort["(default)"] == {"invocations": 2, "cost_usd": 0.0}


def test_rows_roundtrip(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    recorded = [
        ledger.record(make_result(work_item_id=f"wi-{i}", input=100 * i))
        for i in range(3)
    ]
    read_back = ledger.rows()
    assert read_back == recorded
    # And matches what's on disk byte-for-byte (JSON parse).
    disk = [
        json.loads(line)
        for line in (tmp_path / "stage-costs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert disk == recorded


def test_rows_empty_when_no_file(tmp_path: Path) -> None:
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    assert ledger.rows() == []
    assert ledger.summary()["total_invocations"] == 0
    assert ledger.total_attributed() == 0.0


# --- cost analysis: per-stage/-task breakdown + the session-reuse win ----------

def test_analysis_session_reuse_win_worked_example(tmp_path: Path) -> None:
    """Opus, in=$5/Mtok, read=10%, write=125%. Stage 1 writes 1M cache tokens
    ($6.25), stage 2 reads 1M cache tokens ($0.50). Total $6.75. The uncached
    counterfactual bills both as fresh input: 2M * $5/Mtok = $10.00, so the
    session-reuse win is $3.25 (32.5% of uncached)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    ledger.record(make_result(work_item_id="w1", stage=Stage.INTAKE, cache_write=1_000_000))
    ledger.record(make_result(work_item_id="w2", stage=Stage.IMPLEMENT, cache_read=1_000_000))

    a = ledger.analysis()
    assert a["total_cost_usd"] == 6.75
    reuse = a["session_reuse"]
    assert reuse["cache_read_savings_usd"] == 4.5
    assert reuse["cache_write_premium_usd"] == 1.25
    assert reuse["net_win_usd"] == 3.25
    assert reuse["uncached_cost_usd"] == 10.0
    assert reuse["win_pct"] == 32.5
    assert reuse["cache_hit_ratio"] == 0.5  # 1M read / 2M input-side
    # per-stage breakdown keyed by stage, ordered-agnostic content
    assert a["by_stage"]["intake"]["cache_write_tokens"] == 1_000_000
    assert a["by_stage"]["intake"]["cost_usd"] == 6.25
    assert a["by_stage"]["implement"]["cache_read_tokens"] == 1_000_000
    assert a["by_stage"]["implement"]["cost_usd"] == 0.5
    assert a["by_task"]["task-1"]["invocations"] == 2


def test_analysis_unpriced_model_excluded_from_counterfactual(tmp_path: Path) -> None:
    """A model absent from the price table still counts toward spend (its recorded
    cost) but is excluded from the cache counterfactual and named in unpriced_models."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"stage": "implement", "task_id": "t", "model": "some-future-model", "cost_usd": 2.0,
         "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 999, "cache_write_tokens": 0},
    ]
    a = ledger.analysis(rows=rows)
    assert a["total_cost_usd"] == 2.0  # spend still counted
    assert a["session_reuse"]["cache_read_savings_usd"] == 0.0  # not priced
    assert a["session_reuse"]["unpriced_models"] == ["some-future-model"]


def test_analysis_empty_is_zeroed(tmp_path: Path) -> None:
    a = CostLedger(tmp_path / "stage-costs.jsonl").analysis()
    assert a["total_cost_usd"] == 0.0
    assert a["session_reuse"]["win_pct"] == 0.0
    assert a["session_reuse"]["cache_hit_ratio"] == 0.0
    assert a["by_stage"] == {} and a["by_task"] == {}


def test_by_effort_groups_and_rates(tmp_path: Path) -> None:
    """(stage, effort, model) grouping with summed spend/duration and retry/failure rates (#141).

    The implement/high group has two rows: a clean first attempt and a failed retry
    (attempt>0, status=failure) — so retry_rate and failure_rate are each 1/2."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"stage": "implement", "effort": "high", "model": "claude-opus-5", "attempt": 0,
         "status": "success", "cost_usd": 2.0, "duration_s": 10.0},
        {"stage": "implement", "effort": "high", "model": "claude-opus-5", "attempt": 1,
         "status": "failure", "cost_usd": 3.0, "duration_s": 20.0},
        {"stage": "deliver", "effort": "low", "model": "claude-sonnet-5", "attempt": 0,
         "status": "success", "cost_usd": 0.5, "duration_s": 4.0},
    ]
    agg = ledger.by_effort(rows=rows)
    # ordered by stage in PIPELINE order then effort then model (#154): IMPLEMENT (pipeline
    # rank 2) precedes DELIVER (rank 4), even though "deliver" < "implement" alphabetically.
    assert [(g["stage"], g["effort"], g["model"]) for g in agg] == [
        ("implement", "high", "claude-opus-5"),
        ("deliver", "low", "claude-sonnet-5"),
    ]
    impl = agg[0]
    assert impl["invocations"] == 2
    assert impl["cost_usd"] == 5.0
    assert impl["total_duration_s"] == 30.0
    assert impl["avg_duration_s"] == 15.0
    assert impl["retries"] == 1 and impl["retry_rate"] == 0.5
    assert impl["failures"] == 1 and impl["failure_rate"] == 0.5
    deliver = agg[1]
    assert deliver["invocations"] == 1
    assert deliver["retry_rate"] == 0.0 and deliver["failure_rate"] == 0.0


def test_by_effort_none_effort_normalized_and_graceful_statuses(tmp_path: Path) -> None:
    """None effort buckets under '(default)'; skipped/rate_limited are NOT failures (#141)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        # effort absent -> (default); a rate_limited re-queue is a graceful fallback, not a failure
        {"stage": "review", "model": "claude-sonnet-5", "attempt": 0, "status": "rate_limited",
         "cost_usd": 0.0},
        {"stage": "review", "model": "claude-sonnet-5", "attempt": 0, "status": "skipped",
         "cost_usd": 0.0},
        {"stage": "review", "model": "claude-sonnet-5", "attempt": 0, "status": "success",
         "cost_usd": 1.0},
    ]
    agg = ledger.by_effort(rows=rows)
    assert len(agg) == 1
    g = agg[0]
    assert g["effort"] == "(default)"
    assert g["invocations"] == 3
    # rate_limited + skipped + success are all non-failures
    assert g["failures"] == 0 and g["failure_rate"] == 0.0
    # tolerant of the missing duration_s field
    assert "duration_s" not in rows[0]
    assert g["total_duration_s"] == 0.0 and g["avg_duration_s"] == 0.0


def test_by_effort_empty_effort_surfaces_as_anomaly_not_default(tmp_path: Path) -> None:
    """A present-but-empty '' effort forms its own (stage, '', model) group rather than
    silently folding into '(default)'; None/missing still normalize to '(default)' (#180)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"stage": "review", "effort": "", "model": "m", "attempt": 0, "status": "success",
         "cost_usd": 2.0},
        {"stage": "review", "effort": None, "model": "m", "attempt": 0, "status": "success",
         "cost_usd": 1.0},
    ]
    agg = ledger.by_effort(rows=rows)
    efforts = {g["effort"] for g in agg}
    assert efforts == {"", "(default)"}
    empty = next(g for g in agg if g["effort"] == "")
    assert empty["invocations"] == 1 and empty["cost_usd"] == 2.0


def test_by_effort_orders_stages_in_pipeline_sequence(tmp_path: Path) -> None:
    """Groups sort by STAGE_ORDER (INTAKE→…→REVIEW), not alphabetically (#154).

    Rows are supplied in scrambled order and include an unknown stage, which must sort
    after every known stage (and alphabetically among any peers)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    rows = [
        {"stage": "review", "effort": "medium", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "intake", "effort": "low", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "deliver", "effort": "low", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "scope", "effort": "high", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "implement", "effort": "high", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "test", "effort": "medium", "model": "m", "attempt": 0, "status": "success"},
        {"stage": "mystery", "effort": "low", "model": "m", "attempt": 0, "status": "success"},
    ]
    assert [g["stage"] for g in ledger.by_effort(rows=rows)] == [
        "intake", "scope", "implement", "test", "deliver", "review", "mystery",
    ]


def test_by_effort_empty_is_empty_list(tmp_path: Path) -> None:
    assert CostLedger(tmp_path / "stage-costs.jsonl").by_effort() == []


def test_by_effort_self_read_is_memoised_and_stat_invalidated(tmp_path: Path) -> None:
    """#220: the self-read by_effort() path memoises its O(rows) aggregation, keyed on the
    ledger file's stat, so the band-edge downshift check (fired per dispatch, per tick) does
    not re-scan the whole JSONL every call — while an appended row still invalidates the memo
    and an explicit ``rows`` arg neither reads the cache nor poisons it."""
    path = tmp_path / "stage-costs.jsonl"
    ledger = CostLedger(path)

    def _append(row: dict) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # No file yet -> [] and a stable memo across repeated calls (same object == cache hit).
    assert ledger.by_effort() == []
    assert ledger.by_effort() is ledger.by_effort()

    _append({"stage": "implement", "effort": "high", "model": "m", "attempt": 0,
             "status": "success", "cost_usd": 1.0})
    first = ledger.by_effort()
    assert [g["invocations"] for g in first] == [1]
    # Unchanged file -> the identical cached object is returned (no re-scan).
    assert ledger.by_effort() is first

    # A newly appended row grows the file, so the stat key changes and the memo recomputes.
    _append({"stage": "implement", "effort": "high", "model": "m", "attempt": 1,
             "status": "failure", "cost_usd": 2.0})
    second = ledger.by_effort()
    assert second is not first
    assert [g["invocations"] for g in second] == [2]
    assert second[0]["cost_usd"] == 3.0 and second[0]["failure_rate"] == 0.5

    # An explicit rows= arg bypasses the cache entirely and leaves the self-read memo intact.
    assert ledger.by_effort(rows=[]) == []
    assert ledger.by_effort() is second


def test_codex_session_totals_are_de_cumulated_per_stage(tmp_path: Path) -> None:
    """A codex turn.completed reports the whole SESSION's totals, not the turn's (#350).

    Stages chain through one resumed session, so each stage re-reports a running total that
    already contains its predecessors. Summing those rows put `batch-codex-3` 22x over its
    real spend. Each row must carry only what its own stage added.
    """
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    common = dict(provider=Provider.CODEX, model="gpt-5.5", session_ref="th-1")

    scope = ledger.record(make_result(**common, work_item_id="wi-scope", input=1_000, output=100))
    implement = ledger.record(
        make_result(**common, work_item_id="wi-impl", stage=Stage.IMPLEMENT,
                    input=9_000, output=500)
    )
    deliver = ledger.record(
        make_result(**common, work_item_id="wi-deliver", stage=Stage.DELIVER,
                    input=12_000, output=800)
    )

    assert scope["input_tokens"] == 1_000
    assert implement["input_tokens"] == 8_000, "the second stage re-billed the first"
    assert deliver["input_tokens"] == 3_000
    assert [scope["output_tokens"], implement["output_tokens"], deliver["output_tokens"]] == [
        100, 400, 300
    ]
    # The deltas still sum to the last cumulative figure the provider reported.
    assert sum(r["input_tokens"] for r in ledger.rows()) == 12_000

    # A different session starts its own accounting rather than differencing against this one.
    other = ledger.record(
        make_result(provider=Provider.CODEX, model="gpt-5.5", session_ref="th-2",
                    work_item_id="wi-other", input=400, output=40)
    )
    assert other["input_tokens"] == 400


def test_claude_per_call_usage_is_not_de_cumulated(tmp_path: Path) -> None:
    """Claude reports per-call usage, so two calls on one session are two full rows (#350).
    De-cumulating them would erase the second call's real spend."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    common = dict(provider=Provider.CLAUDE, model="claude-opus-5", session_ref="sess-1")

    first = ledger.record(make_result(**common, work_item_id="wi-1", input=1_000, output=100))
    second = ledger.record(make_result(**common, work_item_id="wi-2", input=1_000, output=100))

    assert first["input_tokens"] == second["input_tokens"] == 1_000
    assert sum(r["input_tokens"] for r in ledger.rows()) == 2_000


def test_a_shrinking_cumulative_report_never_prices_as_a_credit(tmp_path: Path) -> None:
    """A cumulative counter should only grow; if one resets, the row floors at zero rather
    than going negative and refunding the session's real spend (#350)."""
    ledger = CostLedger(tmp_path / "stage-costs.jsonl")
    common = dict(provider=Provider.CODEX, model="gpt-5.5", session_ref="th-reset")

    ledger.record(make_result(**common, work_item_id="wi-a", input=5_000, output=500))
    after_reset = ledger.record(
        make_result(**common, work_item_id="wi-b", stage=Stage.TEST, input=200, output=20)
    )

    assert after_reset["input_tokens"] == 0
    assert after_reset["output_tokens"] == 0
    assert after_reset["cost_usd"] == 0.0
