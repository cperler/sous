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


def _session_lost(stderr: str | None) -> bool:
    text = (stderr or "").lower()
    return bool(text) and any(m in text for m in _SESSION_LOST_MARKERS)


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


def claude_cli_transport(schema_path_for: Callable[[str], str | None] | None = None) -> Transport:
    """Real headless×claude transport: shells ``claude -p ... --output-format json``.

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
        if schema_path_for and (sp := schema_path_for(work.schema_ref)):
            argv += ["--json-schema", sp]
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
            structured = None  # prose, not structured
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
