"""Transport layer for in-process runners (target.md §4 Phase 4).

Separates I/O (actually invoking ``claude -p`` / ``codex exec``) from the success
policy (each runner decides what counts as success). A ``Transport`` takes a
WorkItem and returns a normalized ``RawResult``; the real transports shell out, and
tests inject a fake. This keeps the runners deterministic and unit-testable without
a live model.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as _JSONSchemaError

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage, WorkItem
from orchestrator.status_store import safe_task_dirname
from orchestrator.stream_probe import (
    stderr_filename,
    stream_filename,
    stream_relpath,
)


@dataclass
class RawResult:
    structured_output: dict | None
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_output: str | None = None
    exit_code: int = 0
    error: str | None = None
    invocation: str = ""
    # FULL, untruncated provider stderr (the ``error`` field above is a bounded excerpt
    # for the ledger). Kept whole so the stream-teeing wrapper can persist it verbatim as
    # the primary post-mortem evidence (#56). None on lanes/paths without a provider stream.
    raw_stderr: str | None = None
    # Paths (relative to the run root) the raw provider stdout/stderr were teed to under the
    # per-stage log dir — ``{"stream": ..., "stderr": ...}`` (or ``{"error": ...}`` if the
    # best-effort tee failed). Stamped by ``stream_teeing_transport`` (#56); None when no
    # teeing wrapper is installed or there was no provider stream to save.
    stream_files: dict | None = None
    # Provider session the call used/created (design pass §2); None if unsupported.
    session_ref: str | None = None
    # {"tag", "sha"} stamped by the checkpoint wrapper after a successful
    # git-affecting stage (design pass §3); None otherwise.
    checkpoint: dict | None = None
    # {"anchor", "count", "commits": [...]} stamped by the checkpoint wrapper when a
    # FAILED/TIMED-OUT attempt left commits past the anchor (#59); None otherwise.
    salvage: dict | None = None
    # How many corrective schema-retries the transport spent salvaging a malformed
    # structured output (#32). 0 = valid on the first call (the overwhelming case).
    # Audit-only metadata: surfaced on the cost-ledger row, never a correctness input.
    schema_retries: int = 0


Transport = Callable[[WorkItem], RawResult]

# Substrings that mark a transient rate-limit / overload (→ ResultStatus.RATE_LIMITED,
# which the engine answers by re-dispatching on a cheaper model). Case-insensitive.
# Kept narrow + API-specific on purpose: broad words like "capacity" or "quota" also
# appear in hard errors (disk capacity, disk quota) and would misroute a real failure
# around the retry/breaker accounting, so they are deliberately excluded.
_RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "ratelimit", "429", "too many requests",
    "overloaded", "usage limit", "rate-limited",
)


def is_rate_limited(raw: RawResult) -> bool:
    """True if a RawResult's error looks like a transient rate-limit / overload."""
    text = (raw.error or "").lower()
    return bool(text) and any(m in text for m in _RATE_LIMIT_MARKERS)


# Errors meaning "the session to resume no longer exists" (expired/gc'd/unknown) — and
# ONLY that. A lost session falls back to a fresh one inside the same dispatch (design
# pass §2: a session ref is routing metadata; correctness never depends on continuity).
# Any other resume error fails the dispatch normally.
_SESSION_LOST_MARKERS = (
    "no conversation found", "session not found", "unknown session", "invalid session",
)


# Appended to every headless `claude -p` call that carries a schema. `--json-schema` is
# best-effort (the CLI only guarantees session_id/stop_reason/envelope, not
# structured_output); this makes the JSON object the required final output so an agentic
# stage doesn't finish in prose. Composes with _json_object_from_text + _validate_shape.
_JSON_ONLY_POSTAMBLE = (
    "When your work for this stage is complete, your FINAL message must be ONLY a single "
    "JSON object that satisfies the required JSON schema — using the exact required "
    "property names, with no prose, no explanation, and no markdown code fences before or "
    "after it. Do the stage's actual work first (tools, edits, commands as needed), then "
    "emit that JSON object as the last thing you output."
)


def _session_lost(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return bool(text) and any(m in text for m in _SESSION_LOST_MARKERS)


def _json_object_from_text(text: str) -> dict | None:
    """Recover a JSON object a model printed as text (in the envelope's ``result``) when
    it answered the ``--json-schema`` prompt with prose instead of the structured-output
    tool. Extracts the first balanced ``{...}`` object, so it survives the common shapes a
    model actually emits — a ```json fenced block, a "Here is the result:" preamble, or a
    trailing "Let me know if…" sentence. Returns the object, or None if the text has no
    parseable JSON object (genuine prose stays a SCHEMA_VIOLATION). Shape is validated
    separately against the stage schema — this only finds the object, it doesn't vouch for it."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:  # matched the first object's close brace
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _validate_shape(obj: dict, schema_json: str) -> dict | None:
    """Return ``obj`` iff it satisfies the stage's JSON Schema, else None. Guards the
    recovery/tool paths where the CLI didn't (or can't) enforce shape, so a wrong-shape
    object becomes a SCHEMA_VIOLATION instead of a false SUCCESS. A malformed schema is not
    the model's fault — pass the object through rather than failing the dispatch on it."""
    try:
        validator = Draft202012Validator(json.loads(schema_json))
    except Exception:  # noqa: BLE001 - a malformed schema must not veto real work
        return obj
    try:
        validator.validate(obj)
    except _JSONSchemaError:
        return None
    return obj


# --- schema-validate-and-retry (#32) -----------------------------------------------------
# A headless model call that comes back exit-0 but with a wrong-shape structured output used
# to burn a whole stage attempt (full re-run at full cost). Instead we retry the SAME call a
# bounded number of times with a corrective follow-up that tells the model exactly what was
# wrong — cheap and targeted. The band-aid `--append-system-prompt` (`_JSON_ONLY_POSTAMBLE`)
# stays: it's a first-call preventive nudge that keeps most calls valid so this loop rarely
# engages; the loop is the guarantee layered behind it.

_MAX_SCHEMA_ERRORS = 6  # bound how many validation errors we echo back (a bad object can have many)
_SCHEMA_EXCERPT_CHARS = 4000  # bound the schema we quote back into the corrective prompt
_PREV_INVALID_CHARS = 1500  # bound the model's own prior invalid output when we can't resume


def _schema_errors(obj: dict | None, schema_json: str) -> list[str]:
    """Bounded ``path: message`` strings for why ``obj`` fails the stage schema, or ``[]``
    when it satisfies the schema (or the schema itself is malformed — a bad schema must never
    veto real work, mirroring ``_validate_shape``). Feeds the corrective retry prompt so the
    model is told precisely what to fix instead of re-guessing the whole contract."""
    try:
        validator = Draft202012Validator(json.loads(schema_json))
    except Exception:  # noqa: BLE001 - a malformed schema is not the model's fault
        return []
    out: list[str] = []
    for err in validator.iter_errors(obj):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        out.append(f"{path}: {err.message}"[:300])
        if len(out) >= _MAX_SCHEMA_ERRORS:
            break
    return out


def _corrective_prompt(
    errors: list[str], schema_json: str, prev_invalid: dict | None, *, continued: bool
) -> str:
    """The follow-up prompt for one schema retry: the specific validation errors, the schema
    (bounded excerpt), and an instruction to return ONLY corrected structured output. When we
    could NOT resume the model's session (``continued`` False) we also embed its own prior
    invalid output, since a fresh call has no memory of what it just returned."""
    lines = [
        "Your previous response did not satisfy the required JSON schema for this stage.",
        "",
        "Validation errors (JSON path: problem):",
        *(f"  - {e}" for e in errors),
        "",
        "Required JSON schema:",
        schema_json[:_SCHEMA_EXCERPT_CHARS]
        + ("\n…(schema truncated)" if len(schema_json) > _SCHEMA_EXCERPT_CHARS else ""),
    ]
    if not continued and prev_invalid is not None:
        blob = json.dumps(prev_invalid)
        lines += [
            "",
            "Your previous (invalid) output was:",
            blob[:_PREV_INVALID_CHARS] + ("…(truncated)" if len(blob) > _PREV_INVALID_CHARS else ""),
        ]
    lines += [
        "",
        "Return ONLY a single corrected JSON object that satisfies the schema exactly — using "
        "the exact required property names, with no prose, no explanation, and no markdown code "
        "fences before or after it.",
    ]
    return "\n".join(lines)


# --- raw provider stream retention (#56) -------------------------------------------------
# The old bash system kept each stage's FULL raw provider stream + full stderr on disk; the
# rebuild parsed the stream for usage then dropped it (truncating stderr to 500 chars), so a
# post-mortem of a weird model call lost its primary evidence. This wrapper tees the whole
# stdout stream and whole stderr to files alongside the per-stage JSON record, under the same
# ``stages/<task>/`` dir the StatusStore writes into. Best-effort: a tee failure NEVER breaks
# the dispatch (it is recorded on ``stream_files["error"]`` instead). Plain files, NOT gzip —
# the point is full, directly-greppable evidence; gzip would force a decompress step between a
# failure and its raw stream. Interactive/ENGINE lanes carry no provider stream, so a
# RawResult with neither stdout nor stderr is skipped cleanly (nothing to tee).


def _stream_dir(root: Path, task_id: str) -> Path:
    return root / "stages" / safe_task_dirname(task_id)


def _tee_streams(root: Path, work: WorkItem, raw: RawResult) -> dict | None:
    """Write raw stdout/stderr to ``stages/<task>/<stage>-attempt<N>.{stream.jsonl,stderr.log}``
    and return the saved paths RELATIVE to the run root (portable if the run dir is moved), or
    None when there is nothing to save. Named per (stage, attempt) so a retry doesn't clobber a
    prior attempt's evidence. Raises nothing the caller must handle — failures are caught in the
    wrapper."""
    if raw.raw_output is None and not raw.raw_stderr:
        return None  # no provider stream (deterministic/interactive lane) — skip cleanly
    d = _stream_dir(root, work.task_id)
    d.mkdir(parents=True, exist_ok=True)
    files: dict = {}
    if raw.raw_output is not None:
        p = d / stream_filename(work.stage.value, work.attempt)
        p.write_text(raw.raw_output, encoding="utf-8")
        files["stream"] = str(p.relative_to(root))
    if raw.raw_stderr:
        p = d / stderr_filename(work.stage.value, work.attempt)
        p.write_text(raw.raw_stderr, encoding="utf-8")
        files["stderr"] = str(p.relative_to(root))
    return files or None


def stream_teeing_transport(inner: Transport, run_log_root: str | Path | None) -> Transport:
    """Wrap a provider transport so each call's FULL raw stdout/stderr is teed to disk under
    the run's per-stage log dir (#56), and the saved paths are stamped onto the RawResult
    (``stream_files``) so a human can navigate from a recorded failure straight to the raw
    stream. Best-effort by contract: any teeing error is swallowed and noted in
    ``stream_files["error"]`` — retaining evidence must never break the model call. A None
    root (no run dir wired) is a no-op passthrough.

    A transport that already teed its stream in-flight (#66 — the real provider transports,
    which stream stdout to the SAME file as it arrives so it can be tailed live) returns a
    RawResult with ``stream_files`` already set; this wrapper then passes through untouched.
    So it only does the after-the-fact tee for transports that DON'T stream (injected/test
    transports that hand back a whole ``raw_output``)."""
    root = Path(run_log_root) if run_log_root is not None else None

    def run(work: WorkItem) -> RawResult:
        raw = inner(work)
        if root is None or raw.stream_files is not None:
            return raw  # no run dir, or the inner transport already streamed+stamped it live
        try:
            files = _tee_streams(root, work, raw)
        except Exception as exc:  # noqa: BLE001 - retaining evidence is never worth a failed call
            return replace(raw, stream_files={"error": f"stream tee failed: {exc}"[:300]})
        return replace(raw, stream_files=files) if files else raw

    return run


# --- in-flight stream teeing (#66) -------------------------------------------------------
# #56 retained the raw stream but wrote it AFTER the call returned, so an in-flight headless
# stage (10-30 min) could not be tailed. ``_run_teed`` runs the provider subprocess and tees
# its stdout to the stream file LINE-BY-LINE as it arrives (flushed), while still capturing the
# full stdout/stderr for parsing. The tee is best-effort: a tee-file write error drops teeing
# and keeps capturing — retaining/observing evidence must NEVER break the model call. Timeout
# is enforced with a watchdog that kills the process even when it emits no output, then re-raised
# as ``subprocess.TimeoutExpired`` so the callers' existing timeout handling is unchanged.


def _run_teed(
    argv: list[str], *, timeout: float | None, cwd: str | None, tee_path: Path
) -> tuple[int, str, str]:
    """Run ``argv`` (Popen), teeing stdout to ``tee_path`` as it streams in. Returns
    ``(returncode, full_stdout, full_stderr)``. Raises ``subprocess.TimeoutExpired`` on
    timeout (process killed), and ``FileNotFoundError`` if the binary is missing — matching
    ``subprocess.run`` so the transports' existing except-clauses apply unchanged."""
    tee = None
    try:  # best-effort: teeing must never break the call
        tee_path.parent.mkdir(parents=True, exist_ok=True)
        tee = open(tee_path, "w", encoding="utf-8")  # noqa: SIM115 - closed in finally
    except OSError:
        tee = None
    # Popen raises FileNotFoundError here for a missing binary (as subprocess.run does).
    proc = subprocess.Popen(  # noqa: S603
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd
    )
    stderr_box: dict[str, str] = {"data": ""}

    def _drain_stderr() -> None:
        # stderr is post-mortem evidence, not control flow — a read hiccup must not matter.
        with contextlib.suppress(Exception):
            stderr_box["data"] = proc.stderr.read() or ""  # type: ignore[union-attr]

    et = threading.Thread(target=_drain_stderr, daemon=True)
    et.start()
    killed = {"timeout": False}

    def _kill_on_timeout() -> None:
        killed["timeout"] = True
        proc.kill()

    watchdog = threading.Timer(timeout, _kill_on_timeout) if timeout else None
    if watchdog is not None:
        watchdog.start()
    parts: list[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            parts.append(line)
            if tee is not None:
                try:
                    tee.write(line)
                    tee.flush()
                except OSError:  # tee target went away — keep capturing, stop teeing
                    tee.close()
                    tee = None
    finally:
        proc.wait()
        if watchdog is not None:
            watchdog.cancel()
        et.join(timeout=1)
        if tee is not None:
            tee.close()
    if killed["timeout"]:
        raise subprocess.TimeoutExpired(argv, timeout or 0)
    return proc.returncode, "".join(parts), stderr_box["data"]


def _streaming_stream_files(
    root: Path, work: WorkItem, stdout: str, stderr: str
) -> dict:
    """Assemble the ``stream_files`` map for a call that streamed its stdout live (#66). The
    ``.stream.jsonl`` was written in-flight by ``_run_teed``; here we only write the stderr
    file (post-hoc — stderr isn't the live-tail surface) and return both paths RELATIVE to the
    run root. Both writes are best-effort; a stdout stream with no content is still recorded so
    a probe/tail finds the (empty) file the stage owns."""
    files: dict = {"stream": stream_relpath(work.task_id, work.stage.value, work.attempt)}
    if stderr:
        try:
            p = _stream_dir(root, work.task_id) / stderr_filename(work.stage.value, work.attempt)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(stderr, encoding="utf-8")
        except OSError:  # pragma: no cover - defensive; stderr is evidence, not control
            pass
        else:
            files["stderr"] = str(p.relative_to(root))
    return files


def to_stage_result(
    work: WorkItem,
    raw: RawResult,
    status: ResultStatus,
    *,
    mode: ExecutionMode,
    provider: Provider,
) -> StageResult:
    """Build the engine-facing StageResult from a runner's RawResult + verdict."""
    # StageResult.structured_output is dict|None; a non-dict payload (list/scalar)
    # is coerced to None here (the raw text is preserved in raw_output).
    structured = raw.structured_output if isinstance(raw.structured_output, dict) else None
    return StageResult(
        work_item_id=work.id,
        content_hash=work.content_hash,
        run_id=work.run_id,
        task_id=work.task_id,
        stage=work.stage,
        attempt=work.attempt,
        model=work.model,
        status=status,
        structured_output=structured,
        raw_output=raw.raw_output,
        error=raw.error,
        lane_used=LaneUsed(
            execution_mode=mode, provider=provider, invocation=raw.invocation or provider.value
        ),
        session_ref=raw.session_ref,
        checkpoint=raw.checkpoint,
        salvage=raw.salvage,
        stream_files=raw.stream_files,
        schema_retries=raw.schema_retries,
        token_usage=raw.usage,
        completed_at=datetime.now(UTC).isoformat(),
    )


def _usage_from(d: dict) -> TokenUsage:
    u = d.get("usage") or {}
    return TokenUsage(
        input=u.get("input_tokens", 0) or 0,
        output=u.get("output_tokens", 0) or 0,
        cache_read=u.get("cache_read_input_tokens", 0) or 0,
        cache_write=u.get("cache_creation_input_tokens", 0) or 0,
    )


def _codex_usage(events_stdout: str) -> TokenUsage:
    """Best-effort token usage from the codex JSONL event stream.

    Codex emits `--json` events; scan for the last object carrying a `usage`
    (directly or under `msg`). Returns zeros if none is found (so codex cost is
    captured when codex reports it, instead of always 0)."""
    usage = TokenUsage()
    for line in events_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = ev.get("usage") or (ev.get("msg") or {}).get("usage")
        if isinstance(u, dict):
            usage = _usage_from({"usage": u})
    return usage


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,  # noqa: S603, S607
                          text=True, timeout=60)


def _reset_worktree(cwd: str | None, ref: str) -> str | None:
    """Hard-reset a task worktree to a checkpoint ref before dispatch (design pass §3).
    Returns an error string (fails the dispatch) or None. DESTRUCTIVE — guarded:

    - refuses without an explicit cwd (never resets the process CWD), and
    - refuses unless ``<cwd>/.git`` is a FILE, i.e. a linked ``git worktree`` — in the
      main checkout ``.git`` is a directory, and wiping a checkout the human may be
      working in is exactly the accident this guard exists to prevent.
    """
    if not cwd:
        return "checkpoint reset requires an explicit worktree cwd"
    if not Path(cwd, ".git").is_file():
        return f"refusing checkpoint reset: {cwd} is not a linked git worktree"
    for args in (("reset", "--hard", ref), ("clean", "-fd")):
        proc = _git(cwd, *args)
        if proc.returncode != 0:
            # Do NOT run the stage over unknown/dirty state — fail the dispatch.
            return f"checkpoint reset failed (git {' '.join(args)}): {proc.stderr.strip()[:300]}"
    return None


# Cap on how many commit records a salvage report carries — bounded like the failure
# learning's failing-test list, so a runaway attempt can't balloon the task state.
_SALVAGE_COMMIT_CAP = 20


def _salvageable_commits(cwd: str | None, anchor: str) -> dict | None:
    """Report the COMMITTED work an attempt left past ``anchor`` (design §59) — a pure,
    non-destructive git read of ``anchor..HEAD``. Returns
    ``{"anchor", "count", "commits": [{"sha", "subject"}, ...]}`` (commit list capped),
    or None when the tree didn't advance past the anchor (nothing to salvage) or any git
    step fails (fail-OPEN: a salvage we can't read is simply a reset, never a broken
    dispatch). Uncommitted/dirty changes are DELIBERATELY ignored — salvage is for vetted,
    committed work only; scraps in the index/worktree are reset with the retry."""
    if not cwd:
        return None
    try:
        log = _git(cwd, "log", "--reverse", "--format=%H%x1f%s", f"{anchor}..HEAD")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    if log.returncode != 0:
        return None
    commits: list[dict] = []
    for line in log.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, subject = line.partition("\x1f")
        commits.append({"sha": sha, "subject": subject.strip()[:200]})
    if not commits:
        return None
    return {
        "anchor": anchor,
        "count": len(commits),
        "commits": commits[:_SALVAGE_COMMIT_CAP],
    }


def _tag_head(cwd: str, tag: str) -> dict | None:
    """Tag HEAD (``-f``: a crash between tag and record re-runs the stage and
    overwrites the same attempt's tag). Returns {"tag", "sha"} or None — fail-OPEN:
    a missing checkpoint only means no reset anchor later, never a failed stage."""
    try:
        if _git(cwd, "tag", "-f", tag).returncode != 0:
            return None
        head = _git(cwd, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    return {"tag": tag, "sha": head.stdout.strip()} if head.returncode == 0 else None


def checkpointing_transport(inner: Transport) -> Transport:
    """Wrap a transport with the stage-commit checkpoint protocol (design pass §3).

    All git I/O lives HERE — models are unreliable at bookkeeping and the engine must
    stay pure (it only names tags / picks reset anchors, as WorkItem fields). Before
    dispatch: reset the worktree to ``reset_to`` if set. After a successful raw result
    of a checkpoint stage: tag HEAD as ``checkpoint_tag`` and stamp {tag, sha} into
    the RawResult. Intake has no cwd yet, so the tag lands in the worktree its own
    output names.
    """

    def run(work: WorkItem) -> RawResult:
        if work.reset_to:
            err = _reset_worktree(work.cwd, work.reset_to)
            if err:
                return RawResult(None, exit_code=1, error=err,
                                 invocation=f"checkpoint reset {work.reset_to}")
        raw = inner(work)
        if work.checkpoint_tag and raw.exit_code == 0 and not raw.error:
            out = raw.structured_output if isinstance(raw.structured_output, dict) else {}
            tag_dir = work.cwd or out.get("worktree")
            if isinstance(tag_dir, str) and tag_dir:
                raw.checkpoint = _tag_head(tag_dir, work.checkpoint_tag)
        elif work.salvage_anchor and (raw.exit_code != 0 or raw.error):
            # A failed/timed-out attempt may have COMMITTED real work before it died.
            # Report those commits so the engine can KEEP them for the retry (#59) rather
            # than resetting them away. Git I/O stays HERE with the checkpoint reset it
            # mirrors; the engine only reads the report and decides by failure kind.
            raw.salvage = _salvageable_commits(work.cwd, work.salvage_anchor)
        return raw

    return run


def _envelope_to_raw(
    data: dict, *, stdout: str, stderr: str, invocation: str, stream_files: dict | None
) -> RawResult:
    """Build a RawResult from a claude result envelope (the whole ``--output-format json``
    object, or the ``type=="result"`` event of the ``stream-json`` stream — same field
    names). Shared so both output formats recover structured output identically."""
    # Presence check, not truthiness: a valid-but-empty structured_output ({}) must not
    # fall through to the prose `result`.
    structured = data["structured_output"] if "structured_output" in data else data.get("result")
    if isinstance(structured, str):
        # The model answered in prose/fenced text rather than the structured-output tool —
        # recover the JSON object it printed (validated later by the schema-retry loop).
        structured = _json_object_from_text(structured)
    session_id = data.get("session_id")
    return RawResult(structured, _usage_from(data), raw_output=stdout, raw_stderr=stderr,
                     exit_code=0, invocation=invocation, stream_files=stream_files,
                     session_ref=session_id if isinstance(session_id, str) else None)


def _last_result_event(stdout: str) -> dict | None:
    """The final ``{"type":"result", ...}`` event from a claude ``stream-json`` stdout — the
    envelope carrying the model's answer/usage/session. ``None`` if the stream ended without
    one (a crashed/killed call), which the caller treats as a transport error."""
    result: dict | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            result = ev
    return result


def claude_cli_transport(
    schema_json_for: Callable[[str], str | None] | None = None,
    *,
    max_schema_retries: int = 2,
    run_log_root: str | Path | None = None,
) -> Transport:
    """Real headless×claude transport: shells ``claude -p``.

    ``run_log_root`` (#66): when a run dir is wired, the call streams
    ``--output-format stream-json --verbose`` and tees stdout to the stage's
    ``.stream.jsonl`` LINE-BY-LINE as it arrives (so an in-flight stage can be tailed live),
    then parses the final ``result`` event for the answer. Without a run dir (tests / no log
    dir) it keeps the single-shot ``--output-format json`` path. Both paths recover the
    structured output identically via ``_envelope_to_raw``, so the schema-retry loop below is
    unchanged.

    ``schema_json_for`` returns the stage's JSON Schema **inline** (the CLI's
    ``--json-schema`` takes the schema JSON itself, not a file path) so the reply is
    forced into the stage contract and comes back on the envelope's ``structured_output``.

    Session continuity (design pass §2): a WorkItem carrying ``session_ref`` resumes
    that conversation (``--resume``) so later stages of a task are nearly all
    cache-read; the JSON payload's ``session_id`` is reported back so the engine can
    chain the next stage. A lost/expired session cold-starts a fresh one inside the
    same dispatch — prompts are self-contained, so continuity is never load-bearing.

    Schema-validate-and-retry (#32): when a call returns exit-0 but the structured output
    fails the stage schema, retry up to ``max_schema_retries`` (default 2) with a corrective
    follow-up naming the exact validation errors — a cheap, targeted fix instead of burning a
    whole stage attempt. The retry PREFERS resuming the same session (the model keeps its own
    context) and falls back to a fresh call that embeds its prior invalid output. On exhaustion
    the stage fails as before (``structured_output`` None → SCHEMA_VIOLATION), now with the
    validation errors in ``raw_output`` so the retry-with-learnings starts from specifics. The
    count is recorded on ``RawResult.schema_retries`` for the ledger.
    """

    root = Path(run_log_root) if run_log_root is not None else None

    def _call(work: WorkItem, session_ref: str | None) -> RawResult:
        # #66: with a run dir, stream `stream-json` and tee stdout as it arrives so the stage
        # is tailable live; otherwise the single-shot `json` path. `--json-schema` +
        # `_JSON_ONLY_POSTAMBLE` are BEST-EFFORT on `claude -p` (after multi-turn agentic work
        # the model often ends in PROSE with no `structured_output` — the headless #25 failure),
        # so both paths recover the object from `result` text and the schema-retry loop below
        # is the real shape guarantee.
        streaming = root is not None
        fmt = ["--output-format", "stream-json", "--verbose"] if streaming \
            else ["--output-format", "json"]
        argv = ["claude", "-p", work.prompt, "--model", work.model,
                "--dangerously-skip-permissions", *fmt]
        if work.agent:
            argv += ["--agent", work.agent]
        if schema_json_for and (schema := schema_json_for(work.schema_ref)):
            argv += ["--json-schema", schema, "--append-system-prompt", _JSON_ONLY_POSTAMBLE]
        if session_ref:
            argv += ["--resume", session_ref]
        invocation = (f"claude -p --model {work.model}"
                      + (f" --agent {work.agent}" if work.agent else "")
                      + (f" --resume {session_ref}" if session_ref else ""))

        stream_files: dict | None = None
        try:
            if streaming:
                tee_path = _stream_dir(root, work.task_id) / stream_filename(
                    work.stage.value, work.attempt
                )
                returncode, stdout, stderr = _run_teed(
                    argv, timeout=work.timeout_s, cwd=work.cwd, tee_path=tee_path
                )
                stream_files = _streaming_stream_files(root, work, stdout, stderr)
            else:
                proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                      timeout=work.timeout_s, cwd=work.cwd)
                returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as exc:  # pragma: no cover - env dependent
            return RawResult(None, exit_code=127, error=str(exc), invocation=invocation)
        except subprocess.TimeoutExpired:
            # A hung CLI must not hang the scheduler — fail the dispatch on timeout.
            return RawResult(None, exit_code=124,
                             error=f"timed out after {work.timeout_s}s", invocation=invocation)
        if returncode != 0:
            return RawResult(None, exit_code=returncode, error=stderr.strip()[:500],
                             raw_output=stdout, raw_stderr=stderr,
                             invocation=invocation, stream_files=stream_files)
        if streaming:
            data = _last_result_event(stdout)
            if data is None:
                # Exit 0 but no result event (partial/killed stream): fail the dispatch.
                return RawResult(None, raw_output=stdout, raw_stderr=stderr, exit_code=0,
                                 error="no result event in stream-json output",
                                 invocation=invocation, stream_files=stream_files)
        else:
            try:
                data = json.loads(stdout) if stdout.strip() else {}
            except json.JSONDecodeError as exc:
                # Exit 0 but non-JSON stdout (banner/notice): fail the dispatch, never
                # let the exception escape (every dispatch must yield a StageResult).
                return RawResult(None, raw_output=stdout, raw_stderr=stderr, exit_code=0,
                                 error=f"non-JSON output: {exc}", invocation=invocation)
        # NB: the shape-gate is deliberately NOT applied here — the `run` loop below
        # validates (and, on failure, retries with a corrective prompt). Nulling out an
        # invalid object here would throw away exactly what the retry needs to quote back.
        return _envelope_to_raw(data, stdout=stdout, stderr=stderr,
                                invocation=invocation, stream_files=stream_files)

    def run(work: WorkItem) -> RawResult:
        raw = _call(work, work.session_ref)
        # Fallback-to-fresh on a dead session ONLY (one retry, same dispatch). Any
        # other resume failure returns as-is and fails the dispatch normally.
        if work.session_ref and raw.exit_code != 0 and _session_lost(raw.error):
            raw = _call(work, None)

        # Without a schema in hand there is nothing to validate against (the CLI's
        # best-effort --json-schema wasn't sent) — keep the as-was behavior.
        schema_json = schema_json_for(work.schema_ref) if schema_json_for else None
        if not schema_json:
            return raw

        retries = 0
        while True:
            # A transport-level error (timeout / non-JSON / non-zero exit) is not a schema
            # problem — never spend a schema retry on it.
            if raw.exit_code != 0 or raw.error:
                return raw
            errors = _schema_errors(raw.structured_output, schema_json)
            if not errors:  # valid (or unvalidatable schema) — done
                return raw
            if retries >= max_schema_retries:
                # Honest failure, as before: no structured output → SCHEMA_VIOLATION. Carry the
                # validation errors in raw_output so the failure-learning tail is specific
                # (the engine surfaces raw_output's tail into the next attempt's learnings).
                summary = (f"[schema-retry] validation still failing after {retries} corrective "
                           f"retr{'y' if retries == 1 else 'ies'}:\n" + "\n".join(errors))
                raw = replace(
                    raw, structured_output=None, schema_retries=retries,
                    raw_output=((raw.raw_output or "") + "\n\n" + summary),
                )
                return raw
            retries += 1
            # PREFER continuing the same session (the model keeps its own context); fall back
            # to a fresh call embedding the prior invalid output when there's no session to resume.
            resume_ref = raw.session_ref
            corrective = _corrective_prompt(
                errors, schema_json, raw.structured_output, continued=bool(resume_ref)
            )
            raw = _call(work.model_copy(update={"prompt": corrective}), resume_ref)
            raw = replace(raw, schema_retries=retries)

    return run


def codex_cli_transport(*, run_log_root: str | Path | None = None) -> Transport:
    """Real codex transport: ``codex exec --json --output-last-message``.

    ``run_log_root`` (#66): codex ``--json`` already emits its events as a JSONL stream, so
    when a run dir is wired the stdout is teed to the stage's ``.stream.jsonl`` LINE-BY-LINE as
    it arrives (tailable live), same as the claude path. The result parsing is unchanged — it
    still reads ``--output-last-message`` and scans the full stdout for usage."""
    root = Path(run_log_root) if run_log_root is not None else None

    def run(work: WorkItem) -> RawResult:
        # A linked worktree's real .git lives in the main checkout — the codex sandbox
        # must be granted that dir too or commits from the worktree fail (the reference
        # system's --add-dir <git-common-dir>). Best-effort: no cwd / not a repo / any
        # git hiccup just means no extra grant.
        add_dir: list[str] = []
        if work.cwd:
            try:
                gcd = _git(work.cwd, "rev-parse", "--git-common-dir")
                common = gcd.stdout.strip()
                if gcd.returncode == 0 and common:
                    add_dir = ["--add-dir", str((Path(work.cwd) / common).resolve())]
            except Exception:  # noqa: BLE001 - the grant is an optimization, never a failure
                add_dir = []
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "last.json"
            argv = ["codex", "exec", "-m", work.model, "--full-auto", "--skip-git-repo-check",
                    *add_dir, "--json", "--output-last-message", str(last), work.prompt]
            invocation = f"codex exec --json (model {work.model})"
            stream_files: dict | None = None
            try:
                if root is not None:
                    tee_path = _stream_dir(root, work.task_id) / stream_filename(
                        work.stage.value, work.attempt
                    )
                    returncode, stdout, stderr = _run_teed(
                        argv, timeout=work.timeout_s, cwd=work.cwd, tee_path=tee_path
                    )
                    stream_files = _streaming_stream_files(root, work, stdout, stderr)
                else:
                    proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                          timeout=work.timeout_s, cwd=work.cwd)
                    returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            except FileNotFoundError as exc:  # pragma: no cover - env dependent
                return RawResult(None, exit_code=127, error=str(exc), invocation=invocation)
            except subprocess.TimeoutExpired:
                return RawResult(None, exit_code=124,
                                 error=f"timed out after {work.timeout_s}s", invocation=invocation)
            structured = None
            if last.exists():
                txt = last.read_text().strip()
                try:
                    structured = json.loads(txt)
                except json.JSONDecodeError:
                    structured = None
            return RawResult(structured, usage=_codex_usage(stdout), raw_output=stdout,
                             raw_stderr=stderr, exit_code=returncode, stream_files=stream_files,
                             error=(stderr.strip()[:500] or None) if returncode else None,
                             invocation=invocation)

    return run
