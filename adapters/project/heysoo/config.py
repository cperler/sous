"""Hey Soo! reference project-config adapter (target.md §5).

Ports the Hey Soo! commands + roster + taxonomy from the bash system into the
project-config surface the engine consumes. The fix-forwards land here: a generic
docstring agent (no ``phpdoc-writer``), and the test-taxonomy living in config.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import HeysooClassifier
from .task_source import GitHubIssuesSource

# Roster (ports the agent names; review/spec stay generic, frontend/backend are
# product-specific drop-ins). docstring -> a GENERIC docstring agent (fix D13).
_ROSTER: dict[str, str] = {
    "implement": "python-backend-developer",
    "implement:frontend": "bulletproof-frontend-developer",
    "test": "python-backend-developer",
    "review": "code-reviewer",
    "review:spec": "spec-reviewer",
    "docstring": "docstring-writer",  # generic; NOT phpdoc-writer
}


class HeysooConfig:
    name = "heysoo"

    def __init__(self, repo: str = "cperler/heysoo") -> None:
        self.repo = repo
        self._classifier = HeysooClassifier()
        self._task_source = GitHubIssuesSource(repo)

    # --- commands (ported; runners shell these inside a worktree) -------------
    def install_cmd(self) -> list[str]:
        return ["bash", "-lc", "npm install && uv sync"]

    def test_unit_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["uv", "run", "pytest", *files, "-v"]
        return ["bash", ".claude/scripts/test-unit.sh", "--skip-infra"]

    def test_e2e_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["bash", ".claude/scripts/e2e-smoke.sh", *files]
        return ["bash", ".claude/scripts/e2e-smoke.sh"]

    def test_shell_cmd(self, files: list[str] | None = None) -> list[str]:
        if files:
            return ["bash", ".claude/scripts/test-shell.sh", *files]
        return ["bash", ".claude/scripts/test-shell.sh"]

    def typecheck_cmd(self) -> list[str]:
        return ["npx", "tsc", "--noEmit"]

    def infra_reset(self) -> list[str]:
        # reset-infra.sh is a SOURCED function library under lib/ (not an executable
        # script — the old pointer at .claude/scripts/reset-infra.sh named a file that
        # doesn't exist); invoke its entry function with the worktree as the target.
        return ["bash", "-c",
                'source .claude/scripts/lib/reset-infra.sh && '
                'reset_test_infrastructure "$PWD" orchestrator-reset']

    # --- pluggable behavior ---------------------------------------------------
    @property
    def classifier(self) -> HeysooClassifier:
        return self._classifier

    @property
    def task_source(self) -> GitHubIssuesSource:
        return self._task_source

    def agent_for(self, stage: Stage, role: str | None = None) -> str | None:
        if role and role in _ROSTER:
            return _ROSTER[role]
        return None

    def schema_for(self, ref: str) -> dict | None:
        # Inherit the engine's canonical stage-output contracts (gives codex full-validation).
        return resolve_stage_schema(ref)

    # --- deterministic review policy gates (#65) --------------------------------
    def review_findings(self, *, worktree: str | None = None) -> list[dict]:
        """Reference implementation of the policy-gate seam: the TSC gate (OC:3689)
        and the e2e-policy check (OC:1201-1316) as engine-merged review findings the
        model cannot skip. Best-effort: no worktree / git hiccups yield no findings."""
        if not worktree or not Path(worktree).is_dir():
            return []
        return self._tsc_finding(worktree) + self._e2e_policy_finding(worktree)

    def _tsc_finding(self, worktree: str) -> list[dict]:
        try:
            proc = subprocess.run(  # noqa: S603
                self.typecheck_cmd(), cwd=worktree, capture_output=True, text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode == 0:
            return []
        tail = f"{proc.stdout}\n{proc.stderr}".strip()[-400:]
        return [{
            "severity": "critical", "blocking": True,
            "description": f"deterministic TSC gate: typecheck fails (rc={proc.returncode}): {tail}",
            "suggested_fix": "fix the type errors before this PR can be approved",
        }]

    def _changed_files(self, worktree: str) -> list[str]:
        for base in ("origin/main", "main", "master"):
            try:
                mb = subprocess.run(  # noqa: S603
                    ["git", "merge-base", "HEAD", base], cwd=worktree,
                    capture_output=True, text=True, timeout=30,
                )
                if mb.returncode != 0:
                    continue
                diff = subprocess.run(  # noqa: S603
                    ["git", "diff", "--name-only", f"{mb.stdout.strip()}..HEAD"],
                    cwd=worktree, capture_output=True, text=True, timeout=30,
                )
                if diff.returncode == 0:
                    return [f for f in diff.stdout.splitlines() if f.strip()]
            except (OSError, subprocess.SubprocessError):
                return []
        return []

    def _e2e_policy_finding(self, worktree: str) -> list[dict]:
        """E2E policy: a user-facing frontend change without any e2e spec change is a
        blocking finding (ports evaluate_e2e_policy's core heuristic)."""
        files = self._changed_files(worktree)
        if not files:
            return []
        frontend = [
            f for f in files
            if f.startswith("frontend/") and f.endswith((".ts", ".tsx"))
            and ".spec." not in f and "/tests/" not in f
        ]
        specs = [f for f in files if f.endswith(".spec.ts")]
        if frontend and not specs:
            shown = ", ".join(frontend[:5])
            return [{
                "severity": "critical", "blocking": True,
                "description": (
                    f"e2e policy: user-facing frontend change ({len(frontend)} file(s): "
                    f"{shown}) with no e2e spec change"
                ),
                "suggested_fix": "add/extend a Playwright spec covering the change, or "
                                 "record an explicit exemption in the PR",
            }]
        return []


def get_config() -> HeysooConfig:
    """Factory the CLI loads via ``--project adapters.project.heysoo``."""
    return HeysooConfig(repo=os.environ.get("HEYSOO_REPO", "cperler/heysoo"))
