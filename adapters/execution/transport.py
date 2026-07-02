"""Transport layer for in-process runners (target.md §4 Phase 4).

Separates I/O (actually invoking ``claude -p`` / ``codex exec``) from the success
policy (each runner decides what counts as success). A ``Transport`` takes a
WorkItem and returns a normalized ``RawResult``; the real transports shell out, and
tests inject a fake. This keeps the runners deterministic and unit-testable without
a live model.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as _JSONSchemaError

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import LaneUsed, StageResult, TokenUsage, WorkItem


@dataclass
class RawResult:
    structured_output: dict | None
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_output: str | None = None
    exit_code: int = 0
    error: str | None = None
    invocation: str = ""
    # Provider session the call used/created (design pass §2); None if unsupported.
    session_ref: str | None = None
    # {"tag", "sha"} stamped by the checkpoint wrapper after a successful
    # git-affecting stage (design pass §3); None otherwise.
    checkpoint: dict | None = None


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
        return raw

    return run


def claude_cli_transport(schema_json_for: Callable[[str], str | None] | None = None) -> Transport:
    """Real headless×claude transport: shells ``claude -p ... --output-format json``.

    ``schema_json_for`` returns the stage's JSON Schema **inline** (the CLI's
    ``--json-schema`` takes the schema JSON itself, not a file path) so the reply is
    forced into the stage contract and comes back on the envelope's ``structured_output``.

    Session continuity (design pass §2): a WorkItem carrying ``session_ref`` resumes
    that conversation (``--resume``) so later stages of a task are nearly all
    cache-read; the JSON payload's ``session_id`` is reported back so the engine can
    chain the next stage. A lost/expired session cold-starts a fresh one inside the
    same dispatch — prompts are self-contained, so continuity is never load-bearing.
    """

    def _call(work: WorkItem, session_ref: str | None) -> RawResult:
        argv = ["claude", "-p", work.prompt, "--model", work.model,
                "--dangerously-skip-permissions", "--output-format", "json"]
        if work.agent:
            argv += ["--agent", work.agent]
        if schema_json_for and (schema := schema_json_for(work.schema_ref)):
            # `--json-schema` is BEST-EFFORT on `claude -p`, not a hard constraint: after
            # multi-turn agentic work the model often ends `stop_reason=end_turn` with a
            # PROSE summary and no `structured_output` (the headless #25 failure). Force
            # the contract with a system-prompt directive — the same lever the reference
            # bash system applied on its codex path (which also lacked --json-schema).
            argv += ["--json-schema", schema, "--append-system-prompt", _JSON_ONLY_POSTAMBLE]
        if session_ref:
            argv += ["--resume", session_ref]
        invocation = (f"claude -p --model {work.model}"
                      + (f" --agent {work.agent}" if work.agent else "")
                      + (f" --resume {session_ref}" if session_ref else ""))
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                  timeout=work.timeout_s, cwd=work.cwd)
        except FileNotFoundError as exc:  # pragma: no cover - env dependent
            return RawResult(None, exit_code=127, error=str(exc), invocation=invocation)
        except subprocess.TimeoutExpired:
            # A hung CLI must not hang the scheduler — fail the dispatch on timeout.
            return RawResult(None, exit_code=124,
                             error=f"timed out after {work.timeout_s}s", invocation=invocation)
        if proc.returncode != 0:
            return RawResult(None, exit_code=proc.returncode, error=proc.stderr.strip()[:500],
                             raw_output=proc.stdout, invocation=invocation)
        try:
            data = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError as exc:
            # Exit 0 but non-JSON stdout (banner/notice): fail the dispatch, never
            # let the exception escape (every dispatch must yield a StageResult).
            return RawResult(None, raw_output=proc.stdout, exit_code=0,
                             error=f"non-JSON output: {exc}", invocation=invocation)
        # Presence check, not truthiness: a valid-but-empty structured_output ({})
        # must not fall through to the prose `result`.
        structured = data["structured_output"] if "structured_output" in data else data.get("result")
        if isinstance(structured, str):
            # --json-schema only populates `structured_output` when the model answers via
            # the structured-output tool; a model that instead prints the JSON object as
            # text (often fenced ```json) lands it in `result`. Recover that rather than
            # discarding a schema-shaped answer as a SCHEMA_VIOLATION.
            structured = _json_object_from_text(structured)
        # Shape-gate: the CLI enforces --json-schema ONLY on the tool path, so a recovered
        # (or tool-path) object must still satisfy the stage schema — else a valid-JSON-but-
        # wrong-shape answer would record as SUCCESS with a missing required field. Failing
        # validation drops to None → SCHEMA_VIOLATION (the engine retries).
        if structured is not None and schema_json_for and (sj := schema_json_for(work.schema_ref)):
            structured = _validate_shape(structured, sj)
        session_id = data.get("session_id")
        return RawResult(structured, _usage_from(data), raw_output=proc.stdout,
                         exit_code=0, invocation=invocation,
                         session_ref=session_id if isinstance(session_id, str) else None)

    def run(work: WorkItem) -> RawResult:
        raw = _call(work, work.session_ref)
        # Fallback-to-fresh on a dead session ONLY (one retry, same dispatch). Any
        # other resume failure returns as-is and fails the dispatch normally.
        if work.session_ref and raw.exit_code != 0 and _session_lost(raw.error):
            raw = _call(work, None)
        return raw

    return run


def codex_cli_transport() -> Transport:
    """Real codex transport: ``codex exec --json --output-last-message``."""

    def run(work: WorkItem) -> RawResult:
        with tempfile.TemporaryDirectory() as td:
            last = Path(td) / "last.json"
            argv = ["codex", "exec", "-m", work.model, "--full-auto", "--skip-git-repo-check",
                    "--json", "--output-last-message", str(last), work.prompt]
            invocation = f"codex exec --json (model {work.model})"
            try:
                proc = subprocess.run(argv, capture_output=True, text=True,  # noqa: S603
                                      timeout=work.timeout_s, cwd=work.cwd)
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
            return RawResult(structured, usage=_codex_usage(proc.stdout), raw_output=proc.stdout,
                             exit_code=proc.returncode,
                             error=(proc.stderr.strip()[:500] or None) if proc.returncode else None,
                             invocation=invocation)

    return run
