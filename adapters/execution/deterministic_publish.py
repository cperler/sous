"""Deterministic PUBLISH runner — the ENGINE lane (no model call).

#389 split what DELIVER used to do in one stage. DELIVER pushes the branch at its old
position in the pipeline, keeping the durability property that a dead run's work is
already on the remote (#385). This runner opens the pull request, and it runs AFTER
REVIEW — so a PR only ever describes work that passed review, and a rejected task's fix
cycles re-push a branch instead of churning an open PR through rounds of rejected commits.

The approval gate is this stage's POSITION in the pipeline, not a condition evaluated
here: a pipeline whose quality tier has no REVIEW (``QualityTier.NONE``) simply reaches
PUBLISH immediately. This runner never asks whether a review happened.

The duplicate-PR guard survives the split even though #378's stale-``pr_url`` reuse is now
unreachable by construction (no PR exists before this stage, so none can go stale during
the fix cycles). It survives for a DIFFERENT reason: PUBLISH has its own retry budget, so
an attempt that creates the PR and then fails before its result is recorded — a crash, a
timeout, a truncated response — will be retried, and without the guard the retry opens a
second PR for the same branch. The lookup is therefore idempotency, not staleness repair.

``gh pr create`` mirrors the PUBLISH stage template in orchestrator/stages.py (PR title/body
from the task id/title/issue; ``Closes #N`` when an issue number is present). Every
subprocess call goes through an injected ``runner`` (default ``subprocess.run``) so unit
tests never touch the network or a real ``gh``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .transport import RawResult, to_stage_result

# Base refs tried, in order, to answer "does this branch have anything to publish?" — the
# same project-agnostic ladder the deliver runner uses.
_BASE_CANDIDATES = ("@{u}", "origin/HEAD", "origin/main", "main")


class _PublishError(Exception):
    """A deterministic-publish failure (no commits / gh error) → ResultStatus.FAILURE."""


class DeterministicPublishRunner:
    """In-process ENGINE-lane runner for the PUBLISH stage (delegated from the ENGINE cell)."""

    def __init__(
        self,
        project: object,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        timeout_s: int = 300,
    ) -> None:
        self._project = project  # kept for parity/future use; publish is gh + context
        self._runner = runner or subprocess.run
        self._timeout_s = timeout_s

    def dispatch(self, work: WorkItem) -> StageResult:
        try:
            out = self._publish(work)
        except _PublishError as exc:
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:publish")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        except Exception as exc:  # noqa: BLE001 - every dispatch MUST yield a StageResult,
            # never an escaped exception (an injected runner raising, a JSON parse blow-up).
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:publish")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        # No checkpoint tag: PUBLISH makes no commits (StageSpec.checkpoint is False), so
        # there is no new head to anchor and nothing for a retry to reset to.
        raw = RawResult(out, exit_code=0, invocation="engine:publish")
        return to_stage_result(work, raw, ResultStatus.SUCCESS,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def _publish(self, work: WorkItem) -> dict:
        cwd = work.cwd
        if not cwd:
            raise _PublishError("publish requires the task worktree (cwd); none set")
        ctx = work.context or {}
        branch = self._branch(cwd)
        if not self._has_commits(cwd):
            raise _PublishError(
                f"no commits on {branch} vs base — refusing to open an empty PR"
            )
        existing = self._existing_pr(cwd, branch)
        if existing is not None:
            # A prior attempt of THIS stage already opened it (see the module docstring):
            # report it rather than opening a duplicate.
            return {"pr_number": existing["number"], "pr_url": existing["url"], "reused": True}
        out = self._open_pr(cwd, branch, work, ctx)
        # Re-check after creating: two attempts racing (a retry dispatched while the first
        # was still in `gh pr create`) would otherwise both report their own PR. The
        # LOWEST-numbered open PR for the branch is the canonical one — it is the one that
        # existed first, so both racers agree on it without coordinating.
        canonical = self._existing_pr(cwd, branch)
        if canonical is not None and canonical["number"] != out["pr_number"]:
            return {"pr_number": canonical["number"], "pr_url": canonical["url"], "reused": True}
        return out

    # --- git / gh (all via the injected runner) -------------------------------
    def _run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess:
        return self._runner(argv, cwd=cwd, capture_output=True, text=True, timeout=self._timeout_s)

    def _branch(self, cwd: str) -> str:
        proc = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
        branch = (proc.stdout or "").strip()
        if proc.returncode != 0 or not branch or branch == "HEAD":
            raise _PublishError(
                f"could not resolve the task branch (git rev-parse: "
                f"{(proc.stderr or '').strip()[:200] or 'detached HEAD'})"
            )
        return branch

    def _has_commits(self, cwd: str) -> bool:
        for base in _BASE_CANDIDATES:
            proc = self._run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd)
            if proc.returncode == 0:
                return _to_int((proc.stdout or "").strip()) > 0
        proc = self._run(["git", "rev-list", "--count", "HEAD"], cwd)
        return _to_int((proc.stdout or "").strip()) > 0

    def _existing_pr(self, cwd: str, branch: str) -> dict | None:
        """The lowest-numbered OPEN PR whose head is ``branch``, or None.

        Deterministic tie-break (lowest number) so two attempts that both see two PRs pick
        the same one. A failing ``gh`` is an error, not an absence: treating "I could not
        ask" as "there is none" is exactly how a duplicate gets opened.
        """
        proc = self._run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json",
             "number,url,state,headRefName,baseRefName", "--limit", "10"],
            cwd,
        )
        if proc.returncode != 0:
            raise _PublishError(
                f"could not look up an open PR for {branch}: "
                f"{(proc.stderr or '').strip()[:200] or 'gh pr list failed'}"
            )
        try:
            arr = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise _PublishError(f"gh pr list returned invalid JSON for {branch}") from exc
        if not isinstance(arr, list):
            raise _PublishError(f"gh pr list returned an invalid payload for {branch}")
        candidates = []
        for raw in arr:
            if not isinstance(raw, dict):
                continue
            # The query itself constrains these fields; keep compatibility with older gh
            # versions/test doubles that omit requested discriminator fields.
            raw.setdefault("state", "OPEN")
            raw.setdefault("headRefName", branch)
            candidate = self._normalize_pr(raw)
            if candidate["state"] == "OPEN" and candidate["headRefName"] == branch:
                candidates.append(candidate)
        return min(candidates, key=lambda pr: pr["number"]) if candidates else None

    @staticmethod
    def _normalize_pr(raw: dict) -> dict:
        state = str(raw.get("state") or "").upper()
        head = str(raw.get("headRefName") or "")
        url = str(raw.get("url") or "")
        number = _to_int(raw.get("number")) or _pr_number(url)
        if state not in {"OPEN", "CLOSED", "MERGED"} or not head or not url or number <= 0:
            raise _PublishError(
                "GitHub returned incomplete PR evidence "
                f"(state={state!r}, headRefName={head!r}, number={number!r}, url={url!r})"
            )
        return {
            "number": number,
            "url": url,
            "state": state,
            "headRefName": head,
            "baseRefName": str(raw.get("baseRefName") or ""),
        }

    def _open_pr(self, cwd: str, branch: str, work: WorkItem, ctx: dict) -> dict:
        title = _pr_title(work, ctx)
        body = _pr_body(work, ctx)
        # No --base: let gh target the repository's default branch, exactly as the old
        # DELIVER did when it opened a FRESH pull request. The base-ancestry requirement
        # that used to live beside this call guarded REPLACEMENT PRs specifically — a
        # replacement opened against a base that had since absorbed the original could
        # regress it. #389 leaves no replacements to guard: the PR is opened once, after
        # approval, so a base that has merely moved ahead is the ordinary PR case and must
        # not refuse to publish reviewed work.
        argv = ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body]
        proc = self._run(argv, cwd)
        if proc.returncode != 0:
            raise _PublishError(f"gh pr create failed: {(proc.stderr or '').strip()[:200]}")
        url = _first_url((proc.stdout or "").strip())
        if not url:
            raise _PublishError(
                f"gh pr create returned no PR url: {(proc.stdout or '').strip()[:200]}"
            )
        return {"pr_number": _pr_number(url), "pr_url": url}


def _pr_title(work: WorkItem, ctx: dict) -> str:
    task_id = ctx.get("task_id") or work.task_id
    title = str(ctx.get("title") or "").strip()
    return f"{task_id}: {title}" if title else str(task_id)


def _pr_body(work: WorkItem, ctx: dict) -> str:
    task_id = ctx.get("task_id") or work.task_id
    lines = [f"Task: {task_id}"]
    body = str(ctx.get("body") or "").strip()
    if body:
        lines += ["", body]
    issue = ctx.get("issue_number")
    if issue is not None:
        # Mirror the PUBLISH template: a GitHub-issue task closes its issue on merge.
        lines += ["", f"Closes #{_issue_ref(issue)}"]
    # #232: when #216 composed a batch dependency's branch into this worktree at intake,
    # those commits ride along in this PR's diff until the dependency's own PR merges
    # (the accepted stacked-PR topology). Name them so a reviewer knows which commits are
    # upstream context vs. this task's own change, rather than guessing.
    deps = [str(d) for d in (ctx.get("composed_deps") or []) if str(d).strip()]
    if deps:
        lines += [
            "",
            "---",
            "_Built on batch dependencies composed at intake (#216); their commits "
            f"({', '.join(f'`{d}`' for d in deps)}) appear in this diff until those PRs "
            "merge. Changes beyond those commits are this task's own._",
        ]
    return "\n".join(lines)


def _issue_ref(issue: object) -> str:
    return str(issue).lstrip("#")


def _pr_number(url: str) -> int:
    m = re.search(r"/pull/(\d+)", url or "")
    return int(m.group(1)) if m else 0


def _first_url(text: str) -> str | None:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else None


def _to_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
