"""Append-only cost ledger (target.md §6.4, closes as-built D6).

EVERY model call gets exactly one row. ``record`` is the only entry point and it
always writes, so there is no code path that runs a model without a ledger row —
the as-built bug was that the one-shot path bypassed ``record_stage_invocation``.

The unit is the model CALL, not the dispatch (#73 design §4): a plan-bearing dispatch
whose result carries ``sub_calls`` (each finder / verifier of a review panel) writes one
row PER SUB-CALL — sharing the dispatch's ``work_item_id``/stage/attempt and discriminated
by ``phase`` — and NO aggregate row on top. Sums are the report's job; a double-counted
aggregate is worse than one more row to add up. Without ``sub_calls`` the dispatch is
itself the single call and the row is exactly what it always was.

Pricing is authoritative: the cost written is computed from the engine's single
``model_table`` (the same table used everywhere), NOT from the runner-supplied
``StageResult.cost_usd`` (nor a ``SubCall``'s self-reported usage pricing). A runner
cannot under- or over-report spend; the engine's table is the single source of truth.

Recording is IDEMPOTENT on the durable key ``(work_item_id, phase)`` (#277): a
dispatch's work_item_id is unique per model call (sub-calls share it, discriminated
by ``phase``), so replaying the same StageResult after a crash mid-``Engine.record``
converges on the rows already on disk instead of charging the call twice. The
scan-then-append runs under an exclusive file lock so two concurrent duplicate
records cannot both append. The ledger append is itself a crash boundary: an
interrupted write (crash/ENOSPC) can leave a torn FINAL line, and the scan is
self-healing for exactly that tear — ``record_rows`` truncates it under the lock and
the converge pass re-appends the row the interrupted write lost, so the replay still
converges instead of wedging every later ``record`` on a ``JSONDecodeError``. A write
interrupted at the content/newline boundary instead leaves a COMPLETE, decodable final
line missing only its terminator — valid data, never truncated: ``record_rows``
newline-terminates it in place before appending, so the next row cannot weld onto it
(``record_rows`` enumerates why those two branches cover every interruption offset).
An undecodable line anywhere ELSE is real corruption and still raises (see ``_scan``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model_table import DEFAULT_MODEL_TABLE, ModelTable
from .schemas.enums import STAGE_ORDER
from .schemas.work import StageResult, TokenUsage
from .status_store import file_lock

# Pipeline-execution rank for a stage's string value (#154): lets by_effort() order
# groups in the natural INTAKE→SCOPE→IMPLEMENT→TEST→DELIVER→REVIEW sequence instead of
# alphabetically. Unknown/malformed stages sort last (after every known stage).
_STAGE_RANK: dict[str, int] = {stage.value: i for i, stage in enumerate(STAGE_ORDER)}

# Canonical model-call attribution fields that BOTH audit write paths must surface under
# the SAME top-level key (#164): the cost-ledger row (``CostLedger.record``) and every
# per-stage JSON log payload (``engine._record_result`` and ``engine.abandon``). The
# ledger is the primary cost-attribution artifact, so the canonical set lives here and the
# stage-log paths agree with it. Adding a new attribution field means adding it HERE — the
# parity test (``tests/test_attribution_field_parity.py``) then fails until BOTH paths
# carry it, catching the next missing-field omission at CI time (the #151 `effort` gap
# regressing again). Only fields present as a top-level key in both are listed: provider
# and lane are attribution too, but the stage log nests them inside ``lane_used`` (a dict)
# while the ledger flattens them (``provider``/``lane``), so they are a deliberate
# representational difference, not a literal-key parity field.
_ATTRIBUTION_FIELDS: frozenset[str] = frozenset({"model", "effort", "cost_usd"})


class CostLedger:
    """A ``stage-costs.jsonl`` file: one row per model invocation."""

    def __init__(self, path: Path, model_table: ModelTable = DEFAULT_MODEL_TABLE) -> None:
        self.path = Path(path)
        self.model_table = model_table
        # by_effort() memo (#220): the aggregation is O(rows), and the band-edge downshift
        # check (engine._observed_downshift_rate) fires it on every dispatch decision across
        # a scheduler process's thousands of ticks. Cache the last self-read aggregation,
        # keyed on the ledger file's stat (see _stat_key). Only the ``rows is None`` path
        # (we read the file) is cached; an explicit ``rows`` arg is the caller's own input.
        self._by_effort_cache: list[dict] | None = None
        self._by_effort_cache_key: tuple[int, int] | None = None

    def record(self, result: StageResult, *, duration_s: float | None = None) -> dict:
        """Append this invocation's JSONL row(s) and return the DISPATCH-level view.

        One row per model call (see the module docstring): the plain path writes exactly
        one row and returns it, unchanged; a ``sub_calls``-bearing result writes one row
        per sub-call and returns a non-persisted aggregate over them (``cost_usd`` = Σ
        sub-calls) for the caller that attributes one number to the stage.

        Cost is recomputed from ``model_table`` (authoritative) — the runner's
        ``result.cost_usd`` is deliberately ignored. ``duration_s`` is the engine-
        measured wall time of the dispatch (dispatch->record). Idempotent (#277):
        replaying a result whose rows are already on disk answers from those rows
        (the view recomputed over them) without appending — see ``record_rows``.
        """
        rows = self.record_rows(result, duration_s=duration_s)
        # Keyed on the RESULT's shape, not on ``len(rows)``: a one-sub-call panel must
        # still answer with the aggregate view, so a caller never has to guess whether the
        # dict it got back is a persisted row or a sum.
        if result.sub_calls:
            return self._dispatch_view(result, rows, duration_s=duration_s)
        return rows[0]

    def record_rows(
        self, result: StageResult, *, duration_s: float | None = None
    ) -> list[dict]:
        """Append one JSONL row per MODEL CALL in this dispatch and return them (#73 §4).

        ``result.sub_calls`` empty/absent -> a single dispatch row, byte-identical to the
        pre-#73 ledger line (no ``phase`` key at all). Otherwise one row per ``SubCall``,
        each priced on its OWN model and usage, and deliberately no aggregate row — the
        report sums; the ledger must never double-count.

        Idempotent on ``(work_item_id, phase)`` (#277): rows already on disk for this
        dispatch are returned as-is and NOT re-appended, so a replay after a crash
        mid-``Engine.record`` (ledger row committed, task doc not) converges. A partial
        prior write (some of a panel's sub-call rows) appends only the missing phases.
        The scan-then-append is atomic under an exclusive lock on the ledger file, so
        two concurrent duplicate records cannot both append the same call.

        Self-healing for a torn tail: a crash/ENOSPC mid-append can leave a partial
        FINAL line (an append is content-then-newline, so a torn line never has its
        terminating newline). The locked scan detects exactly that tear and truncates
        it BEFORE appending — appending onto torn bytes would weld the new row into
        them and turn a recoverable tear into real mid-file corruption — and the
        converge pass then re-appends the row the interrupted write lost. An
        undecodable line anywhere but the tail still raises (see ``_scan``).

        Together the truncate branch and the newline guard cover EVERY byte offset
        at which an append (content bytes, then ``b"\\n"``) can be interrupted:

        * 0 bytes persisted — the file is exactly its prior (clean) state;
        * a strict prefix of the content — never valid JSON (a serialized object's
          top-level closing brace is its FINAL byte, so every proper prefix has an
          unclosed brace or string), hence undecodable and unterminated: the
          torn-tail branch truncates it;
        * all content bytes, terminator lost — complete VALID data: the newline
          guard terminates it in place (never truncates — no good row destroyed);
        * content + newline — the append completed; nothing to repair.

        A multi-row (panel) append is a sequence of such writes, so an interruption
        lands inside exactly one row's content-or-terminator window; earlier rows are
        whole lines and the converge pass re-appends the rows that never landed."""
        if result.sub_calls:
            built = [
                self._row(
                    result,
                    model=sub.model,
                    usage=sub.usage,
                    duration_s=sub.duration_s,
                    schema_retries=sub.schema_retries,
                    phase=sub.phase,
                    usage_recovered=sub.usage_recovered,
                )
                for sub in result.sub_calls
            ]
        else:
            built = [
                self._row(
                    result,
                    model=result.model,
                    usage=result.token_usage,
                    duration_s=duration_s,
                    schema_retries=result.schema_retries,
                    phase=None,
                    usage_recovered=result.usage_recovered,
                )
            ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            on_disk, torn_at = self._scan()
            if torn_at is not None:
                # Repair the tear before the append lands on top of it: truncate drops
                # ONLY the undecodable partial tail (byte-offset precise), the converge
                # pass below re-appends whatever that interrupted write was carrying.
                with self.path.open("r+b") as fh:
                    fh.truncate(torn_at)
            elif self._tail_missing_newline():
                # The OTHER interruption offset (#277 review cycle 2): the write
                # persisted every content byte and lost ONLY the terminating newline
                # (ENOSPC/crash can end a write at any offset, including len-1). The
                # final line is complete, decodable, VALID data — so it must NOT be
                # truncated (that would destroy a good row) — but appending onto it
                # would weld the next row into one line: exactly the mid-file
                # corruption ``_scan`` correctly refuses to heal, i.e. a permanent
                # wedge. Terminate it in place before the append.
                with self.path.open("ab") as fh:
                    fh.write(b"\n")
            existing = [r for r in on_disk if r.get("work_item_id") == result.work_item_id]
            rows, to_append = self._converge(built, existing) if existing else (built, built)
            if to_append:
                with self.path.open("a", encoding="utf-8") as fh:
                    for row in to_append:
                        fh.write(json.dumps(row) + "\n")
        return rows

    def _tail_missing_newline(self) -> bool:
        """True when the ledger exists, is non-empty, and its last byte is not ``\\n``.

        The write-side check for the interruption offset ``_scan`` cannot see: a final
        line whose content fully persisted but whose terminator did not is decodable,
        so it scans clean (``torn_at is None``) — yet appending onto it would weld two
        rows into one line. Called only under ``record_rows``'s file lock, right before
        the append, so the answer cannot go stale between check and write."""
        try:
            with self.path.open("rb") as fh:
                if fh.seek(0, os.SEEK_END) == 0:  # empty file needs no terminator
                    return False
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except FileNotFoundError:
            return False

    def existing_rows_for(self, work_item_id: str) -> list[dict]:
        """The rows already recorded for one dispatch, in file order (#277).

        The replay-detection read: ``Engine.record`` uses a non-empty answer as the
        signal that a PRIOR record attempt already charged this dispatch (and so its
        audit events may also already be on disk). O(rows) over the JSONL — the same
        read every aggregation here performs, and a run's ledger stays small (one row
        per model call)."""
        return [row for row in self.rows() if row.get("work_item_id") == work_item_id]

    @staticmethod
    def _converge(built: list[dict], existing: list[dict]) -> tuple[list[dict], list[dict]]:
        """Fold freshly-built rows onto the dispatch's rows already on disk.

        Returns ``(rows, to_append)``: ``rows`` is the built list with each row replaced
        by its on-disk counterpart when one exists (same ``phase`` key; duplicates of a
        phase pair up positionally), preserving the ORIGINAL priced/timestamped row;
        ``to_append`` is the subset of built rows with no counterpart — the missing
        phases of a partial prior write. A full prior write appends nothing."""
        by_phase: dict[str | None, list[dict]] = {}
        for row in existing:
            by_phase.setdefault(row.get("phase"), []).append(row)
        used: dict[str | None, int] = {}
        rows: list[dict] = []
        to_append: list[dict] = []
        for row in built:
            key = row.get("phase")
            i = used.get(key, 0)
            prior = by_phase.get(key, [])
            if i < len(prior):
                rows.append(prior[i])
                used[key] = i + 1
            else:
                rows.append(row)
                to_append.append(row)
        return rows, to_append

    def _row(
        self,
        result: StageResult,
        *,
        model: str,
        usage: TokenUsage,
        duration_s: float | None,
        schema_retries: int,
        phase: str | None,
        usage_recovered: bool = True,
    ) -> dict:
        """Build one ledger row for a single model call within ``result``.

        Everything but ``model``/``usage``/``duration_s``/``schema_retries``/``phase`` is
        dispatch-level and therefore shared by every row of the same call — notably
        ``work_item_id``, ``stage`` and ``attempt``, which is what lets a report re-group a
        multi-call dispatch into one stage with an internal breakdown. ``phase`` is
        APPENDED LAST and only when set, so a plain (single-call) row is byte-identical to
        the pre-#73 line."""
        # Tolerant pricing: an unknown model id (e.g. a new provider model not yet in the
        # table) must NOT raise — every call still gets exactly one row. An unpriced call
        # is flagged (priced=False) and costed at 0.0, the same tolerance analysis() has.
        cost, priced = self.model_table.try_cost_usd(model, usage)
        # HONESTY flag: the interactive lane cannot meter per-call usage in-session, so
        # its zero-token rows are UNMETERED (cost unknown), not free. Metered lanes and
        # the deterministic engine lane (genuinely $0) stay metered=True. Renderers use
        # this to say "n/a / unmetered" instead of a confident $0.0000.
        tokens_seen = usage.input + usage.output + usage.cache_read + usage.cache_write
        metered = not (
            result.lane_used.execution_mode.value == "interactive" and tokens_seen == 0
        )
        # #319: the SAME honesty rule for a call whose usage could not be read at all — a
        # stage killed before its provider printed a usage report. Its zeros are an unknown,
        # not a measurement, so the row must not claim to be a priced, metered $0.00: that
        # reads as "this attempt was free" when it may have burned minutes of Opus. Marking
        # it unmetered also keeps it OUT of `metered_spend`, so the budget gate reports what
        # it can actually account for rather than silently absorbing a guess of zero.
        if not usage_recovered:
            priced = False
            metered = False
        row = {
            "ts": result.completed_at,
            "run_id": result.run_id,
            "task_id": result.task_id,
            "stage": result.stage.value,
            "attempt": result.attempt,
            "model": model,
            # #96: the reasoning effort the dispatch ran at, alongside model — so a cost
            # report can split spend by effort as well as tier. None on effort-less rows.
            "effort": result.effort,
            "provider": result.lane_used.provider.value,
            "lane": result.lane_used.execution_mode.value,
            "input_tokens": usage.input,
            "output_tokens": usage.output,
            "cache_read_tokens": usage.cache_read,
            "cache_write_tokens": usage.cache_write,
            "cost_usd": cost,
            "priced": priced,
            "metered": metered,
            "duration_s": round(duration_s, 3) if duration_s is not None else None,
            "status": result.status.value,
            "work_item_id": result.work_item_id,
            # Corrective schema-retries the transport spent salvaging this call's output (#32).
            # Almost always 0; a non-zero value flags an invocation that cost extra model turns.
            # On a sub-call row this is that SUB-CALL's own retry count: a `_schema_retry_loop`
            # spent inside one finder rides that finder's row, not the dispatch's.
            "schema_retries": schema_retries,
        }
        if phase is not None:
            row["phase"] = phase
        return row

    def _dispatch_view(
        self, result: StageResult, rows: list[dict], *, duration_s: float | None
    ) -> dict:
        """Aggregate the just-written sub-call ``rows`` into a dispatch-level dict.

        NOT written to the ledger — writing it would be exactly the double-count #73 §4
        forbids. It exists only for the caller that needs ONE number per dispatch (the
        engine attributes ``cost_usd`` to the stage log and the task doc), and it is a
        pure sum of the rows on disk, so ``view["cost_usd"] == Σ rows``. Token/retry
        counts sum likewise; ``duration_s`` stays the ENGINE-measured dispatch wall time
        (summing sub-call durations would over-count concurrent finders). ``priced`` /
        ``metered`` are ``all(...)``: one unpriced or unmetered sub-call means the
        aggregate understates the dispatch, and saying so is the honest direction."""
        view = self._row(
            result,
            model=result.model,
            usage=result.token_usage,
            duration_s=duration_s,
            schema_retries=result.schema_retries,
            phase=None,
            usage_recovered=result.usage_recovered,
        )
        view.update(
            {
                "input_tokens": sum(r["input_tokens"] for r in rows),
                "output_tokens": sum(r["output_tokens"] for r in rows),
                "cache_read_tokens": sum(r["cache_read_tokens"] for r in rows),
                "cache_write_tokens": sum(r["cache_write_tokens"] for r in rows),
                "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
                "priced": all(r["priced"] for r in rows),
                "metered": all(r["metered"] for r in rows),
                "schema_retries": sum(r["schema_retries"] for r in rows),
                # How many ledger rows this dispatch actually wrote — present only on the
                # (non-persisted) view, so a caller can tell an aggregate from a real row.
                "sub_calls": len(rows),
            }
        )
        return view

    def rows(self) -> list[dict]:
        """Read back every recorded row (fully-decodable lines; a torn tail is skipped).

        Read-only view of ``_scan``: a torn FINAL line — the signature of an append
        interrupted by a crash/ENOSPC — is tolerated (skipped, not repaired; only the
        locked ``record_rows`` path truncates), so readers like ``Engine.record``'s
        replay pre-check and ``summary()`` keep working after such a crash instead of
        raising until a human repairs the file by hand. An undecodable line anywhere
        else still raises — mid-file corruption must never be silently skipped."""
        return self._scan()[0]

    def _scan(self) -> tuple[list[dict], int | None]:
        """Parse the ledger JSONL, tolerating exactly a torn FINAL line (#277).

        Returns ``(rows, torn_at)``: the decoded rows in file order, and the byte
        offset where a torn trailing line begins (``None`` when the file is clean or
        absent). A tear is recognized ONLY as the very last content of the file with
        no terminating newline — an append writes content-then-newline, so an
        interrupted append can never leave a newline after its partial bytes. Any
        other undecodable line is real corruption and raises ``JSONDecodeError``
        (never silently skipped: mid-file damage must surface, not be masked)."""
        if not self.path.exists():
            return [], None
        data = self.path.read_bytes()
        rows: list[dict] = []
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            line = data[pos:] if nl == -1 else data[pos:nl]
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if nl == -1:  # final line, unterminated: the torn-append signature
                        return rows, pos
                    raise
            if nl == -1:
                break
            pos = nl + 1
        return rows, None

    def summary(self, rows: list[dict] | None = None) -> dict:
        """Aggregate the ledger: totals plus per-model, per-effort, and ENGINE-lane rollups.

        Alongside the per-model breakdown, two additive rollups over the already-present
        row fields (#169, no schema change):

        * ``by_effort_spend`` — spend split by the reasoning effort the dispatch ran at
          (#96), keyed by ``high``/``medium``/``low`` with ``None`` normalized to the
          ``(default)`` label. Lets an operator see how much spend high-effort stages
          consume vs medium/low — the actionable signal for tuning ``effort_pin`` defaults.
        * ``engine_lane`` — deterministic ENGINE-lane attribution (#68/#120): the count and
          (always $0) cost of rows the engine ran with no model call (``lane == "engine"``),
          so the deterministic-stage cost win is a visible line item, not a ledger scan.

        Accepts pre-read ``rows`` so a caller (engine.status) reads the JSONL once
        and shares it. Tolerant of a malformed/partial row via ``.get`` defaults."""
        by_model: dict[str, dict] = {}
        by_effort_spend: dict[str, dict] = {}
        engine_lane = {"invocations": 0, "cost_usd": 0.0}
        total_cost = 0.0
        total_invocations = 0
        unmetered_calls = 0
        total_wall_s = 0.0
        for row in (self.rows() if rows is None else rows):
            total_invocations += 1
            cost = row.get("cost_usd") or 0.0
            total_cost += cost
            if row.get("metered") is False:
                unmetered_calls += 1
            total_wall_s += row.get("duration_s") or 0.0
            bucket = by_model.setdefault(
                row.get("model", "unknown"),
                {
                    "invocations": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            bucket["invocations"] += 1
            bucket["input_tokens"] += row.get("input_tokens", 0) or 0
            bucket["output_tokens"] += row.get("output_tokens", 0) or 0
            bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)
            # Per-effort spend rollup (#145/#152): a genuinely-absent effort (None/missing)
            # -> '(default)' so effort-less rows (deterministic ENGINE-lane stages, specs
            # without a default) still bucket cleanly. An explicit ``is None`` guard, not a
            # falsy one, so a present-but-empty '' effort surfaces as its own bucket — the
            # data anomaly it is — instead of hiding under '(default)' (#180).
            effort = row.get("effort")
            ebucket = by_effort_spend.setdefault(
                "(default)" if effort is None else effort,
                {"invocations": 0, "cost_usd": 0.0},
            )
            ebucket["invocations"] += 1
            ebucket["cost_usd"] = round(ebucket["cost_usd"] + cost, 6)
            # Deterministic ENGINE-lane attribution (#120): rows the engine ran itself.
            if row.get("lane") == "engine":
                engine_lane["invocations"] += 1
                engine_lane["cost_usd"] = round(engine_lane["cost_usd"] + cost, 6)
        return {
            "total_cost_usd": round(total_cost, 6),
            "total_invocations": total_invocations,
            "unmetered_calls": unmetered_calls,
            "total_wall_s": round(total_wall_s, 1),
            "by_model": by_model,
            "by_effort_spend": by_effort_spend,
            "engine_lane": engine_lane,
        }

    def total_attributed(self) -> float:
        """Convenience: total cost attributed across the whole ledger."""
        return self.summary()["total_cost_usd"]

    def metered_spend(self, rows: list[dict] | None = None) -> float:
        """USD spent on METERED rows only — the honest figure a budget gate checks (#34).

        Unmetered interactive rows record $0 (they carry no per-call usage), so they add
        nothing anyway; excluding them explicitly keeps the budget semantics honest — a
        run billed to a subscription can't accidentally count as spend. Accepts pre-read
        ``rows`` so a caller (status) reads the JSONL once and shares it."""
        total = 0.0
        for row in (self.rows() if rows is None else rows):
            if row.get("metered") is False:
                continue
            total += row.get("cost_usd") or 0.0
        return round(total, 6)

    def _stat_key(self) -> tuple[int, int] | None:
        """``(size, mtime_ns)`` of the ledger file, or ``None`` when it does not exist yet.

        The ledger is append-only, so every recorded row grows the file (and bumps mtime) —
        making this an EXACT invalidation key for the by_effort() memo (#220): the first
        call after a ``record()`` recomputes, and every band-edge call in between reuses the
        cached aggregation. Robust to a rewrite/truncation (tests recreate the file) because
        mtime moves even when size happens to match."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def by_effort(self, rows: list[dict] | None = None) -> list[dict]:
        """Split spend/duration/retry+failure rates by ``(stage, effort, model)`` (#141).

        Closes the #96 loop: the per-stage effort defaults (SCOPE/IMPLEMENT=high,
        TEST/REVIEW=medium, DELIVER=low) are a-priori judgments; this aggregation lets
        real run history validate or revise them (e.g. does DELIVER at low actually retry
        more?). Each group carries, over its rows:

        * ``invocations`` — number of ledger rows in the group
        * ``cost_usd`` — summed (authoritative) spend
        * ``unmetered`` — rows in the group whose usage was never recorded (#319/#331).
          Those rows contribute ``cost_usd: 0.0``, so a group with ``unmetered > 0`` has a
          ``cost_usd`` that is a FLOOR, not a measurement; ``unmetered == invocations``
          means the group's spend is entirely unknown. Renderers must say so rather than
          print a bare, confident dollar figure.
        * ``total_duration_s`` / ``avg_duration_s`` — wall time summed and averaged
        * ``retries`` / ``retry_rate`` — rows that are re-dispatches (``attempt > 0``)
        * ``failures`` / ``failure_rate`` — rows whose status is a real failure, using the
          same semantics as ``retrospective._is_failure_status`` (anything not in
          ``success``/``skipped``/``rate_limited``; the last two are graceful, not failures)

        ``effort`` of ``None`` is normalized to the ``(default)`` label so effort-less rows
        still bucket cleanly. Tolerant of malformed/partial rows via ``.get`` defaults, and
        accepts pre-read ``rows`` so a caller reads the JSONL once. Returned as a list of
        group dicts ordered by ``stage`` (in pipeline-execution order, not alphabetical —
        #154) then ``effort`` then ``model`` for readability. Unknown/malformed stages sort
        after every known stage, and alphabetically among themselves (#166)."""
        # #220 memo: cache only the self-read path; an explicit ``rows`` is the caller's own
        # input and is aggregated directly. The cached groups are read-only — every caller
        # (engine._observed_downshift_rate, render_by_effort) only reads via ``.get`` — so
        # the cached list is returned as-is; callers must not mutate it.
        if rows is not None:
            return self._aggregate_by_effort(rows)
        key = self._stat_key()
        if self._by_effort_cache is not None and self._by_effort_cache_key == key:
            return self._by_effort_cache
        result = self._aggregate_by_effort(self.rows())
        self._by_effort_cache_key = key
        self._by_effort_cache = result
        return result

    def _aggregate_by_effort(self, rows: list[dict]) -> list[dict]:
        """Pure aggregation body of ``by_effort`` (see it for the group schema and ordering);
        split out so ``by_effort`` can memoise the self-read path (#220)."""
        groups: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            stage = row.get("stage", "unknown")
            raw_effort = row.get("effort")
            # ``is None`` (not falsy) so a present-but-empty '' effort surfaces as its own
            # anomalous group rather than silently folding into '(default)' (#180).
            effort = "(default)" if raw_effort is None else raw_effort
            model = row.get("model", "unknown")
            key = (stage, effort, model)
            g = groups.setdefault(
                key,
                {
                    "stage": stage,
                    "effort": effort,
                    "model": model,
                    "invocations": 0,
                    "cost_usd": 0.0,
                    "unmetered": 0,
                    "total_duration_s": 0.0,
                    "retries": 0,
                    "failures": 0,
                },
            )
            g["invocations"] += 1
            g["cost_usd"] = round(g["cost_usd"] + (row.get("cost_usd") or 0.0), 6)
            # ``is False`` (not falsy) so a row predating the flag counts as metered, matching
            # summary()/analysis(). An unmetered row still adds its 0.0 above, which is exactly
            # why the count has to travel with the group (#331).
            if row.get("metered") is False:
                g["unmetered"] += 1
            g["total_duration_s"] += row.get("duration_s") or 0.0
            if (row.get("attempt") or 0) > 0:
                g["retries"] += 1
            status = row.get("status", "")
            if status not in ("success", "skipped", "rate_limited"):
                g["failures"] += 1
        out: list[dict] = []
        for g in groups.values():
            n = g["invocations"]
            g["total_duration_s"] = round(g["total_duration_s"], 3)
            g["avg_duration_s"] = round(g["total_duration_s"] / n, 3) if n else 0.0
            g["retry_rate"] = round(g["retries"] / n, 4) if n else 0.0
            g["failure_rate"] = round(g["failures"] / n, 4) if n else 0.0
            out.append(g)
        out.sort(
            key=lambda g: (
                _STAGE_RANK.get(g["stage"], len(STAGE_ORDER)),
                g["stage"],
                g["effort"],
                g["model"],
            )
        )
        return out

    def analysis(self, rows: list[dict] | None = None) -> dict:
        """Rich cost report: per-stage + per-task breakdowns and the session-reuse win.

        The rebuild's thesis is that chaining the collapsed stages in ONE session
        reuses the prompt cache, so most input-side tokens come back as cheap
        ``cache_read`` (billed at ``cache_read_mult`` of input) instead of fresh input.
        This quantifies that: the win is what those reads saved vs. an uncached
        counterfactual, net of the ``cache_write`` premium paid to establish the cache.

        Accepts pre-read ``rows`` so a caller (engine.status) reads the JSONL once.
        Tolerant of malformed rows (``.get`` defaults) and of models absent from the
        price table — those still count toward spend but are excluded from the
        counterfactual (and named in ``unpriced_models``).

        ``unmetered_calls``/``total_invocations`` (#319, mirroring ``summary()``): a row
        whose usage was never recoverable still contributes its ``cost_usd: 0.0`` to
        ``total_cost_usd`` like any other row, so a caller (``render_cost_report``) needs
        the unmetered count to say the total is a floor, not a complete figure."""
        rows = self.rows() if rows is None else rows

        def _bump(d: dict, key: str, cost: float, ci: int, co: int, cr: int, cw: int) -> None:
            b = d.setdefault(key, {"invocations": 0, "input_tokens": 0, "output_tokens": 0,
                                   "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0})
            b["invocations"] += 1
            b["input_tokens"] += ci
            b["output_tokens"] += co
            b["cache_read_tokens"] += cr
            b["cache_write_tokens"] += cw
            b["cost_usd"] = round(b["cost_usd"] + cost, 6)

        by_stage: dict[str, dict] = {}
        by_task: dict[str, dict] = {}
        total_cost = 0.0
        total_invocations = 0
        unmetered_calls = 0
        fresh_input = output = cache_read = cache_write = 0
        cache_read_savings = 0.0  # money saved: reads billed at read_mult, not full input
        cache_write_premium = 0.0  # money spent: writes billed above plain input
        unpriced: set[str] = set()

        for row in rows:
            cost = row.get("cost_usd") or 0.0
            model = row.get("model", "unknown")
            total_invocations += 1
            # #319: mirror summary()'s unmetered count. An unmetered row contributes its 0.0
            # to the totals like any other, so without this the report's bottom line silently
            # absorbs calls of UNKNOWN cost as free — the exact confident-$0 defect #319
            # exists to remove, one artifact over.
            if row.get("metered") is False:
                unmetered_calls += 1
            ci = row.get("input_tokens", 0) or 0
            co = row.get("output_tokens", 0) or 0
            cr = row.get("cache_read_tokens", 0) or 0
            cw = row.get("cache_write_tokens", 0) or 0
            total_cost += cost
            fresh_input += ci
            output += co
            cache_read += cr
            cache_write += cw
            _bump(by_stage, row.get("stage", "unknown"), cost, ci, co, cr, cw)
            _bump(by_task, row.get("task_id", "unknown"), cost, ci, co, cr, cw)
            try:
                info = self.model_table.info(model)
            except KeyError:
                unpriced.add(model)
                continue
            in_price = info.input_per_mtok
            cache_read_savings += cr * in_price * (1.0 - info.cache_read_mult) / 1_000_000
            cache_write_premium += cw * in_price * (info.cache_write_mult - 1.0) / 1_000_000

        # uncached counterfactual: every input-side token billed once at full input
        # price (no read discount, no write premium) => actual + net_win.
        net_win = cache_read_savings - cache_write_premium
        uncached_cost = total_cost + net_win
        input_side = fresh_input + cache_read + cache_write
        return {
            "total_cost_usd": round(total_cost, 6),
            "total_invocations": total_invocations,
            "unmetered_calls": unmetered_calls,
            "by_stage": by_stage,
            "by_task": by_task,
            "session_reuse": {
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "fresh_input_tokens": fresh_input,
                "output_tokens": output,
                # share of input-side tokens served from cache (the reuse rate)
                "cache_hit_ratio": round(cache_read / input_side, 4) if input_side else 0.0,
                "cache_read_savings_usd": round(cache_read_savings, 6),
                "cache_write_premium_usd": round(cache_write_premium, 6),
                "net_win_usd": round(net_win, 6),
                "uncached_cost_usd": round(uncached_cost, 6),
                "win_pct": round(100.0 * net_win / uncached_cost, 2) if uncached_cost else 0.0,
                "unpriced_models": sorted(unpriced),
            },
        }
