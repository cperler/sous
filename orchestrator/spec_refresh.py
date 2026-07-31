"""#271 — pure comparison of a task's SNAPSHOTTED spec against the task source's current one.

A task's ``title``/``body`` are snapshotted onto the Task doc once, at ``add_task`` time, and
every stage prompt for the rest of the run renders from that copy (``Engine.next_work`` →
``stages.render_prompt``). That snapshot is deliberate: it is what makes a run reproducible
and a stage prompt byte-stable across attempts. What was missing is the *deliberate, audited*
way to move it — amending the upstream issue mid-run was silently a no-op, so the only
workarounds were rebuilding the run or hand-patching the status JSON behind the engine's back.

This module is the detection half, kept pure the same way ``commit_attribution`` is: no I/O,
no wall clock, no model call, no store access. It answers "how does the stored snapshot differ
from this freshly-resolved spec?" and nothing else, so ``Engine.refresh_spec`` /
``Engine.status`` can decide, write, and emit the events.

The fingerprint is the staleness verdict's basis rather than the source's ``updated_at``: a
timestamp moves for edits that never touch title or body (a label change, a comment, a
project-board move), and some sources cannot report one at all. Content compared to content
answers the only question a run cares about — is the prompt we would render today different
from the prompt we rendered from?
"""

from __future__ import annotations

import hashlib

# Diff summaries ride into an event line and a status document; a pathological issue body
# (or a title someone pasted a file into) must not blow either up.
_EXCERPT_CAP = 200


def fingerprint(title: str, body: str) -> str:
    """Stable content hash of the spec fields that reach a prompt.

    Both fields are covered because both are rendered (``render_prompt``'s task-spec
    section), and they are joined by a NUL — a byte neither a GitHub title nor body can
    contain — so no title/body split can collide with another (``"a"``/``"b"`` and
    ``"a\\0b"``/``""`` hash differently).
    """
    digest = hashlib.sha256()
    digest.update((title or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((body or "").encode("utf-8"))
    return digest.hexdigest()


def diff_summary(
    old_title: str, old_body: str, new_title: str, new_body: str
) -> dict:
    """Summarize the snapshot → source delta as a JSON-safe dict.

    Shape: ``{"changed", "title_changed", "body_changed", "old_fingerprint",
    "new_fingerprint", "title": {...}, "body": {...}}``. Each per-field block carries
    ``chars_before``/``chars_after``/``lines_added``/``lines_removed``, and the title block
    additionally carries the before/after strings (capped) because a title is short enough
    to read inline and is usually the fastest way to recognize WHICH edit landed.

    Line counts are multiset deltas, not an LCS diff: a body whose lines were only reordered
    reports 0 added / 0 removed while still reporting ``changed`` (the fingerprint covers
    it). That is the honest cheap answer — this is an audit summary, not a patch, and the
    caller who wants the exact text has both copies.
    """
    old_fp = fingerprint(old_title, old_body)
    new_fp = fingerprint(new_title, new_body)
    title_changed = (old_title or "") != (new_title or "")
    body_changed = (old_body or "") != (new_body or "")
    title_block = _field_delta(old_title, new_title)
    title_block["before"] = _cap(old_title or "")
    title_block["after"] = _cap(new_title or "")
    return {
        "changed": old_fp != new_fp,
        "title_changed": title_changed,
        "body_changed": body_changed,
        "old_fingerprint": old_fp,
        "new_fingerprint": new_fp,
        "title": title_block,
        "body": _field_delta(old_body, new_body),
    }


def _field_delta(before: str, after: str) -> dict:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    added, removed = _line_multiset_delta(before_lines, after_lines)
    return {
        "chars_before": len(before or ""),
        "chars_after": len(after or ""),
        "lines_added": added,
        "lines_removed": removed,
    }


def _line_multiset_delta(before: list[str], after: list[str]) -> tuple[int, int]:
    """(added, removed) counting each line as a multiset member — order-insensitive."""
    counts: dict[str, int] = {}
    for line in before:
        counts[line] = counts.get(line, 0) + 1
    added = 0
    for line in after:
        if counts.get(line, 0) > 0:
            counts[line] -= 1
        else:
            added += 1
    removed = sum(n for n in counts.values() if n > 0)
    return added, removed


def _cap(text: str) -> str:
    return text if len(text) <= _EXCERPT_CAP else text[:_EXCERPT_CAP] + "…"
