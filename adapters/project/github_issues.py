"""Shared GitHub-Issues task source (target.md §5, build-fresh D8).

Repo-agnostic: any project adapter whose tasks are GitHub issues instantiates this
with its repo slug (the selfhost adapter does). ``resolve`` reads an issue via
``gh``; ``mark_complete`` posts a PR link comment. The subprocess runner is
injectable so unit tests never hit the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable

from adapters.project.task_discussion import append_discussion
from orchestrator.ports.project import TaskSpec

Runner = Callable[[list[str]], str]


def _gh(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _issue_number(task_id: str) -> str:
    return task_id.lstrip("#")


def _ref_from_url(url: str) -> str:
    """Turn the ``gh issue create`` URL into a ``#N`` ref (matching the task_id
    convention ``resolve`` consumes), falling back to the raw URL if no number is found."""
    url = url.strip()
    m = re.search(r"/(\d+)/?$", url)
    return f"#{m.group(1)}" if m else url


def _followup_marker(idempotency_key: str) -> str:
    """Return a searchable, non-user-visible marker for one follow-up side effect."""
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"<!-- orchestrator:followup-idempotency:{digest} -->"


def _find_pr_url(texts: list[str], repo: str) -> str | None:
    """Best-effort PR discovery: the first ``github.com/<repo>/pull/<n>`` url across the
    given texts (issue body + comments), preferring this repo's PRs. The engine's
    ``mark_complete`` / ``publish_note`` post an ``Implemented via <pr_url>`` comment, so a
    finished task's PR is usually recoverable from its issue thread. Returns None if none."""
    pat = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
    fallback: str | None = None
    for text in texts:
        for m in pat.finditer(text or ""):
            url = f"https://github.com/{m.group(1)}/pull/{m.group(2)}"
            if m.group(1) == repo:
                return url
            fallback = fallback or url
    return fallback


def _parse_depends_on(body: str) -> list[str]:
    """Extract the ``#N`` refs from a ``Depends-on:`` line in an issue body — the spec
    front door's own encoding (``spec_intake._compose_body`` writes ``Depends-on: #12,
    #34``). Reading it back means a spec-filed issue arrives with its real edges already
    known, so the batch-plan model doesn't re-derive edges the front door already recorded.
    Case-insensitive on the label; de-duplicated in first-seen order."""
    refs: list[str] = []
    for line in body.splitlines():
        m = re.match(r"\s*depends[- ]on\s*:\s*(.+)", line, re.IGNORECASE)
        if not m:
            continue
        for ref in re.findall(r"#\d+", m.group(1)):
            if ref not in refs:
                refs.append(ref)
    return refs


class GitHubIssuesSource:
    """Task source backed by GitHub issues via the ``gh`` CLI."""

    def __init__(self, repo: str, *, runner: Runner = _gh, allow_closed: bool = False) -> None:
        self.repo = repo
        self._run = runner
        self._allow_closed = allow_closed

    def resolve(self, task_id: str) -> TaskSpec:
        num = _issue_number(task_id)
        raw = self._run(
            ["gh", "issue", "view", num, "--repo", self.repo,
             "--json", "number,title,body,labels,state,updatedAt,comments"]
        )
        data = json.loads(raw)
        # Already-closed early exit (ports implement-orchestrator.sh:519): a batch over
        # a stale issue list must not burn a full pipeline and open a PR against a
        # closed issue. Loud refusal, opt-out via allow_closed for deliberate re-runs.
        state = str(data.get("state", "")).upper()
        if state == "CLOSED" and not self._allow_closed:
            raise ValueError(
                f"issue #{num} in {self.repo} is CLOSED — refusing to run "
                f"(pass allow_closed=True to the task source to override)"
            )
        labels = [lbl["name"] for lbl in data.get("labels", [])]
        raw_comments = data.get("comments", []) or []
        comments = raw_comments if isinstance(raw_comments, list) else []
        return TaskSpec(
            task_id=task_id,
            title=data.get("title", ""),
            # Discussion is composed into the body rather than adding a TaskSpec field:
            # snapshot persistence, prompt rendering, fingerprinting, and refresh-spec then
            # all see the exact same bounded content without changing the adapter contract.
            body=append_discussion(data.get("body") or "", comments),
            issue_number=data.get("number"),
            depends_on=[],  # no analysis step yet; add-task --depends-on supplies edges
            labels=labels,
            # #271: when GitHub last touched the issue. Stamped onto the Task doc beside the
            # snapshot so ``refresh-spec`` / ``status --check-spec`` can say when the upstream
            # moved. Absent from an older `gh` payload → None, which reads as "unknown".
            updated_at=data.get("updatedAt") or None,
        )

    def list_tasks(self, label: str | None = None, limit: int = 50) -> list[TaskSpec]:
        """List OPEN issues as candidate batch tasks (#57). Optional, duck-typed method
        the batch-plan ``candidates`` fetch calls to hand the model its input — not part of
        the versioned TaskSource contract. Filters to open issues (a candidate batch is
        actionable work), optionally by ``label``, capped at ``limit``. Each returned
        ``TaskSpec`` pre-populates ``depends_on`` from any ``Depends-on:`` line in the body
        (the front door's encoding), so edges a spec already recorded aren't re-derived."""
        argv = [
            "gh", "issue", "list", "--repo", self.repo, "--state", "open",
            "--limit", str(limit), "--json", "number,title,body,labels",
        ]
        if label:
            argv += ["--label", label]
        raw = self._run(argv)
        data = json.loads(raw) if raw.strip() else []
        out: list[TaskSpec] = []
        for d in data:
            body = d.get("body") or ""
            labels = [lbl["name"] for lbl in d.get("labels", [])]
            out.append(
                TaskSpec(
                    task_id=f"#{d['number']}",
                    title=d.get("title", ""),
                    body=body,
                    issue_number=d.get("number"),
                    depends_on=_parse_depends_on(body),
                    labels=labels,
                )
            )
        return out

    def describe_issue(self, ref: str) -> dict:
        """Look up a filed issue's state + PR for the conformance gate (#18 bullet 2).

        Optional, duck-typed (like ``list_tasks`` / the evidence-out hooks; NOT part of
        the versioned contract). Returns ``{ref, state (open/closed), body, pr}``. Unlike
        ``resolve`` this does NOT refuse a closed issue — a conformance check is precisely
        about closed ones. PR discovery is best-effort from the issue body + comments."""
        num = _issue_number(ref)
        raw = self._run(
            ["gh", "issue", "view", num, "--repo", self.repo,
             "--json", "number,state,body,comments"]
        )
        data = json.loads(raw)
        texts = [data.get("body") or ""]
        texts += [c.get("body") or "" for c in data.get("comments", []) or []]
        return {
            "ref": ref,
            "state": str(data.get("state", "")).lower() or "unknown",
            "body": data.get("body") or "",
            "pr": _find_pr_url(texts, self.repo),
        }

    def mark_complete(self, task_id: str, pr_url: str | None = None) -> None:
        num = _issue_number(task_id)
        body = f"Implemented via {pr_url}" if pr_url else "Implemented."
        self._run(["gh", "issue", "comment", num, "--repo", self.repo, "--body", body])

    # --- optional evidence-out hooks (duck-typed by the engine; not part of the v1
    # TaskSource contract, so an older external adapter without them still runs) ------

    def publish_note(self, task_id: str, body: str, *, pr_url: str | None = None) -> None:
        """Post a run's completion evidence. Prefers the PR thread when a ``pr_url`` is
        known (a full URL locates the repo+PR by itself, so no ``--repo``); otherwise
        comments on the issue."""
        if pr_url:
            self._run(["gh", "pr", "comment", pr_url, "--body", body])
        else:
            self._run(
                ["gh", "issue", "comment", _issue_number(task_id), "--repo", self.repo,
                 "--body", body]
            )

    def publish_progress(
        self, task_id: str, body: str, *, marker: str, pr_url: str | None = None
    ) -> None:
        """Mid-run progress commentary (#64) with UPSERT semantics — ONE living comment/
        section per task, never comment spam. ``marker`` is an opaque token the engine owns
        (e.g. ``orchestrator:progress:#12``); this source wraps it in an HTML comment so it
        is invisible in the rendered Markdown yet findable on the next update.

        Routes on ``pr_url``: with a PR known, upsert a marker-delimited ``## Run progress``
        section in the PR body (one section, edited in place); otherwise upsert a marker-
        tagged issue comment (found via the REST comments list, edited via a PATCH, created
        when absent). Upsert (edit-in-place) is the deliberate default over per-stage
        append: a long run touches many stage boundaries, and a fresh comment each time
        would bury the issue — the reader wants the CURRENT picture, not a scroll of
        superseded ones."""
        if pr_url:
            self._upsert_pr_section(pr_url, body, marker)
        else:
            self._upsert_issue_comment(task_id, body, marker)

    def _upsert_issue_comment(self, task_id: str, body: str, marker: str) -> None:
        num = _issue_number(task_id)
        tag = f"<!-- {marker} -->"
        full = f"{tag}\n{body}"
        # Find our previous progress comment by the hidden marker. `gh api` (not
        # `gh issue view --json comments`) is used because the REST payload carries the
        # numeric comment id the PATCH edit needs; --paginate folds multiple pages into
        # one JSON array.
        raw = self._run(
            ["gh", "api", f"repos/{self.repo}/issues/{num}/comments", "--paginate"]
        )
        comments = json.loads(raw) if raw.strip() else []
        existing = next((c for c in comments if tag in (c.get("body") or "")), None)
        if existing is not None:
            self._run(
                ["gh", "api", "-X", "PATCH",
                 f"repos/{self.repo}/issues/comments/{existing['id']}",
                 "-f", f"body={full}"]
            )
        else:
            self._run(
                ["gh", "issue", "comment", num, "--repo", self.repo, "--body", full]
            )

    def _upsert_pr_section(self, pr_url: str, body: str, marker: str) -> None:
        start = f"<!-- {marker}:start -->"
        end = f"<!-- {marker}:end -->"
        section = f"{start}\n## Run progress\n\n{body}\n{end}"
        raw = self._run(["gh", "pr", "view", pr_url, "--json", "body"])
        current = (json.loads(raw).get("body") if raw.strip() else "") or ""
        if start in current and end in current:
            # Replace the existing section in place (a lambda replacement so a `body`
            # containing backslashes/group refs is inserted literally).
            new_body = re.sub(
                re.escape(start) + r".*?" + re.escape(end),
                lambda _m: section,
                current,
                flags=re.DOTALL,
            )
        else:
            new_body = f"{current}\n\n{section}" if current else section
        self._run(["gh", "pr", "edit", pr_url, "--body", new_body])

    def file_followup(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> str | None:
        """Create or recover a follow-up issue and return its URL.

        A keyed call first searches issue bodies for the exact hidden marker. This lets a
        caller recover when issue creation succeeded but its local receipt was interrupted.
        Unkeyed calls retain the original create-only behavior.
        """
        if idempotency_key is not None:
            marker = _followup_marker(idempotency_key)
            raw = self._run(
                ["gh", "api", f"repos/{self.repo}/issues?state=all&per_page=100",
                 "--paginate", "--slurp"]
            )
            pages = json.loads(raw) if raw.strip() else []
            matches = [issue for page in pages for issue in page]
            existing = next(
                (
                    issue.get("html_url")
                    for issue in matches
                    if marker in str(issue.get("body") or "") and issue.get("html_url")
                ),
                None,
            )
            if existing is not None:
                return str(existing)
            body = f"{body}\n\n{marker}"
        argv = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels or []:
            argv += ["--label", label]
        return self._run(argv).strip() or None

    def create_task(self, title: str, body: str, labels: list[str] | None = None) -> str:
        """Open a new issue and return its ``#N`` ref (the spec front door's filing hook,
        #18). Thin ``gh issue create`` wrapper on the same injectable runner as the rest of
        this source; the ``#N`` form feeds back into ``resolve`` and Depends-on lines."""
        argv = ["gh", "issue", "create", "--repo", self.repo, "--title", title, "--body", body]
        for label in labels or []:
            argv += ["--label", label]
        return _ref_from_url(self._run(argv))
