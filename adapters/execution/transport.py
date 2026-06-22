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


Transport = Callable[[WorkItem], RawResult]


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


def claude_cli_transport(schema_path_for: Callable[[str], str | None] | None = None) -> Transport:
    """Real headless×claude transport: shells ``claude -p ... --output-format json``."""

    def run(work: WorkItem) -> RawResult:
        argv = ["claude", "-p", work.prompt, "--model", work.model,
                "--dangerously-skip-permissions", "--output-format", "json"]
        if work.agent:
            argv += ["--agent", work.agent]
        if schema_path_for and (sp := schema_path_for(work.schema_ref)):
            argv += ["--json-schema", sp]
        invocation = f"claude -p --model {work.model}" + (f" --agent {work.agent}" if work.agent else "")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=work.timeout_s)  # noqa: S603
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
        return RawResult(structured, _usage_from(data), raw_output=proc.stdout,
                         exit_code=0, invocation=invocation)

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
                                      timeout=work.timeout_s)
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
