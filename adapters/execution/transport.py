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
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as _JSONSchemaError

from orchestrator.gitcmd import run_git
from orchestrator.schemas.enums import (
    ExecutionMode,
    PermissionPosture,
    Provider,
    ResultStatus,
)
from orchestrator.schemas.work import LaneUsed, StageResult, SubCall, TokenUsage, WorkItem
from orchestrator.status_store import safe_task_dirname
from orchestrator.stream_probe import (
    claude_final_text,
    codex_final_text,
    stderr_filename,
    stream_filename,
    stream_relpath,
    stream_tail_note,
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
    # teeing wrapper is installed or there was no provider stream to save. When a dispatch spent
    # schema-retry sub-calls (#70), ``stream``/``stderr`` are the FINAL call's files and
    # ``retries: [{stream, stderr}, ...]`` (oldest first) carries each superseded sub-call's
    # files so the whole retry chain's evidence survives.
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
    # The stage persona injected into the codex worktree's AGENTS.md for this dispatch
    # (#74 codex persona parity) — ``{"agent", "path"}`` (or ``{"agent", "error"}`` when the
    # best-effort injection failed). Audit-only metadata that rides RawResult -> StageResult ->
    # the stage log, like ``stream_files``; it never feeds a verdict or a transition. None on
    # the claude lane (persona arrives via ``--agent``) and when no agent resolved.
    persona_injected: dict | None = None
    # Did ``usage`` come from a report the PROVIDER actually returned (#319)? A FAILED call
    # keeps whatever usage its terminal event carried — a connection drop 12 minutes into a
    # high-effort Opus stage really did spend that money — so the zeros of a call that died
    # BEFORE any terminal event must be distinguishable from a measured zero. False makes the
    # ledger row unpriced/unmetered (an honest unknown) instead of a confident $0.0000.
    usage_recovered: bool = True


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

# The provider CLI's OWN limit notice does NOT arrive on stderr: claude prints it in the
# terminal ``result`` envelope under ``is_error: true``, so it lands in ``raw_output`` while
# ``error`` (built from stderr at the transport's non-zero-exit branch) is empty. Scanning
# only ``error`` therefore missed it entirely — a live "You've hit your session limit ·
# resets 3:50pm" classified FAILURE, burned both TEST attempts 2.3s apart with no backoff,
# and failed the task, while the engine's cooldown path (``not_before`` /
# ``max_rate_limit_waits``) never engaged because it keys on ResultStatus.RATE_LIMITED.
#
# These are matched against ``raw_output``, which on a failing stage ALSO carries task
# content (a test log, the model's final message), so unlike ``_RATE_LIMIT_MARKERS`` they
# are anchored to the CLI's own first-person phrasing. A bare "session limit" or "429" is
# deliberately excluded here: a task whose tests exercise a rate limiter would otherwise be
# reclassified out of the retry/breaker accounting, which is the exact failure mode the
# narrowness of ``_RATE_LIMIT_MARKERS`` was already guarding against. Case-insensitive.
_PROVIDER_LIMIT_NOTICE_MARKERS = (
    "hit your session limit", "hit your usage limit",
    "session limit reached", "usage limit reached",
)


def is_rate_limited(raw: RawResult) -> bool:
    """True if a RawResult looks like a transient rate-limit / overload.

    Two channels, deliberately asymmetric. The broad ``_RATE_LIMIT_MARKERS`` are matched
    against ``error`` only (CLI stderr — never a task's own output). The narrow
    ``_PROVIDER_LIMIT_NOTICE_MARKERS`` are additionally matched against ``raw_output``,
    where the claude CLI actually reports a spent session/usage limit.
    """
    text = (raw.error or "").lower()
    if text and any(m in text for m in _RATE_LIMIT_MARKERS):
        return True
    notice = f"{text}\n{(raw.raw_output or '').lower()}"
    return any(m in notice for m in _PROVIDER_LIMIT_NOTICE_MARKERS)


# Substrings marking the PROVIDER itself being unavailable — an auth/login failure, NOT a
# task failure (→ ResultStatus.PROVIDER_UNAVAILABLE, which the engine may answer by falling
# through to claude, #7). A missing CLI binary is caught separately by exit code 127
# (FileNotFoundError, mapped by the transports). Kept narrow + auth-specific on purpose:
# these markers appear in the CLI's own stderr, never in a task's test output, so they can't
# reclassify a genuine failure. Case-insensitive.
#
# The trailing block is model-availability / plan-mismatch phrasing (#80): a provider that
# refuses the CONFIGURED model outright — e.g. codex's 400 "The 'gpt-5-codex' model is not
# supported when using Codex with a ChatGPT account". Like the auth markers, this is a
# provider/config-level refusal the task's content can't influence, so falling through to
# claude is exactly right; it's kept narrow to the "model … not supported" wording (not a bare
# "invalid_request_error", which could ride a genuine bad request) so a real failure is never
# reclassified. The "is not supported when using {codex,a chatgpt}" variants (#87) are pinned
# to the codex/ChatGPT-plan phrasing (not a bare "is not supported when using X") so a generic
# unsupported-feature message can never be mistaken for a provider-plan refusal.
_PROVIDER_UNAVAILABLE_MARKERS = (
    "not logged in", "not authenticated", "unauthorized", "401 unauthorized",
    "authentication failed", "authentication error", "invalid api key", "missing api key",
    "no api key", "expired token", "token expired", "please log in", "please login",
    "run `codex login`", "codex login", "openai_api_key",
    "model is not supported",
    "is not supported when using codex", "is not supported when using a chatgpt",
)


def is_provider_unavailable(raw: RawResult) -> bool:
    """True if a RawResult signals the PROVIDER is out (CLI binary missing → exit 127, an
    auth/login failure, or a configured-model-not-available refusal in the error) rather than
    the task itself failing. The engine treats this as ``PROVIDER_UNAVAILABLE`` and — on an
    opted-in run — cross-provider-falls-through to claude (#7). Deliberately narrow so a real
    task failure is never reclassified."""
    if raw.exit_code == 127:  # FileNotFoundError: the CLI binary isn't installed
        return True
    text = (raw.error or "").lower()
    return bool(text) and any(m in text for m in _PROVIDER_UNAVAILABLE_MARKERS)


# Errors meaning "the session to resume no longer exists" (expired/gc'd/unknown) — and
# ONLY that. A lost session falls back to a fresh one inside the same dispatch (design
# pass §2: a session ref is routing metadata; correctness never depends on continuity).
# Any other resume error fails the dispatch normally. Covers both providers' phrasings:
# claude (`--resume`) and codex (`codex exec resume` → "no rollout found for thread id …",
# "thread/resume failed").
_SESSION_LOST_MARKERS = (
    "no conversation found", "session not found", "unknown session", "invalid session",
    "no rollout found", "thread/resume failed", "thread not found",
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


def _with_retry_chain(raw: RawResult, chain: list[dict]) -> RawResult:
    """Attach the superseded schema-retry sub-calls' stream-file pairs to the FINAL result's
    ``stream_files`` as ``retries: [{stream, stderr}, ...]`` (oldest first), keeping the
    top-level ``stream``/``stderr`` = the final call's files for backward compatibility (#70).
    A no-op when nothing was teed (the no-run-dir path), so ``stream_files`` stays None/flat
    there."""
    if not chain:
        return raw
    files = dict(raw.stream_files) if raw.stream_files else {}
    files["retries"] = chain
    return replace(raw, stream_files=files)


def _schema_retry_loop(
    raw: RawResult,
    work: WorkItem,
    schema_json: str,
    max_schema_retries: int,
    call: Callable[[WorkItem, str | None, int], RawResult],
) -> RawResult:
    """The provider-agnostic schema-validate-and-retry loop (#32). Shared verbatim by the
    claude and codex transports so the corrective-prompt shape, retry budget, and
    ``schema_retries`` accounting are identical on both lanes (#9/#21 codex parity).

    ``call(work, session_ref, retry)`` issues ONE provider call — resuming ``session_ref`` when
    the provider/CLI supports it, else a fresh call — teeing its stream to the ``retry``-indexed
    file (0 = the first call's bare name, K>=1 = a ``.retry<K>`` sub-call file, #70). On each
    invalid-but-exit-0 result the loop re-dispatches with a corrective follow-up (the exact
    validation errors + schema), PREFERRING to resume the same session so the model keeps its own
    context; when there is no session to resume it falls back to a fresh call that embeds the
    model's own prior invalid output. A transport-level error (timeout / non-JSON / non-zero exit)
    is never spent on a schema retry. On exhaustion the result is failed honestly
    (``structured_output`` None → SCHEMA_VIOLATION), with the validation errors appended to
    ``raw_output`` so the failure-learning tail is specific. ``schema_retries`` rides the
    RawResult onto the ledger row, and each superseded sub-call's stream files ride on
    ``stream_files["retries"]`` so a post-mortem keeps the whole chain's evidence (#70)."""
    retries = 0
    chain: list[dict] = []  # superseded sub-calls' stream-file pairs, oldest first (#70)
    while True:
        if raw.exit_code != 0 or raw.error:
            return _with_retry_chain(raw, chain)
        errors = _schema_errors(raw.structured_output, schema_json)
        if not errors:  # valid (or unvalidatable schema) — done
            return _with_retry_chain(raw, chain)
        if retries >= max_schema_retries:
            summary = (f"[schema-retry] validation still failing after {retries} corrective "
                       f"retr{'y' if retries == 1 else 'ies'}:\n" + "\n".join(errors))
            return _with_retry_chain(replace(
                raw, structured_output=None, schema_retries=retries,
                raw_output=((raw.raw_output or "") + "\n\n" + summary),
            ), chain)
        if raw.stream_files:  # this sub-call is about to be superseded — keep its evidence
            chain.append(raw.stream_files)
        retries += 1
        resume_ref = raw.session_ref
        corrective = _corrective_prompt(
            errors, schema_json, raw.structured_output, continued=bool(resume_ref)
        )
        raw = call(work.model_copy(update={"prompt": corrective}), resume_ref, retries)
        raw = replace(raw, schema_retries=retries)


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
        p = d / stream_filename(work.stage.value, work.attempt, phase=work.phase)
        p.write_text(raw.raw_output, encoding="utf-8")
        files["stream"] = str(p.relative_to(root))
    if raw.raw_stderr:
        p = d / stderr_filename(work.stage.value, work.attempt, phase=work.phase)
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
    argv: list[str], *, timeout: float | None, cwd: str | None, tee_path: Path,
    env: dict[str, str] | None = None,
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
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, env=env
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
        # #319: carry the partial stdout/stderr already buffered onto the exception, the same
        # way `subprocess.run` does. A timed-out call may have printed a terminal usage report
        # before the watchdog killed it, and the transports recover spend from it — dropping
        # the buffer here would make every timeout look free.
        raise subprocess.TimeoutExpired(
            argv, timeout or 0, output="".join(parts), stderr=stderr_box["data"]
        )
    return proc.returncode, "".join(parts), stderr_box["data"]


def _streaming_stream_files(
    root: Path, work: WorkItem, stdout: str, stderr: str, retry: int = 0
) -> dict:
    """Assemble the ``stream_files`` map for a call that streamed its stdout live (#66). The
    ``.stream.jsonl`` was written in-flight by ``_run_teed``; here we only write the stderr
    file (post-hoc — stderr isn't the live-tail surface) and return both paths RELATIVE to the
    run root. Both writes are best-effort; a stdout stream with no content is still recorded so
    a probe/tail finds the (empty) file the stage owns. ``retry`` (>=1) selects the schema-retry
    sub-call's ``.retry<K>`` name so each corrective sub-call's stream is its own file (#70);
    ``work.phase`` (#73) inserts the review-panel sub-call's own segment before it."""
    files: dict = {
        "stream": stream_relpath(
            work.task_id, work.stage.value, work.attempt, retry, phase=work.phase
        )
    }
    if stderr:
        try:
            p = _stream_dir(root, work.task_id) / stderr_filename(
                work.stage.value, work.attempt, retry, phase=work.phase
            )
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(stderr, encoding="utf-8")
        except OSError:  # pragma: no cover - defensive; stderr is evidence, not control
            pass
        else:
            files["stderr"] = str(p.relative_to(root))
    return files


def classify_raw(raw: RawResult) -> ResultStatus:
    """The success policy every claude-lane dispatch (single call or review-panel sub-call)
    classifies with — extracted from ``HeadlessClaudeRunner.dispatch`` so the panel's
    short-circuit reports the SAME status the equivalent single dispatch would (#73): a
    timeout is a TIMEOUT, a rate-limit is RATE_LIMITED (the engine re-dispatches cheaper),
    any other transport error is a FAILURE, and an exit-0 call with no structured output
    (the schema-retry loop exhausted) is a SCHEMA_VIOLATION."""
    if raw.exit_code != 0 or raw.error:
        if raw.exit_code == 124:
            return ResultStatus.TIMEOUT
        if is_rate_limited(raw):
            return ResultStatus.RATE_LIMITED
        return ResultStatus.FAILURE
    if raw.structured_output is None:
        return ResultStatus.SCHEMA_VIOLATION
    return ResultStatus.SUCCESS


def to_stage_result(
    work: WorkItem,
    raw: RawResult,
    status: ResultStatus,
    *,
    mode: ExecutionMode,
    provider: Provider,
    sub_results: dict | None = None,
    sub_calls: tuple[SubCall, ...] | None = None,
) -> StageResult:
    """Build the engine-facing StageResult from a runner's RawResult + verdict.

    ``sub_results``/``sub_calls`` (#73) are populated only by a plan-bearing dispatch that
    fanned out below the seam: the raw, unfolded panel output the engine's deterministic
    synthesis consumes, and one ``SubCall`` per model call inside the dispatch so the ledger
    can write a row each. Both default to None, so every single-call dispatch builds exactly
    the pre-#73 StageResult."""
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
        effort=work.effort,  # #96: echoed for the ledger row / stage events (audit)
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
        persona_injected=raw.persona_injected,
        sub_results=sub_results,
        sub_calls=sub_calls,
        token_usage=raw.usage,
        usage_recovered=raw.usage_recovered,
        completed_at=datetime.now(UTC).isoformat(),
    )


def _usage_from(d: dict) -> TokenUsage:
    """Normalize a provider usage block to the engine's DISJOINT convention: ``input`` is
    fresh input only, with cache reads/writes counted separately (that is what
    ``ModelTable.cost_usd`` prices, charging reads at ``cache_read_mult``).

    The two providers report opposite things under similar names (#350):

    - claude's ``cache_read_input_tokens`` is DISJOINT from ``input_tokens`` — pass through.
    - codex's ``cached_input_tokens`` is a SUBSET of ``input_tokens`` — subtract it, or the
      cached tokens get billed at the full input rate AND again at the cache-read rate. At
      the ~95% hit rates the headless codex lane actually gets, that inflated a row ~7x.
    """
    u = d.get("usage") or {}
    reported_input = u.get("input_tokens", 0) or 0
    claude_cache_read = u.get("cache_read_input_tokens") or 0
    codex_cache_read = u.get("cached_input_tokens") or 0
    if claude_cache_read:
        cache_read, fresh_input = claude_cache_read, reported_input
    else:
        # max(): a provider that ever reports cached > input must not yield negative fresh
        # input, which would price as a credit.
        cache_read, fresh_input = codex_cache_read, max(0, reported_input - codex_cache_read)
    return TokenUsage(
        input=fresh_input,
        output=u.get("output_tokens", 0) or 0,
        cache_read=cache_read,
        cache_write=u.get("cache_creation_input_tokens", 0) or 0,
    )


def _has_tokens(usage: TokenUsage) -> bool:
    """Did a usage report actually land? (#319) Used where the only evidence a call reported
    its spend is the numbers themselves — a lane like codex whose partial event stream has no
    single terminal envelope to point at. All-zero counts a report as MISSING, which errs
    toward 'unknown' over a confident $0."""
    return (usage.input + usage.output + usage.cache_read + usage.cache_write) > 0


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


def _codex_session_id(events_stdout: str) -> str | None:
    """The codex session/thread id from the ``codex exec --json`` event stream — the resume
    handle for session continuity (#9). The installed CLI (verified against real output) emits
    it as ``thread_id`` on an early ``{"type":"thread.started", ...}`` event; other/older
    shapes carry it as ``session_id``/``conversation_id`` (top-level or under ``msg``). Returns
    the FIRST id seen (the session's own id, before any nested tool-call ids), or None when the
    stream has none — in which case the next codex dispatch cold-starts (continuity is routing
    metadata; correctness never depends on it)."""
    for line in events_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        raw_msg = ev.get("msg")
        msg = raw_msg if isinstance(raw_msg, dict) else {}
        for key in ("thread_id", "session_id", "conversation_id"):
            v = ev.get(key) or msg.get(key)
            if isinstance(v, str) and v:
                return v
    return None


# Object-schema children to recurse into when strict-transforming for codex. Covers the
# shapes the stage schemas actually use (properties/items/combinators/$defs).
_SCHEMA_CHILD_KEYS = ("properties", "$defs", "definitions")
_SCHEMA_LIST_KEYS = ("anyOf", "allOf", "oneOf", "prefixItems")


def _codex_strict_schema(schema_json: str) -> str:
    """A strict-transformed COPY of a stage schema for codex's ``--output-schema``.

    OpenAI's structured-output validator requires ``additionalProperties: false`` on every
    object node (live 400 ``invalid_json_schema``: "'additionalProperties' is required to be
    supplied and to be false", run live-20260704-2 #68). Recursively stamps it onto every
    object-shaped node. Only the codex NUDGE sees this copy — engine-side validation and the
    schema-retry loop keep the original, so task semantics are unchanged. Unparseable input
    is returned as-is (the call then degrades via ``_codex_schema_rejected``)."""
    try:
        root = json.loads(schema_json)
    except json.JSONDecodeError:
        return schema_json

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
        for key in _SCHEMA_CHILD_KEYS:
            if isinstance(node.get(key), dict):
                for child in node[key].values():
                    walk(child)
        for key in _SCHEMA_LIST_KEYS:
            if isinstance(node.get(key), list):
                walk(node[key])
        if isinstance(node.get("items"), dict | list):
            walk(node["items"])

    walk(root)
    return json.dumps(root)


# Markers of codex rejecting the `--output-schema` FILE itself (a 400 before any work runs) —
# our nudge's problem, never the task's. Appears in the `--json` event stream (stdout), not
# stderr. Deliberately narrow: `invalid_json_schema` is the API's error code for exactly this.
_SCHEMA_REJECTED_MARKERS = ("invalid_json_schema", "invalid schema for response_format")


def _codex_schema_rejected(raw: RawResult) -> bool:
    """True when a codex call failed because the strict validator refused our schema nudge."""
    if raw.exit_code == 0:
        return False
    text = f"{raw.raw_output or ''} {raw.error or ''}".lower()
    return any(m in text for m in _SCHEMA_REJECTED_MARKERS)


def _unwrap_codex_error_message(message: str) -> str:
    """Codex double-encodes its 400 refusal: the event's ``message`` is ITSELF a JSON string
    encoding ``{"type","status","error":{"type","message"}}``. Return the clean inner
    ``error.message`` when the message parses to that shape, else the message unchanged (#88) —
    a plain (non-JSON) cause always passes through verbatim. Unwrapping gives the ledger/events
    the short human message ("The 'gpt-5-codex' model is not supported when using Codex with a
    ChatGPT account") instead of the encoded blob, and fits the 500-char error cap better; the
    provider-unavailable markers still match, since the marker phrasing lives in the inner text."""
    try:
        decoded = json.loads(message)
    except (json.JSONDecodeError, ValueError, TypeError):
        return message
    if isinstance(decoded, dict):
        err = decoded.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
            return err["message"]
    return message


def _codex_failure_cause(events_stdout: str) -> str | None:
    """The real failure cause from a codex ``--json`` event stream — the ``turn.failed`` event's
    ``error.message`` (or a top-level ``{"type":"error", "message": …}`` event). codex reports a
    provider-side refusal (e.g. an HTTP 400 "model is not supported when using Codex with a
    ChatGPT account") ONLY on this stdout event stream; stderr carries just banners/warnings (the
    ``--full-auto`` deprecation notice). Surfacing this into ``RawResult.error`` lets the verdict's
    rate-limit / provider-unavailable classification (and the ledger) see the actual cause instead
    of an innocuous stderr excerpt (#80). The message is nested-JSON-decoded (#88) so the ledger
    records the clean inner ``error.message`` rather than the double-encoded blob. Returns the LAST
    such message seen, or None when the stream reported no failure event."""
    cause: str | None = None
    for line in events_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "turn.failed":
            err = ev.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
                cause = err["message"]
        elif ev.get("type") == "error" and isinstance(ev.get("message"), str) and ev["message"]:
            cause = ev["message"]
    return _unwrap_codex_error_message(cause) if cause is not None else None


# The git helper moved inward to ``orchestrator.gitcmd`` (#273) so the engine's ``gc``
# subcommand no longer imports a private symbol out of an execution adapter. Aliased under
# its historical private name because every call site in this package (and in
# deterministic_setup/deterministic_test, which import ``_git`` FROM this module) spells it
# that way.
_git = run_git


# --- tool-policy translation (#272) ------------------------------------------------------
# The engine states a provider-neutral posture (`ToolPolicy`); each transport below turns it
# into its own provider's primitive. These are the claude tool names for each posture bit —
# the ONLY place in the codebase that knows them.
_CLAUDE_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "NotebookEdit")
_CLAUDE_EXEC_TOOLS: tuple[str, ...] = ("Bash", "BashOutput", "KillShell")


def _claude_tool_policy_flags(work: WorkItem) -> list[str]:
    """Translate ``work.tool_policy`` into ``claude`` CLI flags (#272).

    EVIDENCE (probed 2026-07-29 against the installed CLI, haiku, in a scratch dir) that
    ``--disallowedTools`` is a real enforcement primitive and not silently ignored under
    ``--dangerously-skip-permissions``: with ``--disallowedTools "Write,Edit,NotebookEdit"``
    the model's ``Write`` call came back
    ``<tool_use_error>Error: No such tool available: Write. Write exists but is not enabled in
    this context.</tool_use_error>`` and no file was created. The tool is removed from the
    toolset, not merely gated behind a prompt — so the deny rule survives bypassPermissions
    and this policy is enforced rather than decorative. (Same probe: ``MultiEdit`` is NOT a
    known tool name — the CLI warns ``matches no known tool`` — which is why it is absent from
    ``_CLAUDE_WRITE_TOOLS``.)

    HONEST LIMIT: with command execution retained (REVIEW's deliberate trade-off), the model
    can still write through the shell. This narrows the toolset against inadvertent mutation;
    it is not a sandbox. Isolating a verifier's test run from the live worktree is tracked
    separately.

    No policy => an EMPTY list, so the argv stays byte-identical to the pre-#272 one."""
    policy = work.tool_policy
    if policy is None:
        return []
    denied: list[str] = []
    if not policy.allow_file_writes:
        denied += list(_CLAUDE_WRITE_TOOLS)
    if not policy.allow_command_execution:
        denied += list(_CLAUDE_EXEC_TOOLS)
    if not denied:
        return []
    # Comma-joined single argument: `--disallowedTools` is variadic (comma OR space
    # separated), and a space-separated list would swallow the following flags.
    return ["--disallowedTools", ",".join(denied)]


def resolve_permission_posture(work: WorkItem) -> PermissionPosture:
    """The permission gate this dispatch runs under (#304) — the ONE source both provider
    translations below read, so claude and codex can never disagree about it.

    Resolution is MONOTONE in tightness, which is the whole safety property:

    1. a write-denying ``tool_policy`` forces RESTRICTED, whatever the stamp says — a stage
       posture can always tighten, so a mis-stamped WorkItem cannot hand a read-only stage
       blanket permission;
    2. otherwise the engine's stamp (the lane's declared default) wins;
    3. an UNSTAMPED item — built by a direct-transport caller, or loaded from a pre-#304
       doc on resume — falls back to BYPASS, exactly what the transport did when the flag
       was an unconditional constant.
    """
    policy = work.tool_policy
    if policy is not None and not policy.allow_file_writes:
        return PermissionPosture.RESTRICTED
    return work.permission_posture or PermissionPosture.BYPASS


def _claude_permission_flags(work: WorkItem) -> list[str]:
    """Translate the resolved posture into ``claude``'s permission flags (#304).

    BYPASS emits ``--dangerously-skip-permissions``, as every headless dispatch did
    unconditionally before this change. RESTRICTED emits NO bypass flag: the session keeps
    its normal permission gate and gets an explicit ``--allowedTools`` pre-grant for exactly
    the tools the posture still allows.

    The grant is derived from the SAME ``ToolPolicy`` bits the deny-list above reads, both of
    them: a RESTRICTED lane whose stage still allows writes pre-grants the write tools too,
    so a writing stage on a no-blanket-permission lane can still write. Withholding blanket
    permission must not silently withhold the stage's own declared authority — with no TTY to
    answer a prompt, an ungranted-but-undenied tool is the one shape this posture must never
    produce (grant it or deny it, never leave it to the gate).

    EVIDENCE (probed 2026-07-30 against the installed CLI, haiku, ``-p`` in a scratch dir)
    that dropping the bypass flag does not break — or hang — a non-interactive dispatch.
    With ``--allowedTools Bash,BashOutput,KillShell --disallowedTools Write,Edit,NotebookEdit``
    and no permission flag at all:

    * ``Bash`` RAN (``echo probe-bash-ok`` returned its output) — the pre-grant is honored,
      so REVIEW's deliberate keep-command-execution trade-off survives without bypass;
    * ``Read`` RAN unlisted — the provider's default-allowed read tools do not need the
      pre-grant, which is why the grant list carries only the exec tools;
    * ``Write`` came back ``Error: No such tool available: Write. Write exists but is not
      enabled in this context.`` and no file was created;
    * the run exited 0 with ``permission_denials: []`` and no stall — a tool outside the
      grant is REFUSED in-band, never queued behind a prompt nobody can answer;
    * the grant PROPAGATES to subagents (a spawned agent ran ``echo`` fine), which matters
      because SCOPE and REVIEW both delegate searching — a grant that stopped at the
      top-level agent would have broken exactly the stages this posture is for.

    The honest limit is unchanged from #272: a stage that keeps command execution can still
    write through the shell. RESTRICTED narrows *authority granted by default*, it is not a
    sandbox — codex's sandbox below is the lane with real containment."""
    if resolve_permission_posture(work) is PermissionPosture.BYPASS:
        return ["--dangerously-skip-permissions"]
    policy = work.tool_policy
    granted: list[str] = []
    if policy is None or policy.allow_file_writes:
        granted += list(_CLAUDE_WRITE_TOOLS)
    if policy is None or policy.allow_command_execution:
        granted += list(_CLAUDE_EXEC_TOOLS)
    if not granted:
        # Nothing beyond the default-allowed read tools — say nothing rather than pass an
        # empty flag value the CLI would read as a stray argument.
        return []
    # Comma-joined for the same reason as `--disallowedTools`: the flag is variadic, and a
    # space-separated list would swallow the flags that follow it.
    return ["--allowedTools", ",".join(granted)]


# #351: codex's workspace-write sandbox denies network EGRESS by default, and that is the
# whole reason `batch-codex-3` delivered nothing. DELIVER's `gh pr view`/`gh pr create` came
# back "error connecting to api.github.com" on every task while the same `gh`, the same auth
# and the same repo work from an ordinary shell. Reproduced on codex-cli 0.146.0: inside the
# default `codex exec` sandbox `gh api user`, `curl https://api.github.com` and
# `git ls-remote https://github.com/…` all fail (DNS never resolves); adding this one config
# override flips the banner to `sandbox: workspace-write [...] (network access enabled)` and
# all three succeed, `gh` included — the keychain-stored token IS readable in-sandbox, so the
# blockage was purely egress, not auth.
#
# It is NOT the API host specifically (the issue's first hypothesis): git and curl are denied
# the same way, so nothing about `api.github.com` is special. The run's own evidence looked
# host-specific only because #227's `git push` did somehow get through moments before
# `gh pr view` did not — that one success is still unexplained, and the point of declaring the
# grant is precisely that a stage's network no longer depends on whatever produced it.
#
# One in-sandbox limit survives this and is worth knowing before leaning on it: `gh` works
# (it can read its token from a config file), but git over HTTPS HANGS on the macOS keychain
# credential helper until the dispatch timeout. DELIVER opens its PR with `gh`, so its blocking
# path is clear; a stage that must PUSH over an https remote on this lane is not proven.
#
# Only the workspace-write paths get it: the key is namespaced under `sandbox_workspace_write`
# and would be inert on `--sandbox read-only`, where codex offers no network knob at all. That
# is a real remaining limit — a read-only REVIEW on this lane still cannot reach `gh` — but it
# is the posture asking for containment, not the bug this constant fixes.
_CODEX_NETWORK_CFG = ["-c", "sandbox_workspace_write.network_access=true"]


def _codex_permission_read_only(work: WorkItem) -> bool:
    """Does this dispatch get codex's read-only sandbox (#272, routed through #304's posture)?

    codex has no per-tool primitive — its enforcement knob is the process sandbox
    (``--sandbox read-only``), which is COARSER than claude's tool deny-list: a command that
    writes anywhere (a pytest cache, a build dir) fails under it. That is the price of real
    enforcement on this lane, and it is why the posture maps to the sandbox rather than being
    dropped as untranslatable. #301's runner-isolated REVIEW is the exception: there the
    process may write inside a disposable clone so ordinary verification commands work.

    A RESTRICTED posture from the LANE alone (no write-denying stage policy) does NOT go
    read-only: it would break every writing stage on that lane, and there is nothing else to
    translate — ``codex exec`` is sandboxed on every path this transport emits and the true
    bypass (``--dangerously-bypass-approvals-and-sandbox``) is never used, so codex has no
    blanket-permission grant to withhold in the first place."""
    # #301: a REVIEW running in an independent throwaway checkout needs workspace-write so
    # pytest/build commands can create their ordinary caches and outputs.  The live task tree
    # and its port block are not reachable from that call, so the coarse process sandbox no
    # longer has to make the verifier brittle to contain those writes.  Claude retains its
    # finer Write/Edit deny-list inside the same isolation.
    if work.workspace_isolated:
        return False
    if resolve_permission_posture(work) is not PermissionPosture.RESTRICTED:
        return False
    policy = work.tool_policy
    return policy is not None and not policy.allow_file_writes


def subprocess_env(work: WorkItem) -> dict[str, str] | None:
    """The environment a stage's subprocess runs with: the inherited process env with the
    WorkItem's per-task ``env`` merged OVER it (#5 port injection), or ``None`` to inherit
    unchanged when the WorkItem carries no extra env (the common, no-ports case)."""
    extra = getattr(work, "env", None)
    if not extra:
        return None
    return {**os.environ, **extra}


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
    data: dict, *, stdout: str, stderr: str, invocation: str, stream_files: dict | None,
    raw_output: str | None = None,
) -> RawResult:
    """Build a RawResult from a claude result envelope (the whole ``--output-format json``
    object, or the ``type=="result"`` event of the ``stream-json`` stream — same field
    names). Shared so both output formats recover structured output identically.

    ``raw_output`` (#93) sets the human-facing ``raw_output`` explicitly: the single-shot
    ``--output-format json`` path leaves it None so it defaults to ``stdout`` (which there IS the
    readable JSON envelope), while the streaming path passes the model's extracted FINAL TEXT so
    the raw event stream never lands in the .md Commentary / failure-learning tail (the full
    stream is already teed to ``.stream.jsonl``)."""
    # Presence check, not truthiness: a valid-but-empty structured_output ({}) must not
    # fall through to the prose `result`.
    structured = data["structured_output"] if "structured_output" in data else data.get("result")
    if isinstance(structured, str):
        # The model answered in prose/fenced text rather than the structured-output tool —
        # recover the JSON object it printed (validated later by the schema-retry loop).
        structured = _json_object_from_text(structured)
    session_id = data.get("session_id")
    return RawResult(structured, _usage_from(data),
                     raw_output=raw_output if raw_output is not None else stdout,
                     raw_stderr=stderr, exit_code=0, invocation=invocation,
                     stream_files=stream_files,
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


def _json_envelope(stdout: str) -> dict | None:
    """The ``--output-format json`` envelope from a single-shot claude stdout, or None if it
    isn't decodable JSON (a banner/notice, or a stream torn mid-write)."""
    try:
        data = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _recover_usage(stdout: str, *, streaming: bool) -> tuple[TokenUsage, str | None, bool]:
    """Best-effort ``(usage, session_ref, recovered)`` for a claude call that did NOT return
    an exit-0 success (#319).

    A non-zero exit — or a timeout — does not mean nothing was spent: the CLI emits its
    terminal ``result`` envelope carrying real ``usage`` and ``session_id`` even when it sets
    ``is_error: true`` (the live case: a connection dropped 11.8 minutes into a high-effort
    Opus stage that had already burned $4.25). Parsing it here is what keeps a failed attempt
    billed to the ledger and its session available to warm retry (#8).

    ``recovered`` is False when no envelope could be read at all (the process was killed
    before it printed one). The caller propagates that to the ledger, which then writes the
    row unpriced/unmetered — an honest unknown — instead of a confident $0.0000.
    """
    data = _last_result_event(stdout) if streaming else _json_envelope(stdout)
    if data is None:
        return TokenUsage(), None, False
    session_id = data.get("session_id")
    return (
        _usage_from(data),
        session_id if isinstance(session_id, str) else None,
        True,
    )


def _timeout_stdout(exc: subprocess.TimeoutExpired) -> str:
    """The partial stdout a timed-out call had produced before it was killed, as text ('' if
    none). ``_run_teed`` attaches what it already buffered; ``subprocess.run`` attaches its
    own. Bytes are decoded leniently — this is evidence, and must never raise into a caller."""
    out = exc.output
    if out is None:
        return ""
    return out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)


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

    def _call(work: WorkItem, session_ref: str | None, retry: int = 0) -> RawResult:
        # #66: with a run dir, stream `stream-json` and tee stdout as it arrives so the stage
        # is tailable live; otherwise the single-shot `json` path. `retry` (>=1, #70) names a
        # schema-retry sub-call's own `.retry<K>` stream so it doesn't clobber the first call's.
        # `--json-schema` +
        # `_JSON_ONLY_POSTAMBLE` are BEST-EFFORT on `claude -p` (after multi-turn agentic work
        # the model often ends in PROSE with no `structured_output` — the headless #25 failure),
        # so both paths recover the object from `result` text and the schema-retry loop below
        # is the real shape guarantee.
        streaming = root is not None
        fmt = ["--output-format", "stream-json", "--verbose"] if streaming \
            else ["--output-format", "json"]
        # #304: the permission gate is now DERIVED (lane default, tightened by a write-denying
        # stage posture) rather than the unconditional `--dangerously-skip-permissions` this
        # line used to hardcode. BYPASS emits that flag in the same argv position as before,
        # so an unpoliced dispatch is byte-identical; a read-only stage instead runs under the
        # normal gate with an explicit pre-grant for the tools its posture still allows.
        argv = ["claude", "-p", work.prompt, "--model", work.model,
                *_claude_permission_flags(work), *fmt]
        # #272: narrow the TOOLSET for a posture-bearing dispatch (SCOPE/REVIEW). This is the
        # enforcement half and stands on its own: `--disallowedTools` removes the tool from the
        # toolset under EITHER permission posture, so the deny rule never depended on the gate.
        argv += _claude_tool_policy_flags(work)
        if work.effort:
            # #96: per-stage reasoning effort — the claude CLI's session effort level
            # (low/medium/high). Unset emits exactly the pre-#96 argv (byte-identical).
            argv += ["--effort", work.effort]
        if work.agent:
            argv += ["--agent", work.agent]
        if schema_json_for and (schema := schema_json_for(work.schema_ref)):
            argv += ["--json-schema", schema, "--append-system-prompt", _JSON_ONLY_POSTAMBLE]
        if session_ref:
            argv += ["--resume", session_ref]
        invocation = (f"claude -p --model {work.model}"
                      + (f" --agent {work.agent}" if work.agent else "")
                      + (f" --resume {session_ref}" if session_ref else ""))

        # #5: per-task port block injected into the CLI subprocess (the model runs the
        # project's tests, which boot dev/test servers), so parallel worktrees don't collide.
        proc_env = subprocess_env(work)
        stream_files: dict | None = None
        try:
            if root is not None:  # == streaming; narrows root for the _stream_dir calls below
                tee_path = _stream_dir(root, work.task_id) / stream_filename(
                    work.stage.value, work.attempt, retry, phase=work.phase
                )
                returncode, stdout, stderr = _run_teed(
                    argv, timeout=work.timeout_s, cwd=work.cwd, tee_path=tee_path, env=proc_env
                )
                stream_files = _streaming_stream_files(root, work, stdout, stderr, retry)
            else:
                proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                      timeout=work.timeout_s, cwd=work.cwd, env=proc_env)
                returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as exc:  # pragma: no cover - env dependent
            # No process ever ran, so zero usage here is a MEASURED zero, not an unknown —
            # usage_recovered stays True (nothing was spent, and the ledger may say so).
            return RawResult(None, exit_code=127, error=str(exc), invocation=invocation)
        except subprocess.TimeoutExpired as exc:
            # A hung CLI must not hang the scheduler — fail the dispatch on timeout. #319: a
            # timeout is one of the costliest failures a run has, so recover whatever usage the
            # partial stream had already reported; with no terminal event the spend is
            # genuinely unknown and the row must say so rather than claim $0.
            usage, session_id, recovered = _recover_usage(
                _timeout_stdout(exc), streaming=streaming
            )
            return RawResult(None, usage=usage, exit_code=124,
                             error=f"timed out after {work.timeout_s}s", invocation=invocation,
                             session_ref=session_id, usage_recovered=recovered)
        # #93: on the streaming path, raw_output is the model's readable final text (result
        # event / last assistant text), falling back to a bounded stream-tail note — NOT the
        # raw JSONL event stream (already teed to `.stream.jsonl`). The single-shot path keeps
        # raw_output = stdout, which there is the readable JSON envelope, not a stream.
        stream_rel = (stream_files or {}).get("stream")
        if returncode != 0:
            raw_out = stdout
            if streaming:
                raw_out = claude_final_text(stdout) or stream_tail_note(stdout, stream_rel)
            # #319: a failed call still SPENT what it spent. The terminal envelope carries the
            # usage and session id even under `is_error: true`, so parse it regardless of the
            # exit code — dropping it billed a real $4.25 attempt as free and threw away the
            # session warm retry (#8) needs for exactly this mechanical-failure case.
            usage, session_id, recovered = _recover_usage(stdout, streaming=streaming)
            return RawResult(None, usage=usage, exit_code=returncode,
                             error=stderr.strip()[:500],
                             raw_output=raw_out, raw_stderr=stderr,
                             invocation=invocation, stream_files=stream_files,
                             session_ref=session_id, usage_recovered=recovered)
        if streaming:
            data = _last_result_event(stdout)
            if data is None:
                # Exit 0 but no result event (partial/killed stream): fail the dispatch. The
                # stream ended before any usage report, so the spend is unknown (#319) — flag
                # it rather than let zeros read as a metered $0.
                return RawResult(None, raw_output=stream_tail_note(stdout, stream_rel),
                                 raw_stderr=stderr, exit_code=0,
                                 error="no result event in stream-json output",
                                 invocation=invocation, stream_files=stream_files,
                                 usage_recovered=False)
            raw_output_value = claude_final_text(stdout) or stream_tail_note(stdout, stream_rel)
        else:
            try:
                data = json.loads(stdout) if stdout.strip() else {}
            except json.JSONDecodeError as exc:
                # Exit 0 but non-JSON stdout (banner/notice): fail the dispatch, never
                # let the exception escape (every dispatch must yield a StageResult). No
                # decodable envelope means no usage report — an unknown, not $0 (#319).
                return RawResult(None, raw_output=stdout, raw_stderr=stderr, exit_code=0,
                                 error=f"non-JSON output: {exc}", invocation=invocation,
                                 usage_recovered=False)
            raw_output_value = None  # single-shot: default raw_output=stdout (the JSON envelope)
        # NB: the shape-gate is deliberately NOT applied here — the `run` loop below
        # validates (and, on failure, retries with a corrective prompt). Nulling out an
        # invalid object here would throw away exactly what the retry needs to quote back.
        return _envelope_to_raw(data, stdout=stdout, stderr=stderr,
                                invocation=invocation, stream_files=stream_files,
                                raw_output=raw_output_value)

    def run(work: WorkItem) -> RawResult:
        raw = _call(work, work.session_ref)
        # Fallback-to-fresh on a dead session ONLY (one retry, same dispatch). Any
        # other resume failure returns as-is and fails the dispatch normally.
        if work.session_ref and raw.exit_code != 0 and _session_lost(raw.error):
            raw = _call(work, None)

        # Without a schema in hand there is nothing to validate against (the CLI's
        # best-effort --json-schema wasn't sent) — keep the as-was behavior. The retry loop
        # itself is shared with the codex transport (_schema_retry_loop).
        schema_json = schema_json_for(work.schema_ref) if schema_json_for else None
        if not schema_json:
            return raw
        return _schema_retry_loop(raw, work, schema_json, max_schema_retries, _call)

    return run


def _codex_git_common_dir(work: WorkItem) -> str | None:
    """The MAIN checkout's git-common-dir a linked worktree must be able to write to for its
    commits to land (the worktree's ``.git`` is a file pointing there). Best-effort: no cwd /
    not a repo / any git hiccup returns None (no grant, never a failure). Shared by the fresh
    call (``--add-dir``) and the resume call (``-c`` writable-root, since ``codex exec resume``
    takes no ``--add-dir``)."""
    if not work.cwd:
        return None
    try:
        gcd = _git(work.cwd, "rev-parse", "--git-common-dir")
    except Exception:  # noqa: BLE001 - the grant is an optimization, never a failure
        return None
    common = gcd.stdout.strip()
    if gcd.returncode != 0 or not common:
        return None
    return str((Path(work.cwd) / common).resolve())


# --- codex-native persona parity (#74) ---------------------------------------------------
# claude reaches its stage persona via ``claude -p --agent <name>`` (the CLI reads the
# roster's ``.claude/agents/<name>.md``). codex has no ``--agent``; its persona/system-
# instructions convention is an ``AGENTS.md`` read from the working directory (and parents).
# So a codex-routed stage would run BARE while the equivalent claude stage gets the persona.
# This closes that gap: for each codex dispatch we resolve the WorkItem's agent content and
# materialize it as ``AGENTS.md`` in the task worktree — composing (not clobbering) any project-
# shipped AGENTS.md via an idempotent marker-delimited section (the same upsert shape as #64's
# PR-body section, so a repeated/next-stage dispatch REPLACES the section, never stacks it).
# File-based (not a flag) composes cleanly with #9's resume, which accepts no ``--add-dir``/
# persona flag. Best-effort by contract: any resolution/write failure is swallowed and noted on
# the RawResult, NEVER a failed call — exactly like the stream tee.

_PERSONA_MARKER_START = "<!-- orchestrator:stage-persona:start -->"
_PERSONA_MARKER_END = "<!-- orchestrator:stage-persona:end -->"


def _strip_frontmatter(text: str) -> str:
    """Return an agent markdown's persona BODY, dropping a leading YAML frontmatter block
    (``---``\\n…\\n``---`` as the very first lines). Agent files carry a ``name``/``description``
    frontmatter the claude CLI parses; codex wants the instructions prose, not that metadata.
    Only a frontmatter fence that opens on line 1 is stripped (a ``---`` horizontal rule mid-file
    is left alone); an unterminated fence is treated as body (never lose content)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text.strip()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text.strip()


def _agent_md_candidates(work: WorkItem) -> list[Path]:
    """Ordered locations to find the WorkItem's agent markdown: the driving repo's
    ``.claude/agents/<name>.md`` (repo_root taken as the parent of the worktree's git-common-dir
    — the transport-visible analog of the #42 ``repo_root`` hook), then the worktree checkout's
    own ``.claude/agents/`` (the CWD convention, for a repo that commits its roster), then the
    packaged starter kit (how seeded agents ship)."""
    name = work.agent
    paths: list[Path] = []
    common = _codex_git_common_dir(work)  # <repo_root>/.git resolved, or None
    if common:
        paths.append(Path(common).parent / ".claude" / "agents" / f"{name}.md")
    if work.cwd:
        paths.append(Path(work.cwd) / ".claude" / "agents" / f"{name}.md")
    with contextlib.suppress(Exception):  # packaged-kit lookup must never break resolution
        from orchestrator.scaffold import KIT_DIR

        paths.append(Path(KIT_DIR) / "agents" / f"{name}.md")
    return paths


def _resolve_agent_content(work: WorkItem) -> tuple[str, Path] | None:
    """``(persona_body, source_path)`` for the WorkItem's agent, or None when it carries no
    agent or none of the candidate locations hold a readable, non-empty file. Frontmatter is
    stripped so codex gets the instructions prose."""
    if not work.agent:
        return None
    for path in _agent_md_candidates(work):
        try:
            if path.is_file():
                body = _strip_frontmatter(path.read_text(encoding="utf-8"))
                if body:
                    return body, path
        except OSError:
            continue
    return None


def _compose_agents_md(existing: str | None, persona: str) -> str:
    """The AGENTS.md content: any project-shipped AGENTS.md with the stage persona upserted under
    an idempotent marker-delimited section. A repeated dispatch (retry) or a stage transition
    REPLACES the section in place rather than stacking a new one; with no existing file the
    section is the whole file."""
    section = (
        f"{_PERSONA_MARKER_START}\n"
        "# Stage persona (orchestrator-injected)\n\n"
        f"{persona}\n"
        f"{_PERSONA_MARKER_END}"
    )
    if existing is None:
        return section + "\n"
    if _PERSONA_MARKER_START in existing and _PERSONA_MARKER_END in existing:
        # Lambda replacement so a persona containing backslashes/group refs inserts literally.
        return re.sub(
            re.escape(_PERSONA_MARKER_START) + r".*?" + re.escape(_PERSONA_MARKER_END),
            lambda _m: section,
            existing,
            flags=re.DOTALL,
        )
    sep = "" if existing.endswith("\n") else "\n"
    return f"{existing}{sep}\n{section}\n"


def _materialize_persona(work: WorkItem) -> dict | None:
    """Materialize the WorkItem's stage persona into ``<worktree>/AGENTS.md`` for a codex
    dispatch so codex — which has no ``--agent`` flag and reads AGENTS.md from cwd — runs WITH
    the roster's persona (parity with the claude ``--agent`` path, #74). Composes onto any
    project-shipped AGENTS.md via an idempotent marker section (replaced per dispatch, never
    stacked). Returns ``{"agent", "path"}`` for the observability slot (rides onto
    ``RawResult.persona_injected`` -> the stage log, like ``stream_files``), or None when there's
    no worktree, no agent, or nothing resolved (in which case any existing AGENTS.md is left
    UNTOUCHED and nothing is written). Best-effort: any failure returns ``{"agent", "error"}``
    and never breaks the call."""
    if not work.cwd or not work.agent:
        return None
    try:
        resolved = _resolve_agent_content(work)
        if resolved is None:
            return None  # no persona content -> leave any existing AGENTS.md untouched
        persona, source = resolved
        target = Path(work.cwd) / "AGENTS.md"
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(_compose_agents_md(existing, persona), encoding="utf-8")
        return {"agent": work.agent, "path": str(source)}
    except Exception as exc:  # noqa: BLE001 - persona injection must never break the model call
        return {"agent": work.agent, "error": f"persona injection failed: {exc}"[:300]}


def codex_cli_transport(
    schema_json_for: Callable[[str], str | None] | None = None,
    *,
    max_schema_retries: int = 2,
    run_log_root: str | Path | None = None,
) -> Transport:
    """Real codex transport: ``codex exec --json --output-last-message`` (with session
    continuity + schema-validate-and-retry — codex parity, #9/#21).

    ``run_log_root`` (#66): codex ``--json`` already emits its events as a JSONL stream, so
    when a run dir is wired the stdout is teed to the stage's ``.stream.jsonl`` LINE-BY-LINE as
    it arrives (tailable live), same as the claude path.

    ``schema_json_for`` returns the stage's JSON Schema JSON so codex enforces the final-message
    shape (``--output-schema <file>`` — the codex analog of claude's ``--json-schema``) and, more
    importantly, so the transport can validate the structured output and RETRY on a violation —
    the loop lives here (shared ``_schema_retry_loop``), where a corrective follow-up can be
    issued, rather than only at the runner verdict (#21). None (the default) keeps the as-was
    validate-at-runner behavior for callers that don't wire a schema.

    Session continuity (#9): the installed ``codex`` CLI DOES expose resume. The first call's
    thread id (``thread.started``/``thread_id`` on the ``--json`` stream) is captured onto
    ``RawResult.session_ref``; a WorkItem carrying a ``session_ref`` resumes it
    (``codex exec resume <id>``) so later stages/retries of a task keep warm context. ``resume``
    accepts no ``--full-auto``/``--sandbox``/``--add-dir``, so the fresh call's write posture is
    replicated with ``-c`` config overrides (workspace-write + non-blocking approvals + the
    worktree's git-common-dir as a writable root). A stale/rejected id cold-starts once inside
    the same dispatch (continuity is routing metadata; correctness never depends on it).

    Error surfacing (#80): on a non-zero exit, ``_codex_failure_cause()`` extracts the real
    provider refusal from the stdout event stream (``turn.failed`` / ``error`` events) and
    prefers it over the stderr excerpt for ``RawResult.error``. This matters because codex
    reports provider-side refusals (e.g. a 400 "model is not supported when using Codex with a
    ChatGPT account") ONLY on the event stream; stderr carries just banners and deprecation
    notices that would otherwise be what the verdict and ledger see. That refusal's ``message``
    is itself double-encoded JSON, so it is nested-decoded (#88) to the clean inner
    ``error.message`` before it lands on ``RawResult.error``."""
    root = Path(run_log_root) if run_log_root is not None else None

    def _call(work: WorkItem, session_ref: str | None, retry: int = 0,
              use_schema: bool = True) -> RawResult:
        grant = _codex_git_common_dir(work)
        proc_env = subprocess_env(work)  # #5: per-task port block for the codex subprocess
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "last.json"
            schema_flags: list[str] = []
            if use_schema and schema_json_for and (schema := schema_json_for(work.schema_ref)):
                # `--output-schema` takes a FILE — the codex analog of claude's inline
                # `--json-schema`. Preventive first-call nudge; the retry loop is the guarantee.
                # OpenAI's strict validator requires `additionalProperties: false` on every
                # object node (live 400 `invalid_json_schema`, run live-20260704-2 #68), so the
                # nudge gets a strict-transformed COPY — engine-side validation keeps the
                # original schema.
                schema_file = Path(td) / "schema.json"
                schema_file.write_text(_codex_strict_schema(schema), encoding="utf-8")
                schema_flags = ["--output-schema", str(schema_file)]
            # #96: per-stage reasoning effort via codex's config override (same TOML-quoted
            # `-c` shape as the resume write posture below). In `tail` so BOTH the fresh and
            # resume calls carry it; unset emits exactly the pre-#96 argv (byte-identical).
            effort_cfg = (
                ["-c", f'model_reasoning_effort="{work.effort}"'] if work.effort else []
            )
            tail = ["-m", work.model, *effort_cfg, "--skip-git-repo-check", *schema_flags,
                    "--json", "--output-last-message", str(last), work.prompt]
            # #272: a write-denying posture swaps the write sandbox for codex's read-only one on
            # BOTH call shapes, so continuity cannot silently revert it. #301 deliberately
            # keeps workspace-write for a REVIEW already moved into a disposable clone; caches
            # and build output then work without reaching the live task tree. Unset policy
            # keeps the pre-#272 argv byte-identical.
            read_only = _codex_permission_read_only(work)
            if session_ref:
                # Resume carries the warm session. It takes no sandbox/approval/--add-dir flags,
                # so replicate `--full-auto`'s write posture via `-c` config overrides.
                if read_only:
                    write_cfg = ["-c", 'sandbox_mode="read-only"',
                                 "-c", 'approval_policy="never"']
                else:
                    write_cfg = ["-c", 'sandbox_mode="workspace-write"',
                                 "-c", 'approval_policy="never"',
                                 *_CODEX_NETWORK_CFG]
                    if grant:
                        write_cfg += [
                            "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(grant)}]"
                        ]
                argv = ["codex", "exec", "resume", session_ref, *write_cfg, *tail]
                invocation = f"codex exec resume {session_ref} --json (model {work.model})"
            elif read_only:
                # `--sandbox read-only` replaces `--full-auto` (which is workspace-write +
                # non-blocking approvals); `approval_policy="never"` is kept explicitly so a
                # sandbox denial fails the command instead of stalling on an approval request
                # nobody can answer.
                argv = ["codex", "exec", "--sandbox", "read-only",
                        "-c", 'approval_policy="never"', *tail]
                invocation = f"codex exec --sandbox read-only --json (model {work.model})"
            else:
                # codex-cli 0.147.0 REMOVED `--full-auto` (`error: unexpected argument`), so it
                # is spelled out as the two things it stood for: workspace-write plus
                # non-blocking approvals — the same pair the resume branch above already sets
                # via `-c`. Latent until batch-369-371: this branch is the only one that needs
                # a COLD start on a WRITING stage, and SCOPE (write-denying, so read-only)
                # normally seeds the session every later stage resumes. A REVIEW fix-cycle
                # re-dispatch of IMPLEMENT arrives with no session_ref and lands here.
                add_dir = ["--add-dir", grant] if grant else []
                argv = ["codex", "exec", "--sandbox", "workspace-write",
                        "-c", 'approval_policy="never"',
                        *add_dir, *_CODEX_NETWORK_CFG, *tail]
                invocation = f"codex exec --json (model {work.model})"
            stream_files: dict | None = None
            try:
                if root is not None:
                    tee_path = _stream_dir(root, work.task_id) / stream_filename(
                        work.stage.value, work.attempt, retry, phase=work.phase
                    )
                    returncode, stdout, stderr = _run_teed(
                        argv, timeout=work.timeout_s, cwd=work.cwd, tee_path=tee_path, env=proc_env
                    )
                    stream_files = _streaming_stream_files(root, work, stdout, stderr, retry)
                else:
                    proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                          timeout=work.timeout_s, cwd=work.cwd, env=proc_env)
                    returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            except FileNotFoundError as exc:  # pragma: no cover - env dependent
                # Nothing ran, so the zeros are measured, not unknown (see the claude lane).
                return RawResult(None, exit_code=127, error=str(exc), invocation=invocation)
            except subprocess.TimeoutExpired as exc:
                # #319: recover whatever the partial `--json` event stream already reported —
                # codex emits cumulative `usage` per turn, so a long timed-out call is billed
                # for the turns it completed instead of recorded as free. No events at all
                # leaves usage unknown, and the row says so.
                partial = _timeout_stdout(exc)
                usage = _codex_usage(partial)
                return RawResult(None, usage=usage, exit_code=124,
                                 error=f"timed out after {work.timeout_s}s",
                                 invocation=invocation,
                                 session_ref=_codex_session_id(partial),
                                 usage_recovered=_has_tokens(usage))
            structured = None
            last_txt: str | None = None
            if last.exists():
                last_txt = last.read_text().strip()
                try:
                    structured = json.loads(last_txt)
                except json.JSONDecodeError:
                    structured = None
            error = None
            if returncode:
                # Prefer the event stream's failure cause (the real provider refusal — e.g. a 400
                # "model is not supported") over the stderr excerpt, which is often just a banner
                # (the `--full-auto` deprecation warning). This is what the verdict classifies on
                # and what lands on the ledger (#80).
                error = (_codex_failure_cause(stdout) or stderr.strip() or None)
                if error:
                    error = error[:500]
            # #93: raw_output is the model's readable final text — the last `agent_message` item,
            # falling back to the prose `--output-last-message` content (when it's prose, not the
            # structured JSON object that already rode `structured_output`), then a bounded stream-
            # tail note. NOT the raw `--json` event stream, which is already teed to `.stream.jsonl`.
            # `_codex_usage`/`_codex_session_id`/`_codex_failure_cause`/`_codex_schema_rejected` all
            # read the local `stdout`/`error`, not raw_output, so this change doesn't touch them.
            final_text = codex_final_text(stdout)
            if final_text is None and last_txt and not last_txt.startswith("{"):
                final_text = last_txt
            if final_text is None:
                final_text = stream_tail_note(stdout, (stream_files or {}).get("stream"))
            # #319: same honest-unknown rule as the timeout branch above, and for the same
            # reason — a codex call can also exit non-zero having printed no usable event
            # (killed mid-dispatch, truncated/corrupt stdout). The event stream has no single
            # terminal envelope to point at, so the numbers themselves are the only evidence a
            # usage report landed: no tokens on a FAILED call means the spend is unknown, and
            # the ledger row must say so instead of asserting a metered $0.00. A successful
            # call is left as-measured (exit 0 IS the provider's report).
            usage = _codex_usage(stdout)
            return RawResult(structured, usage=usage, raw_output=final_text,
                             raw_stderr=stderr, exit_code=returncode, stream_files=stream_files,
                             error=error,
                             invocation=invocation, session_ref=_codex_session_id(stdout),
                             usage_recovered=(not returncode) or _has_tokens(usage))

    def run(work: WorkItem) -> RawResult:
        # #74: refresh the worktree's AGENTS.md with this stage's persona BEFORE dispatch (fresh
        # OR resumed), so codex — which reads AGENTS.md from cwd and has no `--agent` flag — runs
        # with the roster's persona, and a stage transition (implement -> reviewer) swaps it. The
        # marker-section upsert is idempotent, so the corrective schema-retries below (which
        # re-enter `_call`) never stack it. Best-effort; recorded on the RawResult, never fatal.
        persona = _materialize_persona(work)
        raw = _call(work, work.session_ref)
        # Resume fell over on a stale/rejected/missing session id → one cold retry, same
        # dispatch (#9). Any other resume error fails normally, mirroring the claude path.
        if work.session_ref and raw.exit_code != 0 and _session_lost(raw.error):
            raw = _call(work, None)
        # Without a schema wired there's nothing to validate — keep validate-at-runner behavior.
        schema_json = schema_json_for(work.schema_ref) if schema_json_for else None
        # Typed as the 3-arg shape _schema_retry_loop consumes so the schema-rejected
        # branch below can rebind it to a use_schema-dropping wrapper without a redefinition
        # clash (_call carries an extra defaulted use_schema arg, compatible with this).
        call: Callable[[WorkItem, str | None, int], RawResult] = _call
        # The schema nudge must never be the reason a call fails: if codex's strict validator
        # rejects even the transformed schema (a future strictness rule we don't cover), drop
        # the `--output-schema` flag for this dispatch — postamble + retry loop remain the
        # guarantee — instead of burning attempts on a 400 that no retry can fix.
        if schema_json and _codex_schema_rejected(raw):
            def call(w: WorkItem, s: str | None, retry: int = 0) -> RawResult:  # noqa: E306
                return _call(w, s, retry, use_schema=False)
            raw = call(work, work.session_ref)
        if schema_json:
            raw = _schema_retry_loop(raw, work, schema_json, max_schema_retries, call)
        return replace(raw, persona_injected=persona) if persona is not None else raw

    return run
