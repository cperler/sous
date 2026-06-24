"""Failure retrospective auto-generation (target.md §9 / DEFERRED row 2b).

When a run finishes with failures, this turns the durable artifacts the engine
already persists — the ``events.jsonl`` timeline + the per-stage JSON logs + the
task documents — into a structured "what failed and why" summary, so a human (or a
follow-up agent) does not have to re-read the stage tree by hand.

Two pieces, matching the as-built names:
  - ``detect_failure_patterns`` — recompute the *structured* error signature (the
    same one the circuit breaker uses) for every failed stage attempt, then group:
    a signature that recurs within one task is the plateau the breaker caught; one
    that recurs across tasks is systemic.
  - ``build_retrospective`` — per-failed-task trail (failing stage, attempts,
    terminal reason, the learnings the retries accumulated, final error) + the
    cascade map + the recurring patterns.

Pure functions over already-read data (no I/O here) so they are trivially testable;
the engine reads the artifacts and feeds them in.
"""

from __future__ import annotations

from collections import defaultdict

from .retry import error_signature
from .schemas.enums import Stage, TaskState
from .schemas.status import Run, Task

# Terminal stage-record outcomes that mean the task itself failed.
_TASK_FAILED_OUTCOMES = {"task_failed_breaker", "task_failed_max_attempts"}


def _is_failure_status(status: str) -> bool:
    # rate_limited is a transient re-queue (graceful fallback), not a real failure —
    # it must not inflate the failure-pattern table a human reads.
    return status not in ("success", "skipped", "rate_limited")


def _failures_of(log: dict) -> list[str] | None:
    out = log.get("structured_output") or {}
    failures = out.get("failures") if isinstance(out, dict) else None
    return failures if isinstance(failures, list) and failures else None


def detect_failure_patterns(stage_logs_by_task: dict[str, list[dict]]) -> list[dict]:
    """Group failed stage attempts by their structured error signature.

    Returns one entry per distinct (signature) sorted by total occurrences desc,
    each with: stage, occurrences, the tasks it hit, a `within_task_plateau` flag
    (the same signature repeated inside a single task — what the breaker trips on),
    a `cross_task` flag (hit more than one task), and a sample error string.
    """
    by_sig: dict[str, dict] = {}
    per_task_sig_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for task_id, logs in stage_logs_by_task.items():
        for log in logs:
            if not _is_failure_status(log.get("status", "")):
                continue
            stage = Stage(log["stage"])
            sig = error_signature(stage, failures=_failures_of(log), error=log.get("error"))
            per_task_sig_counts[task_id][sig] += 1
            entry = by_sig.setdefault(
                sig,
                {
                    "signature": sig,
                    "stage": stage.value,
                    "occurrences": 0,
                    "tasks": set(),
                    "sample_error": (log.get("error") or "").strip()[:300] or None,
                },
            )
            entry["occurrences"] += 1
            entry["tasks"].add(task_id)

    patterns = []
    for sig, entry in by_sig.items():
        plateau = any(counts.get(sig, 0) >= 2 for counts in per_task_sig_counts.values())
        patterns.append(
            {
                "signature": sig[:12],  # short hash for display; full sig is internal
                "stage": entry["stage"],
                "occurrences": entry["occurrences"],
                "tasks": sorted(entry["tasks"]),
                "within_task_plateau": plateau,
                "cross_task": len(entry["tasks"]) > 1,
                "sample_error": entry["sample_error"],
            }
        )
    patterns.sort(key=lambda p: (p["occurrences"], p["cross_task"]), reverse=True)
    return patterns


def _terminal_reason(task: Task, events: list[dict]) -> str | None:
    """The outcome that put a task into its terminal failure state."""
    if task.state is TaskState.CASCADE_BLOCKED:
        return "cascade_blocked"
    for ev in reversed(events):
        if ev.get("task_id") == task.task_id and ev.get("outcome") in _TASK_FAILED_OUTCOMES:
            return ev["outcome"]
    return None


def _failing_stage(task: Task) -> Stage | None:
    from .schemas.enums import StageStatus

    for stage, rec in task.stages.items():
        if rec.status is StageStatus.FAILED:
            return stage
    return None


def build_retrospective(
    run: Run, tasks: list[Task], events: list[dict], stage_logs_by_task: dict[str, list[dict]]
) -> dict:
    """Assemble the structured failure retrospective for a finished run."""
    failed = [t for t in tasks if t.state is TaskState.FAILED]
    cascaded = [t for t in tasks if t.state is TaskState.CASCADE_BLOCKED]
    completed = [t for t in tasks if t.state is TaskState.COMPLETED]

    # cascade map: failed task -> dependents it blocked (from cascade_blocked events).
    cascade_map: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        if ev.get("type") == "cascade_blocked" and ev.get("caused_by"):
            cascade_map[ev["caused_by"]].append(ev["task_id"])

    task_reports = []
    for t in failed:
        stage = _failing_stage(t)
        rec = t.stages[stage] if stage else None
        task_reports.append(
            {
                "task_id": t.task_id,
                "title": t.title,
                "failing_stage": stage.value if stage else None,
                "attempts": (rec.attempt + 1) if rec else 0,
                "terminal_reason": _terminal_reason(t, events),
                "final_error": t.last_error or (rec.error if rec else None),
                "learnings": list(t.learnings),
                "blocked_dependents": sorted(cascade_map.get(t.task_id, [])),
            }
        )

    return {
        "run_id": run.run_id,
        "run_state": run.state.value,
        "totals": {
            "total": len(tasks),
            "completed": len(completed),
            "failed": len(failed),
            "cascade_blocked": len(cascaded),
        },
        "failed_tasks": task_reports,
        "cascade_blocked_tasks": [t.task_id for t in cascaded],
        "patterns": detect_failure_patterns(stage_logs_by_task),
    }
