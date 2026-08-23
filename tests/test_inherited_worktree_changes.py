"""#385: uncommitted work a REUSED worktree inherited from a previous, DEAD run must reach
the stages that could destroy it.

In-run salvage is deliberately committed-only (`_salvageable_commits` ignores uncommitted
changes; a checkpoint retry hard-resets scraps), and neither fires at a fresh run's intake
against a worktree it did not leave. So intake reports the state and the context plane
carries it forward — report-only: the engine cannot tell finished work from a half-write,
so it resets and adopts nothing.

Covers the two halves the runner test cannot: the fold (does it survive into downstream
context) and the prompt (is the stage TOLD what the line means).
"""

from __future__ import annotations

from orchestrator.schemas.enums import Stage
from orchestrator.schemas.status import Task
from orchestrator.stages import (
    _INHERITED_CHANGES_DIRECTIVE,
    _inherited_changes_present,
    render_prompt,
)
from orchestrator.state_machine import CONTEXT_KEYS, _absorb_outputs
from tests.test_context_plane import make_result_stub

_DIRTY = [" M orchestrator/engine.py", "?? tests/test_new.py"]
_MARKER = "Uncommitted changes already in the worktree"


def _intake(output: dict) -> Task:
    task = Task(task_id="t", run_id="r", created_at="x", updated_at="x")
    _absorb_outputs(task, make_result_stub(Stage.INTAKE, output))
    return task


# --- the fold ---------------------------------------------------------------------------
def test_inherited_changes_fold_into_downstream_context() -> None:
    """Fails if the keys are missing from CONTEXT_KEYS[INTAKE]: they would be dropped at
    the fold and no later stage would ever see them."""
    task = _intake({
        "branch": "task/385",
        "inherited_changes": _DIRTY,
        "inherited_changes_note": "2 uncommitted change(s) …",
    })
    assert task.context["inherited_changes"] == _DIRTY
    assert task.context["inherited_changes_note"].startswith("2 uncommitted")


def test_folded_keys_are_declared_on_intake() -> None:
    for key in ("inherited_changes", "inherited_changes_note"):
        assert key in CONTEXT_KEYS[Stage.INTAKE]


def test_clean_signal_folds_too() -> None:
    """"Clean" and "never looked" must not read alike downstream, so the explicit clean
    signal is folded rather than omitted."""
    task = _intake({"inherited_changes": [], "inherited_changes_note": "clean (no …)"})
    assert task.context["inherited_changes"] == []
    assert task.context["inherited_changes_note"].startswith("clean")


# --- the prompt directive ---------------------------------------------------------------
def test_directive_present_only_when_something_was_inherited() -> None:
    assert _inherited_changes_present({"inherited_changes": _DIRTY}) is True
    assert _inherited_changes_present({"inherited_changes": []}) is False
    assert _inherited_changes_present({}) is False
    assert _inherited_changes_present(None) is False


def test_directive_tolerates_untyped_context_shapes() -> None:
    """The value is folded from a stage output, so it is not typed — a bare string or a
    junk value must not raise on the way into a prompt."""
    assert _inherited_changes_present({"inherited_changes": " M a.py"}) is True
    assert _inherited_changes_present({"inherited_changes": ["", "  "]}) is False
    assert _inherited_changes_present({"inherited_changes": 7}) is False


def test_implement_prompt_warns_before_it_overwrites() -> None:
    """The live near-miss: IMPLEMENT would have rewritten ~2,400 lines of finished work.
    It must be told to look first."""
    prompt = render_prompt(
        Stage.IMPLEMENT, task_id="#9", title="t", body="b",
        context={"worktree": "/wt/9", "inherited_changes": _DIRTY},
    )
    assert _MARKER in prompt
    assert "INSPECT them" in prompt  # look before you write
    assert "do not silently write over them" in prompt


def test_clean_task_prompt_is_byte_identical_to_before() -> None:
    """Conditional by design: a clean run pays no tokens and the shared prefix is unchanged,
    so prompt-cache reuse across stages is not disturbed."""
    base = dict(task_id="#9", title="t", body="b")
    clean = render_prompt(Stage.IMPLEMENT, **base, context={"worktree": "/wt/9"})
    empty = render_prompt(
        Stage.IMPLEMENT, **base,
        context={"worktree": "/wt/9", "inherited_changes": [], "inherited_changes_note": "x"},
    )
    assert _MARKER not in clean
    # only the folded context lines differ; the instruction section is identical
    assert clean.split("## IMPLEMENT")[1] == empty.split("## IMPLEMENT")[1]


def test_every_prompt_reading_stage_is_warned() -> None:
    """Any stage that reads or writes the tree can be misled by inherited work — SCOPE
    caught it live only by accident, and REVIEW would otherwise judge a diff it cannot
    account for."""
    for stage in (Stage.SCOPE, Stage.IMPLEMENT, Stage.SIMPLIFY, Stage.TEST,
                  Stage.DELIVER, Stage.REVIEW):
        prompt = render_prompt(
            stage, task_id="#9", title="t", body="b",
            context={"inherited_changes": _DIRTY, "pr_url": "http://x/pr/1"},
        )
        assert _MARKER in prompt, stage


def test_directive_states_the_engine_did_not_act() -> None:
    """Report-only is the whole design: the stage must not assume the harness already
    resolved the tree for it."""
    text = _INHERITED_CHANGES_DIRECTIVE
    assert "nothing has reset, stashed, or adopted them" in text
    assert "may be FINISHED work" in text and "half-edit" in text
