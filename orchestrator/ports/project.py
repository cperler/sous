"""Project-config adapter PORT (target.md §5) — engine-owned, adapter-implemented.

What a repo plugs in so the same engine drives any project. The self-host adapter
(``adapters/project/selfhost``) is the reference implementation. The engine depends
only on these Protocols, never on a concrete repo.

This module lives INSIDE ``orchestrator/`` on purpose (#273): the dependency arrow
points inward, so an adapter imports the engine's contract and the engine imports no
adapter. ``adapters/project/base.py`` remains as a re-export shim for adapters
scaffolded before the move — new code should import from here.
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
    # When the SOURCE last changed this task, as the source reports it (#271). Recorded
    # alongside the snapshot the engine takes at add_task so a later refresh/staleness check
    # can say WHEN the upstream diverged, not just THAT it did. Optional and purely
    # informational — the engine's staleness verdict compares content fingerprints, never
    # this string, so a source that cannot report it (leaving None) is fully supported and
    # needs no ADAPTER_CONTRACT_VERSION bump.
    updated_at: str | None = None


@runtime_checkable
class TaskSource(Protocol):
    """Pluggable task provider (build-fresh D8; GitHub-Issues is the reference impl)."""

    def resolve(self, task_id: str) -> TaskSpec: ...

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None: ...

    # Optional, duck-typed (NOT part of the versioned contract — the batch-plan
    # ``candidates`` fetch calls it via ``getattr``, so a source without it simply can't
    # feed batch planning):
    #   list_tasks(label=None, limit=50) -> list[TaskSpec]
    #       List candidate tasks (e.g. OPEN issues) for the batch-plan producer (#57) — the
    #       model's input for auto-analysis over an already-filed batch. Should pre-populate
    #       each TaskSpec's ``depends_on`` from whatever edge encoding the source carries
    #       (GitHubIssuesSource reads ``Depends-on: #N`` lines the spec front door wrote), so
    #       edges already recorded aren't re-derived. The shared GitHubIssuesSource implements it.
    #   describe_issue(ref) -> {"ref", "state", "body", "pr"}
    #       Look up a filed issue's state (open/closed) and any discoverable PR url for the
    #       spec-conformance gate (#18 bullet 2 — ``spec_conformance.conformance_report``).
    #       Unlike ``resolve`` it does NOT refuse a closed issue (conformance is about the
    #       closed ones). Best-effort: PR discovery from the issue thread. Both the shared
    #       GitHubIssuesSource and LocalFileTaskSource implement it.

    # Optional evidence-out hooks (NOT part of the versioned contract — the engine calls
    # them only via ``getattr`` at task finalize, so an older external adapter that omits
    # them keeps running unchanged; that's why adding them needs no CONTRACT_VERSION bump):
    #   publish_note(task_id, body, *, pr_url=None) -> None
    #       publish a run's completion evidence (a PR/issue comment, a log line, …).
    #   publish_progress(task_id, body, *, marker, pr_url=None) -> None
    #       upsert MID-RUN progress (#64) — the living status the engine refreshes at each
    #       stage boundary (opt-in per run, throttled, best-effort) so a human can follow a
    #       long run from GitHub. UPSERT semantics (one living comment/section per task, never
    #       spam): the source finds its previous progress by the opaque ``marker`` token
    #       (which it wraps in a hidden HTML comment) and EDITS it, else creates it. Routes on
    #       ``pr_url``: a PR-body ``## Run progress`` section when a PR is known, else an issue
    #       comment — one method, one seam. Same duck-typed best-effort contract as
    #       publish_note: getattr-called at stage boundaries, a raising/missing hook is
    #       swallowed + evented (``progress_publish_failed``) and NEVER breaks record().
    #   file_followup(title, body, labels=None) -> str | None
    #       open a follow-up (e.g. a review's non-blocking finding); return its ref/URL.
    #   file_followup_keyed(title, body, labels=None, *, idempotency_key) -> str | None
    #       optional create-or-look-up variant for crash-safe external side effects. Kept
    #       distinct so adding keyed recovery does not break existing duck-typed adapters.
    # The shared GitHubIssuesSource and LocalFileTaskSource implement all four.
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
    #       events it matters for: a task COMPLETED (#359) or terminally failed, a task
    #       parked BLOCKED_ON_HUMAN autonomously, the batch circuit breaker paused the run,
    #       the run finalized, and (poll-driven, from the scheduler loop / the ``watch``
    #       CLI) a task went stale. ``kind`` is one of alerting.NOTIFY_* ; ``payload``
    #       carries {run_id, task_id?, kind, summary, and specifics like stage/reason}.
    #       Same duck-typed, best-effort contract as the hooks above: getattr-called, so a
    #       raising hook is swallowed + evented (``notify_failed``) and NEVER breaks a
    #       run, and adding it needs no CONTRACT_VERSION bump. Every notification is ALSO
    #       appended to events.jsonl (type ``notification``) so the audit trail shows
    #       what was signalled even when no hook is installed. SelfHostConfig.notify is the
    #       reference sink (stderr line + optional email).
    #
    #       The two PER-TASK terminal kinds (``task_completed``/``task_failed``) additionally
    #       carry the shared enrichment block from ``Engine._notification_facts`` (#359), so
    #       a sink can render an ACTIONABLE alert without the recipient re-opening `status`:
    #         task_id, title, issue_number, pr_url, pr_number, task_state, attempt,
    #         review_cycles, review_approved, run_dir (pointer to the retained runs/<run>/),
    #         stages ([{stage, status, attempt, model, error}] for every stage that RAN),
    #         cost ({usd, invocations, unmetered_calls} — the unmetered count travels WITH
    #         the figure per #319, so a sink never renders a confident $0 for unknown usage).
    #       ``task_completed`` also carries ``note_md`` (the render_completion_note markdown
    #       already published to the PR, bounded — reused rather than re-authored, since the
    #       engine never calls a model), plus followups_filed/improvement_ref.
    #       ``run_finalized`` carries a per-task ``tasks`` roster ({task_id, state, title,
    #       pr_url}) so a batch digest is renderable. The derived blocks are best-effort: a
    #       payload missing ``stages``/``cost`` is evented (``notification_facts_degraded``)
    #       rather than silently thinned, so a sink should treat both as optional.


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

    # Optional (duck-typed, no CONTRACT_VERSION bump), same pattern as repo_root/lockfiles:
    # per-task PORT injection (#5). Parallel tasks run in isolated worktrees but collide on
    # fixed dev/test-server ports; the engine allocates each task a contiguous port BLOCK
    # (orchestrator.port_registry) and exports it into every stage subprocess. Opt IN either
    # way:
    #   port_env(base: int, count: int) -> dict[str, str]
    #       Translate the block into THIS project's server env vars (e.g. a web app maps
    #       base → REACT_PORT + APP_URL). Merged OVER the generic vars the engine always
    #       exports (ORCHESTRATOR_PORT_BASE / ORCHESTRATOR_PORT_COUNT / PORT). Its mere
    #       presence is the opt-in.
    #   needs_ports: bool
    #       A truthy attribute opts in WITHOUT a translation hook — the task then sees only
    #       the generic ORCHESTRATOR_PORT_* / PORT vars.
    # Absent both, port allocation is a clean no-op for that project (no registry file, no
    # events, no env). Further optional knobs, if the defaults (range 42000-42999, block 10,
    # host-shared temp file) don't fit: ``port_range: tuple[int, int]``,
    # ``port_block_size: int``, ``port_registry_path: str``. Same-host scope only.

    # Optional (duck-typed via ``getattr``, NOT part of the versioned contract / not in
    # _REQUIRED_MEMBERS, no CONTRACT_VERSION bump — same duck-typed pattern as port_env/
    # notify, and deliberately NOT a Protocol-body method so a pre-#243 adapter still
    # satisfies ``isinstance(cfg, ProjectConfig)``):
    #   types_cmd() -> list[str]
    #       A STATIC-TYPING command DISTINCT from ``typecheck_cmd``, for a project whose CI
    #       runs a type checker AND a separate linter (this repo: mypy alongside ruff; #243).
    #       The post-merge ``Engine.trunk_gate`` runs it as an extra verification leg with the
    #       same ``['true']``/empty no-op handling the other commands get, so an adapter that
    #       omits it (or returns the sentinel) degrades to skipping it — observably (the gate
    #       records it under ``skipped``), never a crash. Return the no-op sentinel when the
    #       project has no type checker distinct from ``typecheck_cmd`` (e.g. a TS project whose
    #       ``typecheck_cmd`` IS ``tsc --noEmit``).

    # Optional worktree-provenance hooks (#381; duck-typed, no contract-version bump):
    #   fresh_install_paths() -> list[str]
    #       Relative dependency artifacts that must not be copied to a disposable REVIEW
    #       checkout and must be removed before reinstall (for example `.venv`).
    #   worktree_origin_probes() -> list[tuple[str, list[str], str]]
    #       Named argv commands whose final non-empty stdout line is an absolute runner or
    #       source path. The kind is "launcher" (the final symlink may target a shared
    #       interpreter) or "source" (the fully resolved path must remain in the worktree).
    #       Legacy two-value declarations default conservatively to "source". The execution
    #       adapter verifies every path before accepting baseline, TEST, or REVIEW results.
    #       Omission emits an explicit warning-grade skipped-verification notice.

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
