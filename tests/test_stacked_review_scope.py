"""#310: a REVIEW dispatch for a STACKED task (composed on unmerged #216 batch dependencies)
must say which commits are the task's own.

The failure this guards against is silent either way: a reviewer that trusts the PR's
trunk-relative diff on a stacked branch either approves the dependency's commits as if this
review covered them, or rejects the task for a change its dependency made (a pointless fix
cycle). Observed live on #298/PR #309, where the reviewer only got it right by independently
thinking to `git show` its own commit.

Sibling of the #41 docs-only directive and the #62 design lens: engine-template-side, pure,
and safe on model-writable context because it ADDS attribution (which commits are yours)
without relaxing any criterion applied to them.
"""

from __future__ import annotations

from orchestrator.schemas.enums import Stage
from orchestrator.stages import _stacked_diff_directive, render_prompt, render_review_plan

_MARKER = "Stacked branch: review only THIS task's own commits"
_BASE = "c13b4a1f00dd00dd00dd00dd00dd00dd00dd00dd"


def _review_prompt(context: dict) -> str:
    return render_prompt(
        Stage.REVIEW,
        task_id="#298",
        title="stacked task",
        body="do the thing",
        context={"pr_url": "http://x/pr/309", **context},
    )


# --- the pure helper --------------------------------------------------------------------
def test_directive_names_deps_and_scopes_to_base_sha() -> None:
    out = _stacked_diff_directive({"composed_deps": ["task/261"], "base_sha": _BASE})
    assert _MARKER in out
    assert "`task/261`" in out
    # The actionable part: the reviewer is given the range, not just told it is stacked.
    assert f"`{_BASE}..HEAD`" in out
    assert f"git diff {_BASE}..HEAD" in out


def test_directive_lists_every_dep() -> None:
    out = _stacked_diff_directive(
        {"composed_deps": ["task/261", "task/277"], "base_sha": _BASE}
    )
    assert "`task/261`" in out and "`task/277`" in out


def test_directive_does_not_relax_review_criteria() -> None:
    # The trust-boundary contract: it narrows WHICH COMMITS, never what is judged in them.
    # If this wording ever softens, a fabricated composed_deps buys a thinner review.
    out = _stacked_diff_directive({"composed_deps": ["task/261"], "base_sha": _BASE})
    assert "does not narrow what you must judge" in out
    assert "every criterion above still applies in full" in out


def test_directive_degrades_without_base_sha() -> None:
    # No usable fork point: still warn off the raw PR diff and say how to find the boundary.
    for ctx in ({"composed_deps": ["task/261"]}, {"composed_deps": ["task/261"], "base_sha": ""}):
        out = _stacked_diff_directive(ctx)
        assert _MARKER in out
        assert "`task/261`" in out
        assert "no usable base SHA" in out
        assert "Do not take the raw PR diff" in out
        assert "..HEAD" not in out  # no half-formed range like "..HEAD"


def test_directive_empty_for_unstacked_or_malformed() -> None:
    assert _stacked_diff_directive(None) == ""
    assert _stacked_diff_directive({}) == ""
    assert _stacked_diff_directive({"base_sha": _BASE}) == ""
    assert _stacked_diff_directive({"composed_deps": [], "base_sha": _BASE}) == ""
    assert _stacked_diff_directive({"composed_deps": ["", "  "], "base_sha": _BASE}) == ""
    assert _stacked_diff_directive({"composed_deps": None}) == ""
    assert _stacked_diff_directive({"composed_deps": 7}) == ""  # non-sequence -> no directive
    # A bare string dep (a shape the fold can carry) is one dep, not a pile of characters.
    out = _stacked_diff_directive({"composed_deps": "task/261", "base_sha": _BASE})
    assert "`task/261`" in out


# --- the single-reviewer REVIEW prompt ---------------------------------------------------
def test_review_prompt_carries_scope_for_stacked_task() -> None:
    prompt = _review_prompt({"composed_deps": ["task/261"], "base_sha": _BASE})
    assert _MARKER in prompt
    assert f"`{_BASE}..HEAD`" in prompt
    assert "`task/261`" in prompt


def test_review_prompt_unchanged_for_unstacked_task() -> None:
    # Acceptance criterion: an unstacked task's review prompt is untouched. Asserted on the
    # INSTRUCTION section (everything from "## REVIEW" on) — that is the part #310 edits.
    # The context block above it legitimately differs between the two contexts below: an
    # empty composed_deps is still a folded key, so it renders its own "(none)" line.
    plain = _review_prompt({"base_sha": _BASE})
    assert _MARKER not in plain
    empty = _review_prompt({"base_sha": _BASE, "composed_deps": []})
    assert _MARKER not in empty
    marker = "\n\n## REVIEW\n"
    assert marker in plain
    assert plain.split(marker, 1)[1] == empty.split(marker, 1)[1]


def test_scope_is_review_only_not_implement() -> None:
    impl = render_prompt(
        Stage.IMPLEMENT,
        task_id="#298",
        title="stacked task",
        body="do the thing",
        context={"composed_deps": ["task/261"], "base_sha": _BASE},
    )
    assert _MARKER not in impl


# --- the multi-agent REVIEW panel (#73) --------------------------------------------------
def _plan_prompts(context: dict):
    plan = render_review_plan(
        task_id="#298",
        title="stacked task",
        body="do the thing",
        context={"pr_url": "http://x/pr/309", **context},
    )
    assert plan.finders  # guard: an empty panel would make the assertions below vacuous
    return [f.prompt for f in plan.finders]


def test_every_finder_carries_scope_for_stacked_task() -> None:
    prompts = _plan_prompts({"composed_deps": ["task/261"], "base_sha": _BASE})
    assert len(prompts) == 3  # the unrelaxed base panel: code / spec / tests
    for prompt in prompts:
        assert _MARKER in prompt
        assert f"`{_BASE}..HEAD`" in prompt


def test_no_finder_carries_scope_for_unstacked_task() -> None:
    for prompt in _plan_prompts({"base_sha": _BASE}):
        assert _MARKER not in prompt
