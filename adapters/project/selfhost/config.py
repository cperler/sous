"""Self-host project-config adapter — the sous repo itself.

A pure Python/uv/pytest/ruff library: no frontend build, no lambda, no E2E/bats.
Demonstrates Phase 5 generality — the engine drives this with ZERO changes; only
this adapter differs from the Hey Soo! one (different commands, taxonomy, task source).

Tasks default to THIS repo's GitHub issues (the harness's own issue log is directly
orchestratable — the dogfood loop); set ``SELFHOST_TASKS`` to a JSON file for the
local/offline mode instead.
"""

from __future__ import annotations

import os
import subprocess
import sys

from adapters.project.email_sink import email_sink_from_env
from adapters.project.github_issues import GitHubIssuesSource
from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import SelfHostClassifier
from .task_source import LocalFileTaskSource

_SELF_REPO = "cperler/sous"

_NOOP = ["true"]  # this project has no e2e / shell / infra layer

# Bounds for the deterministic review gate (`review_findings`). The cap keeps a wall of
# mypy output from blowing the review prompt's context budget; the tail is kept rather
# than the head because the summary line ("Found N errors") lands last.
_GATE_TIMEOUT_S = 300
_GATE_OUTPUT_CAP = 2000

# Stage sub-role -> agent name. These resolve to the starter-kit personas seeded into
# this repo's own ``.claude/agents/`` (bootstrapped from templates/project-default), so
# both lanes can name them: the interactive lane as a subagent type, the headless lane as
# ``claude -p --agent <name>``.
_ROSTER: dict[str, str] = {
    "implement": "python-backend-developer",
    "simplify": "code-simplifier",
    "test": "test-validator",
    "review": "code-reviewer",
    "docstring": "docstring-writer",
}


class SelfHostConfig:
    name = "sous"

    def __init__(self, tasks_path: str | None = None, repo: str = _SELF_REPO) -> None:
        self._classifier = SelfHostClassifier()
        self._task_source = (
            LocalFileTaskSource(tasks_path) if tasks_path else GitHubIssuesSource(repo)
        )

    def install_cmd(self) -> list[str]:
        return ["uv", "sync"]

    def fresh_install_paths(self) -> list[str]:
        """Artifacts a disposable review must rebuild instead of copying."""
        return [".venv"]

    def worktree_origin_probes(self) -> list[tuple[str, list[str], str]]:
        """Resolve both the pytest launcher and this package through the worktree venv."""
        return [
            (
                "pytest shebang interpreter",
                [
                    "uv", "run", "python", "-c",
                    "from pathlib import Path; "
                    "line=Path('.venv/bin/pytest').read_text().splitlines()[0]; "
                    "print(line.removeprefix('#!'))",
                ],
                "launcher",
            ),
            (
                "orchestrator module",
                [
                    "uv", "run", "python", "-c",
                    "import orchestrator.engine as module; print(module.__file__)",
                ],
                "source",
            ),
        ]

    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]:
        return ["uv", "run", "pytest", *files, "-q"] if files else ["uv", "run", "pytest", "-q"]

    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP  # no E2E layer

    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]:
        return _NOOP

    def typecheck_cmd(self) -> list[str]:
        return ["uv", "run", "ruff", "check", "."]  # ruff is this project's LINT gate

    def types_cmd(self) -> list[str]:
        # Distinct STATIC-TYPING leg (#243): this repo's CI runs mypy alongside ruff, so
        # the post-merge trunk gate must too (typecheck_cmd above is the linter, not the
        # type checker). Without this the gate could report green on a trunk mypy — hence
        # CI — would fail.
        return ["uv", "run", "mypy"]

    def infra_reset(self) -> list[str]:
        return _NOOP

    def review_findings(self, *, worktree: str | None = None) -> list[dict]:
        """Deterministic review gate (#65): run this repo's LINT and TYPE legs over the
        task worktree and block approval on either going red.

        Why this exists. CLAUDE.md's first working norm is that `uv run pytest`,
        `uv run ruff check .` and the bare `uv run mypy` all stay green — "the same trio
        CI enforces". But only the FIRST of those runs during a task:
        ``DeterministicTestRunner`` shells ``test_unit_cmd``/``test_e2e_cmd``/
        ``test_shell_cmd`` and nothing else, while ``typecheck_cmd``/``types_cmd`` were
        reached only by the POST-MERGE trunk gate. So a task could implement, test,
        open its PR and be approved by the reviewer with red ruff or red mypy, and the
        first thing to notice would be CI on the PR — or the trunk gate, after merge.

        This closes that: a blocking finding forces ``approved=false``, which the engine
        turns into a bounded fix cycle with the failure text as learnings, so the run
        repairs itself instead of handing a human a red PR.

        Best-effort in the shape the seam expects. No worktree (or a path that isn't a
        directory) yields no findings. A command that could not be RUN at all — missing
        binary, timeout — yields an ADVISORY finding rather than silence or a block:
        unverified must not read as green, but nor should a flaky environment deadlock a
        task behind a gate it cannot satisfy."""
        if not worktree or not os.path.isdir(worktree):
            return []
        findings: list[dict] = []
        for label, argv in (("ruff (lint)", self.typecheck_cmd()),
                            ("mypy (types)", self.types_cmd())):
            if (finding := self._gate(worktree, label, argv)) is not None:
                findings.append(finding)
        return findings

    def _gate(self, worktree: str, label: str, argv: list[str]) -> dict | None:
        """One gate leg: None when it passes, a finding dict when it fails or can't run."""
        if not argv or argv == _NOOP:
            return None
        try:
            proc = self._run_gate(argv, worktree)
        except Exception as exc:  # noqa: BLE001 - a policy hook must never break record()
            return {
                "description": f"{label} gate could not run in the task worktree "
                               f"({type(exc).__name__}: {exc}). The gate is UNVERIFIED for "
                               f"this change — CI still enforces it on the PR.",
                "severity": "important",
                "blocking": False,  # unproven != failed; don't deadlock on a flaky env
            }
        if proc.returncode == 0:
            return None
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if len(detail) > _GATE_OUTPUT_CAP:
            detail = "…\n" + detail[-_GATE_OUTPUT_CAP:]
        return {
            "description": f"{label} gate is RED on this change — CLAUDE.md requires "
                           f"`{' '.join(argv)}` green on every change, and CI enforces it. "
                           f"Output:\n{detail}",
            "severity": "critical",
            "suggested_fix": f"Run `{' '.join(argv)}` in the worktree and fix what it reports.",
            "blocking": True,
        }

    @staticmethod
    def _run_gate(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        """Injectable seam: tests replace this so the suite never shells out to ruff/mypy."""
        return subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=_GATE_TIMEOUT_S, check=False
        )

    @property
    def classifier(self) -> SelfHostClassifier:
        return self._classifier

    @property
    def task_source(self) -> LocalFileTaskSource | GitHubIssuesSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        return _ROSTER.get(role) if role else None

    def schema_for(self, ref: str) -> dict | None:
        return resolve_stage_schema(ref)

    # --- alerting sink (#55/#359) -----------------------------------------------
    def notify(self, kind: str, payload: dict) -> None:
        """Alerting sink for runs against THIS repo. Before #359 this adapter had no
        ``notify`` at all, so every dogfood batch was entirely silent no matter what the
        seam supported — the gap that made a detached driver's completion undiscoverable
        except by polling ``status``.

        Always a stderr line; additionally mails the payload when the environment configures
        SMTP (see ``adapters.project.email_sink`` for the env vars). Unconfigured, the email
        half is a no-op. Deliberately no ``osascript`` here — this adapter is not
        macOS-specific. Swallows ALL errors: the engine already
        guards this hook, so this is belt-and-suspenders."""
        print(f"[orchestrator:{kind}] {payload.get('summary') or kind}", file=sys.stderr)
        try:
            if sink := email_sink_from_env():
                sink(kind, payload)  # swallows its own errors; short socket timeout
        except Exception:  # noqa: BLE001 - an alert sink must never break the run
            pass


def get_config() -> SelfHostConfig:
    return SelfHostConfig(
        tasks_path=os.environ.get("SELFHOST_TASKS"),
        repo=os.environ.get("SELFHOST_REPO", _SELF_REPO),
    )
