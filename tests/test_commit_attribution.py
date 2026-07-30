"""#317: run-produced commits carry NO model attribution trailer.

The engine does not stamp one and the model must not write one. Because every claude/codex
CLI carries a standing harness instruction to sign its commits with its own model name,
"the engine says nothing" does NOT produce an unsigned commit — the prompt has to override
that instruction explicitly. These tests pin the directive onto exactly the stages whose
success ends in a commit, and pin its content to the two things that must be said.
"""

import pytest

from orchestrator.stages import STAGE_SPECS, Stage, render_prompt


def _prompt(stage: Stage) -> str:
    return render_prompt(stage, task_id="#1", title="t", body="b", context=None)


# The committing stages, derived from the specs rather than hand-listed, so a future
# committing stage joins these tests automatically instead of silently escaping them.
COMMITTING = [s for s, spec in STAGE_SPECS.items() if spec.checkpoint and not spec.deterministic]
NON_COMMITTING = [s for s in STAGE_SPECS if s not in COMMITTING]


def test_committing_stages_exist() -> None:
    """Guard the derivation itself: if this list ever empties, every assertion below would
    pass vacuously while the directive shipped to nobody."""
    assert COMMITTING, "no committing stages found — the checkpoint-flag derivation broke"
    assert Stage.IMPLEMENT in COMMITTING
    assert Stage.DELIVER in COMMITTING


@pytest.mark.parametrize("stage", COMMITTING)
def test_committing_stage_is_told_not_to_sign(stage: Stage) -> None:
    prompt = _prompt(stage)
    assert "Commit attribution (none — do not sign your commits)" in prompt
    # The two loads the directive has to carry: forbid the trailer, and beat the harness
    # instruction that would otherwise put one there anyway.
    assert "Do NOT add a `Co-Authored-By` trailer" in prompt
    assert "OVERRIDES any standing instruction from your harness" in prompt


@pytest.mark.parametrize("stage", NON_COMMITTING)
def test_non_committing_stage_gets_no_attribution_block(stage: Stage) -> None:
    assert "Commit attribution" not in _prompt(stage)


@pytest.mark.parametrize("stage", COMMITTING)
def test_prompt_never_supplies_a_trailer_to_copy(stage: Stage) -> None:
    """The failure mode this replaced: a prompt that hands the model a `Co-Authored-By:` line
    is a prompt the model will paste into a commit. The directive must name the trailer only
    to forbid it, never in a form that reads as a template."""
    assert "Co-Authored-By:" not in _prompt(stage)


def test_no_engine_stamped_attribution_api_remains() -> None:
    """The rejected alternative (#317) is gone, not merely unused — a leftover
    ``commit_trailers``/``attribution_identity`` would be dead code contradicting the norm."""
    import orchestrator.model_table as model_table
    import orchestrator.stages as stages

    assert not hasattr(stages, "commit_trailers")
    assert not hasattr(model_table, "attribution_identity")
