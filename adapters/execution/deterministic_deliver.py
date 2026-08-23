"""Deterministic DELIVER runner — the ENGINE lane (no model call).

#33: pushing the task branch and opening a PR is mechanical
git/gh work. This runner verifies the branch has commits vs its base, pushes it, and
opens — or, on a review fix cycle, revalidates and REUSES an OPEN matching PR (leaving an
advisory comment that the branch was re-pushed, #68) — a pull request, deterministically at
$0, mirroring the DELIVER stage
template in orchestrator/stages.py (PR title/body from the task id/title/issue; ``Closes #N``
when an issue number is present; never a duplicate PR).

Two things the model DELIVER did are deliberately NOT done here:
  - the docstring refresh — a model judgment; a pipeline that wants it keeps model DELIVER;
  - opening a PR when the branch has no commits AND none already exists — that would be a
    silent empty PR, so this runner fails the stage honestly instead. (A no-commit re-deliver
    whose PR is already open is a no-op reuse-success, not a failure — #168.)

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
            out, notices = self._deliver(work)
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
        raw = RawResult(
            out,
            exit_code=0,
            invocation="engine:deliver",
            checkpoint=checkpoint,
            execution_notices=notices,
        )
        return to_stage_result(work, raw, ResultStatus.SUCCESS,
                               mode=ExecutionMode.ENGINE, provider=Provider.NONE)

    def _deliver(self, work: WorkItem) -> tuple[dict, tuple[dict[str, object], ...]]:
        cwd = work.cwd
        if not cwd:
            raise _DeliverError("deliver requires the task worktree (cwd); none set")
        ctx = work.context or {}
        branch = self._branch(cwd)
        existing, stale = self._existing_pr(cwd, branch, ctx)
        replacement_base: str | None = None
        if stale is not None and existing is None:
            replacement_base = self._replacement_base(cwd, stale)
            self._require_current_base(cwd, branch, replacement_base)
        if not self._has_commits(cwd, replacement_base):
            # #168: a fix cycle can re-run DELIVER on a branch whose diff is unchanged (the
            # fix-implement correctly made no new commit — e.g. a docs-only change with nothing
            # to fix). If a PR already exists for this head, that PR IS the deliverable — the
            # no-op re-deliver is a SUCCESS reuse, not a breaker. Only a branch with no commits
            # AND no existing PR is a genuine empty-PR refusal. Do NOT push or leave a re-pushed
            # comment on the no-commit path: nothing changed, so there is nothing to re-push.
            if existing:
                out = {"pr_number": existing["number"], "pr_url": existing["url"],
                       "reused": True}
                return out, self._transition_notices(stale, out)
            raise _DeliverError(
                f"no commits on {branch} vs base — refusing to open an empty PR"
            )
        self._push(cwd, branch)
        if existing:
            # Fix cycle: the branch was already pushed above, so the PR now reflects the new
            # commits. Reuse it — never open a duplicate.
            self._comment_fix_cycle(cwd, branch, existing, ctx)
            out = {"pr_number": existing["number"], "pr_url": existing["url"], "reused": True}
            return out, self._transition_notices(stale, out)
        out = self._open_pr(cwd, branch, work, ctx, base=replacement_base)
        return out, self._transition_notices(stale, out)

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

    def _has_commits(self, cwd: str, replacement_base: str | None = None) -> bool:
        bases = ((f"origin/{replacement_base}",) if replacement_base else ()) + _BASE_CANDIDATES
        for base in bases:
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

    def _existing_pr(
        self, cwd: str, branch: str, ctx: dict
    ) -> tuple[dict | None, dict | None]:
        """Return an OPEN PR for ``branch`` plus any stale recorded-PR evidence.

        A folded ``pr_url`` is only a selector, never proof of delivery: GitHub keeps the
        URL after the PR is closed or merged, and may auto-delete its head branch. Validate
        both state and head before reuse, then independently look for another open PR for
        the branch so a crash after ``gh pr create`` remains idempotent.
        """
        stale: dict | None = None
        url = ctx.get("pr_url")
        if isinstance(url, str) and url:
            recorded = self._view_pr(cwd, url, fallback_number=_to_int(ctx.get("pr_number")))
            if recorded["state"] == "OPEN" and recorded["headRefName"] == branch:
                return recorded, None
            stale = recorded
        proc = self._run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json",
             "number,url,state,headRefName,baseRefName", "--limit", "1"],
            cwd,
        )
        if proc.returncode != 0:
            raise _DeliverError(
                f"could not look up an open PR for {branch}: "
                f"{(proc.stderr or '').strip()[:200] or 'gh pr list failed'}"
            )
        try:
            arr = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise _DeliverError(f"gh pr list returned invalid JSON for {branch}") from exc
        if not isinstance(arr, list):
            raise _DeliverError(f"gh pr list returned an invalid payload for {branch}")
        for raw in arr:
            if not isinstance(raw, dict):
                continue
            # The query itself constrains these fields; keep compatibility with older gh
            # versions/test doubles that omit requested discriminator fields.
            raw.setdefault("state", "OPEN")
            raw.setdefault("headRefName", branch)
            candidate = self._normalize_pr(raw)
            if candidate["state"] == "OPEN" and candidate["headRefName"] == branch:
                return candidate, stale
        return None, stale

    def _view_pr(self, cwd: str, selector: str, *, fallback_number: int) -> dict:
        proc = self._run(
            ["gh", "pr", "view", selector, "--json",
             "number,url,state,headRefName,baseRefName"],
            cwd,
        )
        if proc.returncode != 0:
            raise _DeliverError(
                f"could not validate recorded PR {selector}: "
                f"{(proc.stderr or '').strip()[:200] or 'gh pr view failed'}"
            )
        try:
            raw = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise _DeliverError(f"gh pr view returned invalid JSON for {selector}") from exc
        if not isinstance(raw, dict):
            raise _DeliverError(f"gh pr view returned an invalid payload for {selector}")
        raw.setdefault("url", selector)
        raw.setdefault("number", fallback_number or _pr_number(selector))
        return self._normalize_pr(raw)

    @staticmethod
    def _normalize_pr(raw: dict) -> dict:
        state = str(raw.get("state") or "").upper()
        head = str(raw.get("headRefName") or "")
        url = str(raw.get("url") or "")
        number = _to_int(raw.get("number")) or _pr_number(url)
        if state not in {"OPEN", "CLOSED", "MERGED"} or not head or not url or number <= 0:
            raise _DeliverError(
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

    def _replacement_base(self, cwd: str, stale: dict) -> str:
        base = str(stale.get("baseRefName") or "")
        if not base:
            proc = self._run(["gh", "repo", "view", "--json", "defaultBranchRef"], cwd)
            if proc.returncode != 0:
                raise _DeliverError("could not resolve the current base for a replacement PR")
            try:
                payload = json.loads(proc.stdout or "{}")
                base = str((payload.get("defaultBranchRef") or {}).get("name") or "")
            except (AttributeError, json.JSONDecodeError) as exc:
                raise _DeliverError(
                    "gh repo view returned invalid current-base evidence"
                ) from exc
        if not re.fullmatch(r"(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+", base):
            raise _DeliverError(f"recorded PR has an unsafe or empty base ref: {base!r}")
        return base

    def _require_current_base(self, cwd: str, branch: str, base: str) -> None:
        # Refresh remote-tracking refs, not only FETCH_HEAD: an explicit
        # ``git fetch origin <branch>`` can leave ``origin/<branch>`` stale.
        fetch = self._run(["git", "fetch", "--prune", "origin"], cwd)
        if fetch.returncode != 0:
            raise _DeliverError(
                f"could not refresh replacement PR base origin/{base}: "
                f"{(fetch.stderr or '').strip()[:200]}"
            )
        ancestor = self._run(
            ["git", "merge-base", "--is-ancestor", f"origin/{base}", "HEAD"], cwd
        )
        if ancestor.returncode == 1:
            raise _DeliverError(
                f"recorded PR is {branch}'s stale delivery, but {branch} does not contain "
                f"current origin/{base}; refusing to open a replacement that could regress "
                "the base. Merge or rebase the current base into the task branch, rerun "
                "tests, then retry DELIVER."
            )
        if ancestor.returncode != 0:
            raise _DeliverError(
                f"could not compare {branch} with current origin/{base}: "
                f"{(ancestor.stderr or '').strip()[:200]}"
            )

    @staticmethod
    def _transition_notices(stale: dict | None, current: dict) -> tuple[dict[str, object], ...]:
        if stale is None:
            return ()
        return ({
            "notice": "deliver_pr_transition",
            "previous_pr_url": stale["url"],
            "previous_pr_number": stale["number"],
            "previous_pr_state": stale["state"],
            "previous_head_ref": stale["headRefName"],
            "current_pr_url": current["pr_url"],
            "current_pr_number": current["pr_number"],
            "current_pr_reused": bool(current.get("reused")),
        },)

    def _open_pr(
        self, cwd: str, branch: str, work: WorkItem, ctx: dict, *, base: str | None = None
    ) -> dict:
        title = _pr_title(work, ctx)
        body = _pr_body(work, ctx)
        argv = ["gh", "pr", "create", "--head", branch]
        if base:
            argv += ["--base", base]
        argv += ["--title", title, "--body", body]
        proc = self._run(argv, cwd)
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
