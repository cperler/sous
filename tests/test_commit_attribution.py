"""Commit attribution is stamped from the DISPATCHED model, not model self-belief (#317).

The ``Co-Authored-By`` trailer is the only authorship record that travels with the repo
(events.jsonl / stage-costs.jsonl hold the truth but are local + gitignored), and on
batch-headless-1 both run-produced commits named a model the engine had not dispatched for
that stage — one of them ("Claude Opus 4.5") absent from the entire run. These tests pin the
identity table, the both-models policy, which stages get the directive, and the end-to-end
engine path where implement and deliver deliberately run DIFFERENT models.
"""

from __future__ import annotations

import re

from orchestrator.cost_ledger import CostLedger
from orchestrator.engine import Engine
from orchestrator.model_table import ENGINE_MODEL, attribution_identity
from orchestrator.schemas.enums import ExecutionLane, Stage
from orchestrator.stages import STAGE_SPECS, commit_trailers, render_prompt
from orchestrator.status_store import StatusStore
from tests.conftest import make_result

_TRAILER_RE = re.compile(r"^Co-Authored-By: (.+)$", re.MULTILINE)


def _engine(tmp_path, project, **kw) -> Engine:
    return Engine(StatusStore(tmp_path), CostLedger(tmp_path / "stage-costs.jsonl"),
                  project, **kw)


# --- the identity table ---------------------------------------------------------

def test_attribution_identity_names_the_dispatched_model() -> None:
    assert attribution_identity("claude-opus-5") == "Claude Opus 5 <noreply@anthropic.com>"
    assert attribution_identity("claude-sonnet-5") == "Claude Sonnet 5 <noreply@anthropic.com>"
    # Provider-aware address: a codex-routed stage is not credited to Anthropic.
    assert attribution_identity("gpt-5.5") == "GPT-5.5 <noreply@openai.com>"


def test_attribution_identity_has_nobody_to_credit_on_the_engine_lane() -> None:
    assert attribution_identity(ENGINE_MODEL) is None  # deterministic $0 stage: no model
    assert attribution_identity(None) is None
    assert attribution_identity("") is None


def test_unknown_model_id_attributes_under_its_raw_id_rather_than_inventing_a_name() -> None:
    # A retired/freshly-bumped id must not raise and must not be given a made-up display
    # name: the raw id is still something the engine really dispatched.
    assert attribution_identity("claude-opus-9") == "claude-opus-9 <noreply@anthropic.com>"
    # Unclassifiable vendor => the reserved placeholder, never a guessed one.
    assert attribution_identity("mystery-model") == "mystery-model <noreply@invalid>"


# --- the policy -----------------------------------------------------------------

def test_trailers_credit_both_the_implementer_and_the_committing_stage() -> None:
    lines = commit_trailers(stage_model="claude-sonnet-5", implement_model="claude-opus-5")
    assert lines == [
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>",  # wrote the code first
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>",  # authored the commit
    ]


def test_one_model_is_credited_once() -> None:
    lines = commit_trailers(stage_model="claude-opus-5", implement_model="claude-opus-5")
    assert lines == ["Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"]


def test_unattributable_models_are_skipped_not_faked() -> None:
    assert commit_trailers(stage_model=ENGINE_MODEL, implement_model="claude-opus-5") == [
        "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
    ]
    assert commit_trailers(stage_model=None, implement_model=None) == []


# --- the prompt directive -------------------------------------------------------

def _prompt(stage: Stage, **kw) -> str:
    return render_prompt(stage, task_id="t1", title="x", body="", **kw)


def test_committing_stage_is_given_its_trailers_and_told_not_to_self_identify() -> None:
    prompt = _prompt(Stage.DELIVER, stage_model="claude-sonnet-5",
                     implement_model="claude-opus-5")
    assert _TRAILER_RE.findall(prompt) == [
        "Claude Opus 5 <noreply@anthropic.com>",
        "Claude Sonnet 5 <noreply@anthropic.com>",
    ]
    # The harness-level "sign commits with your own name" instruction must be overridden.
    assert "OVERRIDES" in prompt
    assert "Do NOT state your own identity or version" in prompt


def test_every_committing_stage_gets_the_block_and_no_other_stage_does() -> None:
    for stage, spec in STAGE_SPECS.items():
        prompt = _prompt(stage, stage_model="claude-opus-5", implement_model="claude-opus-5")
        committing = spec.checkpoint and not spec.deterministic
        assert bool(_TRAILER_RE.search(prompt)) is committing, stage
    # The committing set is exactly the git-affecting model-lane stages.
    assert {s for s, sp in STAGE_SPECS.items() if sp.checkpoint and not sp.deterministic} == {
        Stage.IMPLEMENT, Stage.TEST, Stage.DELIVER
    }


def test_prompt_without_dispatch_models_is_byte_identical_to_the_pre_317_shape() -> None:
    # No engine-supplied model => no invented attribution (and every legacy caller — the
    # review-panel path included — renders exactly what it always did).
    assert "Co-Authored-By" not in _prompt(Stage.IMPLEMENT)


# --- end to end through the engine ----------------------------------------------

def test_deliver_trailer_matches_the_models_the_engine_dispatched(tmp_path, project) -> None:
    """The acceptance case: implement and deliver run DIFFERENT models (opus for the heavy
    reasoning stage, sonnet for the cheap prose one), and the trailer the deliver stage is
    given names exactly those two — sourced from the dispatch record, not from the model."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1")

    dispatched: dict[Stage, str] = {}
    prompts: dict[Stage, str] = {}
    while (work := eng.next_work("r1", "t1")) is not None:
        dispatched[work.stage] = str(work.model)
        prompts[work.stage] = work.prompt
        eng.record("r1", make_result(work))

    assert dispatched[Stage.IMPLEMENT] == "claude-opus-5"
    assert dispatched[Stage.DELIVER] == "claude-sonnet-5"  # cost-routed: a different model

    named = _TRAILER_RE.findall(prompts[Stage.DELIVER])
    assert named == [
        "Claude Opus 5 <noreply@anthropic.com>",
        "Claude Sonnet 5 <noreply@anthropic.com>",
    ]
    # Every name in the trailer belongs to a model this run actually dispatched — the
    # property batch-headless-1's "Claude Opus 4.5" violated.
    run_models = {attribution_identity(m) for m in dispatched.values()}
    assert set(named) <= run_models


def test_a_pinned_task_is_credited_to_the_pinned_model(tmp_path, project) -> None:
    """The trailer follows routing rather than a fixed string: a per-task model pin (#84)
    changes which model the engine dispatches, so it must change the attribution too."""
    eng = _engine(tmp_path, project)
    eng.create_run("r1", ExecutionLane.FULL)
    eng.add_task("r1", "t1", model="fable")

    implement_prompt = None
    while (work := eng.next_work("r1", "t1")) is not None:
        if work.stage is Stage.IMPLEMENT:
            implement_prompt = work.prompt
            assert str(work.model) == "claude-fable-5"
        eng.record("r1", make_result(work))

    assert implement_prompt is not None
    assert _TRAILER_RE.findall(implement_prompt) == ["Claude Fable 5 <noreply@anthropic.com>"]
