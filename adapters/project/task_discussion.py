"""Bounded rendering shared by task sources that carry discussion comments."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_DISCUSSION_COMMENT_CAP = 20
_DISCUSSION_COMMENT_CHARS = 4_000
_DISCUSSION_TOTAL_CHARS = 16_000
_PROGRESS_COMMENT_MARKER = re.compile(r"<!--\s*orchestrator:progress(?::|\s|-->)", re.I)


@dataclass(frozen=True)
class _DiscussionComment:
    body: str
    author: str = ""
    created_at: str = ""
    updated_at: str = ""


def _comment_text(value: object) -> str:
    return str(value) if value is not None else ""


def _normalize_comment(raw: object) -> _DiscussionComment | None:
    """Normalize GitHub-shaped or local-file comment data for prompt rendering."""
    if isinstance(raw, str):
        comment = _DiscussionComment(body=raw)
    elif isinstance(raw, Mapping):
        author_value = raw.get("author")
        if isinstance(author_value, Mapping):
            author = _comment_text(author_value.get("login"))
        else:
            author = _comment_text(author_value)
        comment = _DiscussionComment(
            body=_comment_text(raw.get("body")),
            author=author,
            created_at=_comment_text(raw.get("createdAt") or raw.get("created_at")),
            updated_at=_comment_text(raw.get("updatedAt") or raw.get("updated_at")),
        )
    else:
        return None
    if not comment.body.strip() or _PROGRESS_COMMENT_MARKER.search(comment.body):
        return None
    return comment


def _comment_heading(comment: _DiscussionComment) -> str:
    author = comment.author
    if author and not author.startswith("@"):
        author = f"@{author}"
    heading = f"### Comment by {author or 'unknown author'}"
    if comment.created_at:
        heading += f" — {comment.created_at}"
    if comment.updated_at and comment.updated_at != comment.created_at:
        heading += f" (edited {comment.updated_at})"
    return heading


def append_discussion(body: str, comments: Sequence[object]) -> str:
    """Append bounded issue discussion to the task body that feeds every stage prompt.

    The newest eligible comments win both caps, but are rendered in chronological order.
    Orchestrator progress comments are excluded by their durable marker: they are a living
    status projection, not task guidance. Any count or character loss is disclosed in-band
    so a model and a human inspecting the snapshot cannot mistake the excerpt for the whole
    thread.
    """
    eligible = [comment for raw in comments if (comment := _normalize_comment(raw))]
    if not eligible:
        return body

    candidates = eligible[-_DISCUSSION_COMMENT_CAP:]
    omitted_comments = len(eligible) - len(candidates)
    dropped_chars = sum(len(comment.body) for comment in eligible[:omitted_comments])
    remaining = _DISCUSSION_TOTAL_CHARS
    kept_reversed: list[tuple[_DiscussionComment, str, bool]] = []
    for comment in reversed(candidates):
        kept_chars = min(len(comment.body), _DISCUSSION_COMMENT_CHARS, remaining)
        if kept_chars == 0:
            omitted_comments += 1
            dropped_chars += len(comment.body)
            continue
        rendered_body = comment.body[:kept_chars]
        was_truncated = kept_chars < len(comment.body)
        dropped_chars += len(comment.body) - kept_chars
        kept_reversed.append((comment, rendered_body, was_truncated))
        remaining -= kept_chars

    kept = list(reversed(kept_reversed))
    discussion = ["## Discussion"]
    if omitted_comments or dropped_chars:
        discussion.append(
            "[truncated by orchestrator: "
            f"kept {len(kept)} newest of {len(eligible)} eligible comments; "
            f"{dropped_chars} comment chars dropped]"
        )
    for comment, rendered_body, was_truncated in kept:
        if was_truncated:
            rendered_body += " … [truncated]"
        discussion.extend([_comment_heading(comment), rendered_body])

    rendered_discussion = "\n\n".join(discussion)
    return f"{body.rstrip()}\n\n{rendered_discussion}" if body.strip() else rendered_discussion
