"""Deterministic DELIVER runner — the ENGINE lane (no model call).

#33: publishing the task branch is mechanical git work. This runner verifies the branch
has commits vs its base, pushes it, verifies the push actually landed, and tags the
checkpoint — deterministically at $0, mirroring the DELIVER stage template in
orchestrator/stages.py.

#389 removed the PR half. DELIVER used to push AND open the pull request, which meant a
PR existed before REVIEW had judged anything and a rejected task re-pushed onto an open
PR on every fix cycle. Opening the PR now lives in ``deterministic_publish`` and runs
AFTER REVIEW approves. Two consequences for this module:

  - There is no PR lookup, no reuse, no stale-``pr_url`` revalidation and no replacement-
    base dance here any more. Those existed to keep a fix cycle from opening a duplicate
    or re-delivering onto a merged PR (#168, #378); with the PR opened once, after
    approval, that whole class is unreachable rather than guarded.
  - #168's "no commits, but a PR exists, so call it a reuse-success" special case is gone
    with it. A re-deliver on a fix cycle is now an ordinary re-push, so a branch whose
    head is ALREADY on the remote is a plain no-op success — nothing to send, nothing to
    reuse, and the same ``pushed_head_sha`` reported as if it had just been sent.

Two things the model DELIVER did are deliberately NOT done here:
  - the docstring refresh — a model judgment; a pipeline that wants it keeps model DELIVER;
  - pushing a branch with no commits — that would publish an empty change and hand PUBLISH
    an empty PR to open, so this runner fails the stage honestly instead.

Every subprocess call goes through an injected ``runner`` (default ``subprocess.run``) so
unit tests never touch the network or a real ``git``.
"""

from __future__ import annotations

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
    """A deterministic-deliver failure (no commits / push / git error) → ResultStatus.FAILURE."""


class DeterministicDeliverRunner:
    """In-process ENGINE-lane runner for the DELIVER stage (delegated from the ENGINE cell)."""

    def __init__(
        self,
        project: object,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        timeout_s: int = 300,
    ) -> None:
        self._project = project  # kept for parity/future use; deliver is git + context
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
            # never an escaped exception (an injected runner raising, a parse blow-up).
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
        branch = self._branch(cwd)
        if not self._has_commits(cwd):
            raise _DeliverError(
                f"no commits on {branch} vs base — refusing to publish an empty branch"
            )
        head = self._head_sha(cwd)
        if self._remote_head(cwd, branch) != head:
            # Not already published (or published at an older head): send it.
            self._push(cwd, branch)
        # Verify rather than assume: `git push` can exit 0 having sent nothing useful (a
        # stale lock, a hook rewriting the ref), and the engine's deliver gate treats the
        # reported sha as proof the work is recoverable from the remote. Re-resolving the
        # remote ref is what makes that claim true instead of hopeful.
        pushed = self._remote_head(cwd, branch)
        if pushed != head:
            raise _DeliverError(
                f"push did not land: origin/{branch} resolves to "
                f"{pushed or 'nothing'}, not the local head {head}"
            )
        return {"branch": branch, "pushed_head_sha": head}

    # --- git (all via the injected runner) ------------------------------------
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

    def _head_sha(self, cwd: str) -> str:
        proc = self._run(["git", "rev-parse", "HEAD"], cwd)
        sha = (proc.stdout or "").strip()
        if proc.returncode != 0 or not _is_sha(sha):
            raise _DeliverError(
                f"could not resolve HEAD: {(proc.stderr or '').strip()[:200] or sha or 'no output'}"
            )
        return sha

    def _remote_head(self, cwd: str, branch: str) -> str:
        """The sha ``origin/<branch>`` currently resolves to, or "" when it does not exist.

        Read straight from the remote (``git ls-remote``) rather than the local
        remote-tracking ref, which a concurrent fetch/prune can leave stale — the point of
        the check is to learn what the REMOTE has, not what this worktree last heard.
        """
        proc = self._run(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd)
        if proc.returncode != 0:
            return ""
        for line in (proc.stdout or "").splitlines():
            fields = line.split()
            if fields and _is_sha(fields[0]):
                return fields[0]
        return ""

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


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _to_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
