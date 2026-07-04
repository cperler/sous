"""Project-config adapter interface (target.md §5).

What a repo plugs in so the same engine drives any project. The Hey Soo! adapter
(``adapters/project/heysoo``) is the reference implementation. The engine depends
only on these Protocols, never on a concrete repo.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from orchestrator.failure_classifier import FailureClassifier
from orchestrator.schemas.enums import Stage

# The version of the ProjectConfig contract below. An adapter owned by an external
# project repo declares the version it was generated against (module-level
# ``CONTRACT_VERSION`` in its ``__init__.py``); the loader refuses a mismatch loudly
# instead of failing mid-run. Bump on any incompatible change to this surface.
ADAPTER_CONTRACT_VERSION = 1


class TaskSpec(BaseModel):
    """A task resolved from a task source (e.g. a GitHub issue)."""

    task_id: str
    title: str = ""
    body: str = ""
    issue_number: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    provider_tag: str | None = None  # e.g. "codex" — per-task provider routing tag


@runtime_checkable
class TaskSource(Protocol):
    """Pluggable task provider (build-fresh D8; GitHub-Issues is the reference impl)."""

    def resolve(self, task_id: str) -> TaskSpec: ...

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None: ...

    # Optional evidence-out hooks (NOT part of the versioned contract — the engine calls
    # them only via ``getattr`` at task finalize, so an older external adapter that omits
    # them keeps running unchanged; that's why adding them needs no CONTRACT_VERSION bump):
    #   publish_note(task_id, body, *, pr_url=None) -> None
    #       publish a run's completion evidence (a PR/issue comment, a log line, …).
    #   file_followup(title, body, labels=None) -> str | None
    #       open a follow-up (e.g. a review's non-blocking finding); return its ref/URL.
    # The shared GitHubIssuesSource and LocalFileTaskSource implement both.
    #
    # Optional PROJECT-CONFIG hook (same duck-typed pattern, on the ProjectConfig
    # itself rather than the task source):
    #   review_findings(*, worktree: str | None) -> list[dict]
    #       Deterministic review policy gates (#65 — the seam for the old e2e-policy
    #       gate / API-contract trigger / TSC gate family). Called by the engine when
    #       a REVIEW stage completes; each finding is {description, severity?, file?,
    #       line?, suggested_fix?, blocking?=True}. Blocking findings merge into the
    #       review's `issues` and force approved=false (the model cannot skip a policy
    #       gate); non-blocking ones join `non_blocking` and are filed as follow-ups.
    #       Must be best-effort and fast — it runs inline in record().
    #
    #   notify(kind: str, payload: dict) -> None
    #       Alerting sink (#55 — the seam the old bash monitor's email + desktop-notify
    #       plugged into). The engine calls it via ``Engine.emit_notification`` at the
    #       events it matters for: a task terminally failed, a task parked
    #       BLOCKED_ON_HUMAN autonomously, the batch circuit breaker paused the run, the
    #       run finalized, and (poll-driven, from the scheduler loop / the ``watch`` CLI)
    #       a task went stale. ``kind`` is one of alerting.NOTIFY_* ; ``payload`` carries
    #       {run_id, task_id?, kind, summary, and specifics like stage/reason}. Same
    #       duck-typed, best-effort contract as the hooks above: getattr-called, so a
    #       raising hook is swallowed + evented (``notify_failed``) and NEVER breaks a
    #       run, and adding it needs no CONTRACT_VERSION bump. Every notification is ALSO
    #       appended to events.jsonl (type ``notification``) so the audit trail shows
    #       what was signalled even when no hook is installed. HeysooConfig.notify is the
    #       reference sink (stderr line + best-effort macOS desktop notification).


@runtime_checkable
class ProjectConfig(Protocol):
    """The full per-repo adapter surface."""

    name: str

    # Optional (duck-typed, no CONTRACT_VERSION bump): filesystem path to the product
    # repo checkout the deterministic INTAKE runner creates worktrees in. When absent,
    # intake discovers the repo from process CWD (#42). Expose it (a ``str`` path or a
    # property) to decouple intake from the orchestrator's working directory.
    #   repo_root: str

    # Optional (duck-typed, no CONTRACT_VERSION bump), same pattern as repo_root: extra
    # lockfile names to fold into the intake install-cache hash (#63). Intake already
    # detects the common ones generically (uv.lock, package-lock.json, pnpm-lock.yaml,
    # yarn.lock, poetry.lock, Cargo.lock, composer.lock, Gemfile.lock, go.sum — see
    # adapters/execution/install_cache.DEFAULT_LOCKFILES); expose this ONLY when the repo
    # pins deps in a file that list misses. A ``list[str]`` or a zero-arg callable → list.
    #   lockfiles: list[str]

    # --- commands (shelled by runners / test-support, never by the engine itself) ---
    def install_cmd(self) -> list[str]: ...
    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]: ...
    def typecheck_cmd(self) -> list[str]: ...
    def infra_reset(self) -> list[str]: ...

    # --- pluggable behavior ---
    @property
    def classifier(self) -> FailureClassifier: ...

    @property
    def task_source(self) -> TaskSource: ...

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        """Map a (stage, optional sub-role) to an agent name. Returns None for default.

        Includes a generic docstring agent for the deliver stage (fix-forward D13:
        no phantom ``phpdoc-writer``).
        """
        ...

    def schema_for(self, ref: str) -> dict | None:
        """JSON Schema for a stage's structured output (drives codex full-validation).

        Optional — duck-typed via ``getattr`` by the CLI. Delegate to
        ``orchestrator.schemas.stage_schemas.resolve_stage_schema`` to inherit the
        engine's canonical stage contracts (with an optional project-local override).
        """
        ...
