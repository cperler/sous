"""What GitHub will close when a PR merges, and how to stop it naming one issue (#613).

Shared by the two adapters that read a PR's ``closingIssuesReferences``: the GitHub task
source (the engine's after-DELIVER check) and the engine-lane DELIVER runner (which repairs
the PR body it can edit). GitHub-specific, so it lives on the adapter side of the boundary;
the engine only ever sees a list of issue numbers.
"""

from __future__ import annotations

import re

_CLOSING_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_PR_URL_REPO = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/pull/\d+")
_ISSUE_URL_REPO = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/issues/\d+")


def repo_of_pr_url(pr_url: str) -> str | None:
    """The ``owner/name`` a PR URL belongs to, or None when the URL has no such shape."""
    match = _PR_URL_REPO.match(pr_url or "")
    return match.group(1) if match else None


def _ref_repo(ref: dict) -> str | None:
    repository = ref.get("repository")
    if isinstance(repository, dict):
        owner = repository.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        name = repository.get("name")
        if login and name:
            return f"{login}/{name}"
    match = _ISSUE_URL_REPO.match(str(ref.get("url") or ""))
    return match.group(1) if match else None


def closing_numbers(refs: object, repo: str | None) -> list[int]:
    """Issue numbers in ``repo`` that a ``closingIssuesReferences`` payload would close.

    A reference to another repository does not match: its #42 is a different issue. A
    reference that does not say where it lives is counted, because a false "would close"
    fails loudly and a false "would not" closes the issue. A payload that is not a list means
    nobody knows, so it raises ``ValueError`` for the caller to report as unverified.
    """
    if not isinstance(refs, list):
        raise ValueError(f"closingIssuesReferences is not a list: {type(refs).__name__}")
    numbers: list[int] = []
    for ref in refs:
        if not isinstance(ref, dict) or not isinstance(ref.get("number"), int):
            continue
        ref_repo = _ref_repo(ref)
        if repo and ref_repo and ref_repo.casefold() != repo.casefold():
            continue
        numbers.append(ref["number"])
    return numbers


def rewrite_closing_keywords(body: str, issue: int, *, repo: str | None = None) -> str:
    """``body`` with every closing keyword aimed at ``issue`` turned into ``Part of``.

    Bare ``#N`` is always this repository's issue. The ``owner/name#N`` and full-URL forms
    are rewritten only when ``repo`` is given and they name it, so a keyword aimed at the same
    number in another repository keeps working. Text that merely mentions the issue, or a
    longer number that starts with the same digits, is left alone.
    """
    forms = [rf"#{issue}"]
    if repo:
        escaped = re.escape(repo)
        forms += [rf"{escaped}#{issue}", rf"https?://github\.com/{escaped}/issues/{issue}"]
    pattern = rf"\b{_CLOSING_KEYWORD}\s*:?\s*({'|'.join(forms)})(?!\w)"
    return re.sub(pattern, r"Part of \1", body, flags=re.IGNORECASE)
