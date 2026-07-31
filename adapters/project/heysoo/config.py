"""Hey Soo! reference project-config adapter (target.md §5).

Ports the Hey Soo! commands + roster + taxonomy from the bash system into the
project-config surface the engine consumes. The fix-forwards land here: a generic
docstring agent (no ``phpdoc-writer``), and the test-taxonomy living in config.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.stage_schemas import resolve_stage_schema

from .classifier import HeysooClassifier
from .task_source import GitHubIssuesSource

# Roster (ports the agent names; review/spec stay generic, frontend/backend/design are
# product-specific drop-ins). docstring -> a GENERIC docstring agent (fix D13).
#
# Design content drop-in (#62 / target.md:128): heysoo's accumulated frontend-design
# judgment lives in the `bulletproof-frontend-developer` agent (its design-system tokens —
# ADR-053 light-only theme, monochrome baseline, component conventions — plus the linked
# `ui-design-fundamentals` skill). agent_for returns only the agent NAME; the runner
# resolves it cwd-relative against the product repo's `.claude/agents/<name>.md`, so the
# rich content stays in `heysoo/.claude/agents/bulletproof-frontend-developer.md`
# (read-only, NOT copied here). Both the frontend IMPLEMENT roles and the design REVIEW
# role point at it, so a design-tagged stage draws on the same design-system knowledge.
_ROSTER: dict[str, str] = {
    "implement": "python-backend-developer",
    "simplify": "code-simplifier",
    "implement:frontend": "bulletproof-frontend-developer",
    "implement:design": "bulletproof-frontend-developer",
    "test": "python-backend-developer",
    "review": "code-reviewer",
    "review:spec": "spec-reviewer",
    "review:design": "bulletproof-frontend-developer",
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

    def types_cmd(self) -> list[str]:
        # No-op sentinel (#243): heysoo has no static type checker DISTINCT from its
        # ``typecheck_cmd`` — tsc IS its type checker — so the trunk gate's separate
        # static-typing leg has nothing extra to run here and skips it observably.
        return ["true"]

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
        """Resolve a (stage, sub-role) to an agent name. A stage-qualified key
        (``"<stage>:<role>"``) wins over the bare role, so a task can opt a stage into a
        specialized agent by passing a bare sub-role: ``agent_for(REVIEW, "design")`` and
        ``agent_for(IMPLEMENT, "frontend"|"design")`` return the design agent, while the
        default stage roles (``"implement"``/``"review"``/…) fall through to the bare-key
        backend/review agents. A pipeline opts into the design role by pinning the stage's
        ``agent_role`` to ``"design"``; independently, the engine's REVIEW template appends
        the project-agnostic design-review lens whenever the change touches frontend files."""
        if not role:
            return None
        return _ROSTER.get(f"{stage.value}:{role}") or _ROSTER.get(role)

    # --- per-task port block (#5) ----------------------------------------------
    def port_env(self, base: int, count: int) -> dict[str, str]:
        """Reference port-injection hook (#5): the presence of this method is heysoo's
        opt-IN. Maps the engine's per-task port BLOCK onto the env vars the reference
        e2e-smoke.sh consumes — ``REACT_PORT`` (the Vite dev server ``npx vite --port``)
        and ``HEYSOO_REACT_URL`` (the Playwright ``baseURL``) — so two tasks running the
        e2e suite in parallel worktrees each drive their own Vite instead of colliding on
        :5173. The block base is the dev-server port; the rest of the block is headroom for
        additional servers a spec might boot."""
        return {
            "REACT_PORT": str(base),
            "HEYSOO_REACT_URL": f"http://localhost:{base}",
        }

    def schema_for(self, ref: str) -> dict | None:
        # Inherit the engine's canonical stage-output contracts (gives codex full-validation).
        return resolve_stage_schema(ref)

    # --- alerting sink (#55) ----------------------------------------------------
    def notify(self, kind: str, payload: dict) -> None:
        """Reference implementation of the alerting seam the old monitor's email +
        desktop-notify plugged into. Deliberately dead simple: always log a line to
        stderr, and best-effort fire a macOS desktop notification via ``osascript``
        (short timeout). Swallows ALL errors — the engine already guards this hook, so
        this is belt-and-suspenders. No email: SMTP config doesn't belong in this pass."""
        summary = str(payload.get("summary") or kind)
        print(f"[orchestrator:{kind}] {summary}", file=sys.stderr)
        try:
            # json.dumps yields valid AppleScript double-quoted string literals (no shell
            # involved — argv, not a shell string — so no injection surface).
            script = (
                f"display notification {json.dumps(summary)} "
                f"with title {json.dumps(f'orchestrator: {kind}')}"
            )
            subprocess.run(  # noqa: S603
                ["osascript", "-e", script],
                capture_output=True, timeout=5, check=False,
            )
        except Exception:  # noqa: BLE001 - an alert sink must never break the run
            pass

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
