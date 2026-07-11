"""Deterministic DELIVER runner — the ENGINE lane (no model call).

heysoo #227 follow-up (#33): pushing the task branch and opening a PR is mechanical
git/gh work. This runner verifies the branch has commits vs its base, pushes it, and
opens — or, on a review fix cycle, REUSES (leaving an advisory comment that the branch was
re-pushed, #68) — a pull request, deterministically at $0, mirroring the DELIVER stage
template in orchestrator/stages.py (PR title/body from the task id/title/issue; ``Closes #N``
when an issue number is present; never a duplicate PR).

Two things the model DELIVER did are deliberately NOT done here:
  - the docstring refresh — a model judgment; a pipeline that wants it keeps model DELIVER;
  - opening a PR when the branch has no commits — that would be a silent empty PR, so this
    runner fails the stage honestly instead.

Every subprocess call goes through an injected ``runner`` (default ``subprocess.run``) so
unit tests never touch the network or a real ``gh``.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
from collections.abc import Callable

from orchestrator.schemas.enums import ExecutionMode, Provider, ResultStatus
from orchestrator.schemas.work import StageResult, WorkItem

from .transport import RawResult, _tag_head, to_stage_result

# Base refs tried, in order, to answer "are there commits to deliver?" — the branch's
# upstream, then the remote's default branch, then the conventional names. Project-agnostic:
# the first ref that resolves wins; if none do we fall back to "does HEAD exist at all".
_BASE_CANDIDATES = ("@{u}", "origin/HEAD", "origin/main", "main")


class _DeliverError(Exception):
    """A deterministic-deliver failure (no commits / push / gh error) → ResultStatus.FAILURE."""


class DeterministicDeliverRunner:
    """In-process ENGINE-lane runner for the DELIVER stage (delegated from the ENGINE cell)."""

    def __init__(
        self,
        project: object,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        timeout_s: int = 300,
    ) -> None:
        self._project = project  # kept for parity/future use; deliver is git/gh + context
        self._runner = runner or subprocess.run
        self._timeout_s = timeout_s

    def dispatch(self, work: WorkItem) -> StageResult:
        try:
            out = self._deliver(work)
        except _DeliverError as exc:
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:deliver")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        except Exception as exc:  # noqa: BLE001 - every dispatch MUST yield a StageResult,
            # never an escaped exception (an injected runner raising, a JSON parse blow-up).
            raw = RawResult(None, exit_code=1, error=str(exc), invocation="engine:deliver")
            return to_stage_result(work, raw, ResultStatus.FAILURE,
                                   mode=ExecutionMode.ENGINE, provider=Provider.NONE)
        checkpoint = (
            _tag_head(work.cwd, work.checkpoint_tag)
            if work.checkpoint_tag and work.cwd else None
        )
        raw = RawResult(out, exit_code=0, invocation="engine:deliver", checkpoint=checkpoint)
        return to_stage_result(work, raw, ResultStatus.SUCCESS,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def _deliver(self, work: WorkItem) -> dict:
        cwd = work.cwd
        if not cwd:
            raise _DeliverError("deliver requires the task worktree (cwd); none set")
        ctx = work.context or {}
        branch = self._branch(cwd)
        if not self._has_commits(cwd):
            raise _DeliverError(
                f"no commits on {branch} vs base — refusing to open an empty PR"
            )
        self._push(cwd, branch)
        existing = self._existing_pr(cwd, branch, ctx)
        if existing:
            # Fix cycle: the branch was already pushed above, so the PR now reflects the new
            # commits. Reuse it — never open a duplicate.
            self._comment_fix_cycle(cwd, branch, existing, ctx)
            return {"pr_number": existing["number"], "pr_url": existing["url"], "reused": True}
        return self._open_pr(cwd, branch, work, ctx)

    def _comment_fix_cycle(self, cwd: str, branch: str, existing: dict, ctx: dict) -> None:
        """Best-effort note on a REUSED PR that a review fix cycle re-pushed the branch (#68 —
        the optional half of #33's reuse path). Trail-only: a missing selector or a
        failing/absent ``gh`` never fails the stage (the push + reuse already succeeded), so
        the comment is swallowed silently."""
        # gh pr comment takes the PR as a positional selector (url or number), not a flag.
        selector = existing.get("url") or (str(existing["number"]) if existing.get("number") else "")
        if not selector:
            return
        cycles = _to_int(ctx.get("review_cycles"))
        # #118: only a genuine review fix cycle names the review-fix commits + cycle number. A
        # raw re-run reusing the PR carries no cycle info, so keep the wording generic.
        what = f"the review-fix commits (fix cycle {cycles})" if cycles else "updated commits"
        body = (
            f"Orchestrator re-pushed `{branch}` with {what}; "
            "this PR now reflects the latest changes."
        )
        # Advisory only: a failing/absent gh never breaks an already-successful deliver.
        with contextlib.suppress(Exception):
            self._run(["gh", "pr", "comment", selector, "--body", body], cwd)

    # --- git / gh (all via the injected runner) -------------------------------
    def _run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess:
        return self._runner(argv, cwd=cwd, capture_output=True, text=True, timeout=self._timeout_s)

    def _branch(self, cwd: str) -> str:
        proc = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
        branch = (proc.stdout or "").strip()
        if proc.returncode != 0 or not branch or branch == "HEAD":
            raise _DeliverError(
                f"could not resolve the task branch (git rev-parse: "
                f"{(proc.stderr or '').strip()[:200] or 'detached HEAD'})"
            )
        return branch

    def _has_commits(self, cwd: str) -> bool:
        for base in _BASE_CANDIDATES:
            proc = self._run(["git", "rev-list", "--count", f"{base}..HEAD"], cwd)
            if proc.returncode == 0:
                return _to_int((proc.stdout or "").strip()) > 0
        # No base ref resolved (offline / no remote): fall back to "does HEAD have commits".
        proc = self._run(["git", "rev-list", "--count", "HEAD"], cwd)
        return _to_int((proc.stdout or "").strip()) > 0

    def _push(self, cwd: str, branch: str) -> None:
        proc = self._run(["git", "push", "-u", "origin", branch], cwd)
        if proc.returncode != 0:
            raise _DeliverError(f"git push failed: {(proc.stderr or '').strip()[:200]}")

    def _existing_pr(self, cwd: str, branch: str, ctx: dict) -> dict | None:
        """Reuse an existing PR on a fix cycle. First trust the folded ``pr_url`` (the
        engine sets it once DELIVER has run); otherwise ask ``gh`` whether one already
        exists for this head branch (belt-and-suspenders so a re-run never duplicates)."""
        url = ctx.get("pr_url")
        if isinstance(url, str) and url:
            number = _pr_number(url) or _to_int(ctx.get("pr_number"))
            return {"url": url, "number": number}
        proc = self._run(
            ["gh", "pr", "list", "--head", branch, "--json", "number,url", "--limit", "1"], cwd
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            try:
                arr = json.loads(proc.stdout)
            except json.JSONDecodeError:
                arr = []
            if arr:
                return {"url": arr[0].get("url", ""),
                        "number": _to_int(arr[0].get("number"))}
        return None

    def _open_pr(self, cwd: str, branch: str, work: WorkItem, ctx: dict) -> dict:
        title = _pr_title(work, ctx)
        body = _pr_body(work, ctx)
        proc = self._run(
            ["gh", "pr", "create", "--head", branch, "--title", title, "--body", body], cwd
        )
        if proc.returncode != 0:
            raise _DeliverError(f"gh pr create failed: {(proc.stderr or '').strip()[:200]}")
        url = _first_url((proc.stdout or "").strip())
        if not url:
            raise _DeliverError(
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
        # Mirror the DELIVER template: a GitHub-issue task closes its issue on merge.
        lines += ["", f"Closes #{_issue_ref(issue)}"]
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
